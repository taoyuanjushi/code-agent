from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from .approvals import (
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequest,
    ApprovalSource,
    build_default_approval_handler,
    build_resume_recovery_approval_handler,
    create_approval_decision,
    validate_approval_decision,
    validate_resume_recovery_decision,
)
from .context import (
    DEFAULT_MAX_INVENTORY_FILES,
    DEFAULT_MAX_TOTAL_SAMPLE_BYTES,
    collect_workspace_snapshot,
    format_snapshot,
)
from .instructions import discover_agent_instructions
from .model_client import (
    ModelClient,
    OpenAIResponsesClient,
    normalize_model_response,
)
from .prompts import build_system_prompt, build_user_prompt
from .sessions.codec import (
    approval_decision_to_dict,
    approval_request_to_dict,
    artifact_ref_from_dict,
    artifact_ref_to_dict,
    checkpoint_to_dict,
    normalized_model_response_from_dict,
    normalized_model_response_to_dict,
    pending_tool_call_to_dict,
    session_started_from_dict,
    session_started_to_dict,
    verification_result_to_dict,
    verification_tool_state_from_dict,
    verification_tool_state_to_dict,
)
from .sessions.models import (
    AgentSessionState,
    JsonObject,
    ModelFunctionCall,
    NormalizedModelResponse,
    PendingToolCall,
    SessionEvent,
    SessionEventType,
    SessionStarted,
    WorkspaceGuard,
)
from .sessions.recovery import (
    ToolRecoveryPlan,
    build_recovery_event_payload,
    find_recovery_reapproval_call_ids,
    plan_interrupted_tools,
)
from .sessions.reducer import rebuild_state, reduce_event
from .sessions.store import SessionStore
from .sessions.workspace_guard import (
    discover_git_head,
    validate_workspace_guard,
)
from .tool_outputs import (
    build_persistable_tool_output,
    pending_outputs_for_model,
)
from .tool_policy import (
    get_tool_policy,
    hash_tool_arguments,
    summarize_tool_arguments,
)
from .tools import VerificationToolState, execute_tool
from .types import AgentConfig, ToolResult, WorkspaceSnapshot
from .verification import VerificationResult


@dataclass(frozen=True)
class AgentRunReport:
    answer: str
    verifications: tuple[VerificationResult, ...]
    final_status: Literal["passed", "failed", "not_run"]
    session_id: str | None = None


FaultPoint = Literal[
    "after_model_response",
    "after_tool_side_effect",
    "after_tool_finished",
    "before_model_continuation",
]
FaultInjector = Callable[[FaultPoint], None]


@dataclass
class _SessionJournal:
    store: SessionStore
    session_id: str
    state: AgentSessionState

    def append(
        self,
        event_type: SessionEventType,
        payload: Mapping[str, object],
    ) -> None:
        event = self.store.append(self.session_id, event_type, payload)
        self.state = reduce_event(self.state, event)

    def checkpoint(self) -> None:
        self.append(
            "checkpoint.saved",
            checkpoint_to_dict(self.state.to_checkpoint()),
        )


def _build_audited_approval_handler(
    journal: _SessionJournal,
    handler: ApprovalHandler,
    *,
    validator: Callable[[ApprovalRequest, ApprovalDecision], None] = (
        validate_approval_decision
    ),
    failure_source: ApprovalSource | None = None,
) -> ApprovalHandler:
    def audited(request: ApprovalRequest) -> ApprovalDecision:
        decision: ApprovalDecision | None = None
        try:
            decision = handler(request)
            validator(request, decision)
        except BaseException as exc:
            source: ApprovalSource = failure_source or (
                decision.source
                if isinstance(decision, ApprovalDecision)
                else "interactive"
            )
            denied = create_approval_decision(
                request,
                approved=False,
                source=source,
                summary=(
                    "Approval handler failed before a valid decision: "
                    f"{type(exc).__name__}"
                ),
            )
            journal.append(
                "approval.decided",
                {
                    "request": approval_request_to_dict(request),
                    "decision": approval_decision_to_dict(denied),
                    "handler_error": exc,
                },
            )
            raise

        journal.append(
            "approval.decided",
            {
                "request": approval_request_to_dict(request),
                "decision": approval_decision_to_dict(decision),
            },
        )
        return decision

    return audited


class SessionResumeError(RuntimeError):
    """Base error raised when a durable session cannot be resumed safely."""

    code = "session_resume_error"


class SessionAlreadyCompletedError(SessionResumeError):
    code = "session_already_completed"


class ResumeTurnLimitError(SessionResumeError):
    code = "resume_turn_limit_exceeded"


class ResumeModelContextUnavailable(SessionResumeError):
    code = "resume_model_context_unavailable"


class _AgentTurnLimitError(RuntimeError):
    pass


def run_agent(
    task: str,
    config: AgentConfig,
    model_client: ModelClient | None = None,
    session_store: SessionStore | None = None,
    approval_handler: ApprovalHandler | None = None,
    fault_injector: FaultInjector | None = None,
) -> str:
    return run_agent_with_report(
        task,
        config,
        model_client=model_client,
        session_store=session_store,
        approval_handler=approval_handler,
        fault_injector=fault_injector,
    ).answer


def run_agent_with_report(
    task: str,
    config: AgentConfig,
    model_client: ModelClient | None = None,
    session_store: SessionStore | None = None,
    approval_handler: ApprovalHandler | None = None,
    fault_injector: FaultInjector | None = None,
) -> AgentRunReport:
    journal = _start_session(task, config, session_store=session_store)
    audited_approval_handler = _build_audited_approval_handler(
        journal,
        approval_handler or build_default_approval_handler(config),
    )

    try:
        client = model_client or OpenAIResponsesClient()
        verification_state = VerificationToolState(
            task=task,
            max_fix_attempts=config.max_fix_attempts,
        )
        response = _create_initial_model_response(
            task,
            config,
            client,
            journal,
        )
        _inject_fault(fault_injector, "after_model_response")

        for _turn in range(config.max_turns):
            _print_normalized_response(response)
            if not response.function_calls:
                report = _build_final_report(
                    response,
                    verification_state,
                    session_id=journal.session_id,
                )
                _complete_session(journal, report)
                return report

            tool_outputs = _execute_tool_batch(
                config,
                response,
                verification_state,
                journal,
                audited_approval_handler,
                fault_injector=fault_injector,
            )
            _inject_fault(fault_injector, "before_model_continuation")
            response = _create_continuation_model_response(
                config,
                client,
                journal,
                tool_outputs,
            )
            _inject_fault(fault_injector, "after_model_response")

        raise _AgentTurnLimitError(
            f"Agent stopped after reaching max turn limit ({config.max_turns})."
        )
    except KeyboardInterrupt as exc:
        _record_terminal_event(
            journal,
            "session.interrupted",
            {
                "reason": "keyboard_interrupt",
                "error": exc,
                "phase": journal.state.phase,
                "turn_index": journal.state.turn_index,
            },
            original_error=exc,
        )
        raise
    except _AgentTurnLimitError as exc:
        _record_terminal_event(
            journal,
            "session.failed",
            {
                "reason": "turn_limit",
                "error": exc,
                "phase": journal.state.phase,
                "turn_index": journal.state.turn_index,
                "max_turns": config.max_turns,
            },
            original_error=exc,
        )
        raise
    except Exception as exc:
        _record_terminal_event(
            journal,
            "session.failed",
            {
                "reason": "exception",
                "error": exc,
                "phase": journal.state.phase,
                "turn_index": journal.state.turn_index,
                "model_request_may_have_succeeded": (
                    journal.state.model_request_pending
                ),
            },
            original_error=exc,
        )
        raise


def resume_agent(
    session_id: str,
    workspace: str | Path,
    model_client: ModelClient | None = None,
    session_store: SessionStore | None = None,
    approval_handler: ApprovalHandler | None = None,
    recovery_approval_handler: ApprovalHandler | None = None,
    fault_injector: FaultInjector | None = None,
) -> str:
    """Resume one durable session after validating its workspace guard."""

    return resume_agent_with_report(
        session_id,
        workspace,
        model_client=model_client,
        session_store=session_store,
        approval_handler=approval_handler,
        recovery_approval_handler=recovery_approval_handler,
        fault_injector=fault_injector,
    ).answer


def resume_agent_with_report(
    session_id: str,
    workspace: str | Path,
    model_client: ModelClient | None = None,
    session_store: SessionStore | None = None,
    approval_handler: ApprovalHandler | None = None,
    recovery_approval_handler: ApprovalHandler | None = None,
    fault_injector: FaultInjector | None = None,
) -> AgentRunReport:
    """Resume a session without repeating completed tools or ignoring drift."""

    workspace_path = Path(workspace).resolve()
    store = session_store or SessionStore(workspace_path)
    if store.workspace != workspace_path:
        raise ValueError(
            "session_store workspace must match the requested resume workspace."
        )

    with store.exclusive_writer(session_id):
        events = store.load(session_id, repair_tail=True)
        state = rebuild_state(events)
        if state.status == "completed":
            raise SessionAlreadyCompletedError(
                f"Session {session_id} is already completed; use replay instead."
            )

        started = _session_started_from_events(events)
        config = _resume_config(started, workspace_path)
        recovery_plans = plan_interrupted_tools(
            workspace_path,
            events,
            state,
        )
        validate_workspace_guard(
            workspace_path,
            started,
            state,
            recovery_plans=recovery_plans,
        )
        recovery_call_ids = set(
            find_recovery_reapproval_call_ids(events, state)
        )
        retry_pending_model_request = state.model_request_pending
        previous_status = state.status
        journal = _SessionJournal(
            store=store,
            session_id=session_id,
            state=state,
        )
        journal.append(
            "session.resumed",
            {
                "reason": "explicit_resume",
                "previous_status": previous_status,
                "phase": state.phase,
                "turn_index": state.turn_index,
                "retry_pending_model_request": retry_pending_model_request,
            },
        )

        audited_approval_handler = _build_audited_approval_handler(
            journal,
            approval_handler or build_default_approval_handler(config),
        )
        audited_recovery_handler = _build_audited_approval_handler(
            journal,
            recovery_approval_handler
            or build_resume_recovery_approval_handler(),
            validator=validate_resume_recovery_decision,
            failure_source="resume_recovery",
        )

        try:
            client = model_client
            recovery_call_ids.update(
                _apply_recovery_plans(journal, recovery_plans)
            )
            verification_state = verification_tool_state_from_dict(
                journal.state.verification_state
            )
            return _resume_session_loop(
                config,
                client,
                journal,
                verification_state,
                audited_approval_handler,
                audited_recovery_handler,
                recovery_call_ids=frozenset(recovery_call_ids),
                retry_initial_model_request=(
                    retry_pending_model_request
                    and state.phase == "awaiting_initial_model"
                ),
                fault_injector=fault_injector,
            )
        except KeyboardInterrupt as exc:
            _record_terminal_event(
                journal,
                "session.interrupted",
                {
                    "reason": "keyboard_interrupt",
                    "error": exc,
                    "phase": journal.state.phase,
                    "turn_index": journal.state.turn_index,
                    "resumed": True,
                },
                original_error=exc,
            )
            raise
        except (_AgentTurnLimitError, ResumeTurnLimitError) as exc:
            _record_terminal_event(
                journal,
                "session.failed",
                {
                    "reason": "turn_limit",
                    "error": exc,
                    "phase": journal.state.phase,
                    "turn_index": journal.state.turn_index,
                    "max_turns": config.max_turns,
                    "resumed": True,
                },
                original_error=exc,
            )
            raise
        except ResumeModelContextUnavailable as exc:
            _record_terminal_event(
                journal,
                "session.failed",
                {
                    "reason": exc.code,
                    "error": exc,
                    "phase": journal.state.phase,
                    "turn_index": journal.state.turn_index,
                    "model_request_may_have_succeeded": (
                        journal.state.model_request_pending
                    ),
                    "resumed": True,
                },
                original_error=exc,
            )
            raise
        except Exception as exc:
            _record_terminal_event(
                journal,
                "session.failed",
                {
                    "reason": "exception",
                    "error": exc,
                    "phase": journal.state.phase,
                    "turn_index": journal.state.turn_index,
                    "model_request_may_have_succeeded": (
                        journal.state.model_request_pending
                    ),
                    "resumed": True,
                },
                original_error=exc,
            )
            raise

def _apply_recovery_plans(
    journal: _SessionJournal,
    plans: Sequence[ToolRecoveryPlan],
) -> set[str]:
    recovery_call_ids: set[str] = set()
    appended = False
    for plan in plans:
        if plan.disposition == "reuse_completed":
            continue
        payload = build_recovery_event_payload(
            plan,
            store=journal.store,
            session_id=journal.session_id,
        )
        journal.append("tool.recovered", payload)
        appended = True
        if plan.requires_explicit_approval:
            recovery_call_ids.add(plan.call_id)
    if appended:
        journal.checkpoint()
    return recovery_call_ids


def _resume_session_loop(
    config: AgentConfig,
    client: ModelClient | None,
    journal: _SessionJournal,
    verification_state: VerificationToolState,
    approval_handler: ApprovalHandler,
    recovery_approval_handler: ApprovalHandler,
    *,
    recovery_call_ids: frozenset[str],
    retry_initial_model_request: bool,
    fault_injector: FaultInjector | None,
) -> AgentRunReport:
    initial_retry_pending = retry_initial_model_request
    recovery_ids = set(recovery_call_ids)

    while True:
        phase = journal.state.phase
        if phase == "awaiting_initial_model":
            if client is None:
                client = OpenAIResponsesClient()
            response = _resume_initial_model_response(
                config,
                client,
                journal,
                retry_persisted_request=initial_retry_pending,
            )
            initial_retry_pending = False
            _inject_fault(fault_injector, "after_model_response")
            if response.function_calls:
                _print_normalized_response(response)
            continue

        if phase == "awaiting_tools":
            if journal.state.turn_index > config.max_turns:
                raise ResumeTurnLimitError(
                    "Resumed session has no remaining tool-call turns: "
                    f"turn_index={journal.state.turn_index}, "
                    f"max_turns={config.max_turns}."
                )
            if any(call.started for call in journal.state.pending_tool_calls):
                raise SessionResumeError(
                    "Interrupted tool reconciliation left a started call pending."
                )
            response = _pending_response_from_state(journal.state)
            handlers_by_call_id = {
                call_id: recovery_approval_handler
                for call_id in recovery_ids
            }
            _execute_tool_batch(
                config,
                response,
                verification_state,
                journal,
                approval_handler,
                fault_injector=fault_injector,
                approval_handlers_by_call_id=handlers_by_call_id,
            )
            recovery_ids.clear()
            continue

        if phase == "awaiting_model":
            if client is None:
                client = OpenAIResponsesClient()
            tool_outputs = pending_outputs_for_model(
                journal.state.pending_tool_outputs
            )
            _inject_fault(fault_injector, "before_model_continuation")
            response = _resume_continuation_model_response(
                config,
                client,
                journal,
                tool_outputs,
            )
            _inject_fault(fault_injector, "after_model_response")
            if response.function_calls:
                _print_normalized_response(response)
            continue

        if phase == "finalizing":
            response = _last_model_response(
                journal.store.load(journal.session_id)
            )
            _print_normalized_response(response)
            report = _build_final_report(
                response,
                verification_state,
                session_id=journal.session_id,
            )
            _complete_session(journal, report)
            return report

        if phase == "completed":
            raise SessionAlreadyCompletedError(
                f"Session {journal.session_id} is already completed."
            )
        raise SessionResumeError(f"Unsupported resume phase: {phase!r}.")


def _resume_initial_model_response(
    config: AgentConfig,
    client: ModelClient,
    journal: _SessionJournal,
    *,
    retry_persisted_request: bool,
) -> NormalizedModelResponse:
    events = journal.store.load(journal.session_id)
    retry_of_seq: int | None = None
    if retry_persisted_request:
        requested = _last_event(events, "model.requested")
        if requested.payload.get("request_kind") != "initial":
            raise SessionResumeError(
                "Pending initial model request has incompatible persisted metadata."
            )
        instructions = _persisted_text(
            journal.store,
            journal.session_id,
            requested.payload.get("instructions"),
            "initial model instructions",
        )
        input_text = _persisted_text(
            journal.store,
            journal.session_id,
            requested.payload.get("input"),
            "initial model input",
        )
        retry_of_seq = requested.seq
    elif journal.state.context_created:
        context_event = _last_event(events, "context.created")
        workspace_context = _persisted_text(
            journal.store,
            journal.session_id,
            context_event.payload.get("formatted_context"),
            "workspace context",
        )
        repository_instructions = discover_agent_instructions(config.workspace)
        instructions = build_system_prompt(config, repository_instructions)
        input_text = build_user_prompt(journal.state.task, workspace_context)
    else:
        return _create_initial_model_response(
            journal.state.task,
            config,
            client,
            journal,
        )

    request_payload: dict[str, object] = {
        "request_kind": "initial",
        "turn_index": journal.state.turn_index + 1,
        "previous_response_id": None,
        "instructions": instructions,
        "input": input_text,
        "delivery_semantics": "at_least_once_after_unrecorded_response",
        "resumed": True,
    }
    if retry_of_seq is not None:
        request_payload["retry_of_seq"] = retry_of_seq
    journal.append("model.requested", request_payload)
    raw_response = client.create_initial_response(
        config=config,
        instructions=instructions,
        input_text=input_text,
    )
    return _record_model_response(journal, raw_response)


def _resume_continuation_model_response(
    config: AgentConfig,
    client: ModelClient,
    journal: _SessionJournal,
    tool_outputs: list[dict[str, Any]],
) -> NormalizedModelResponse:
    previous_response_id = journal.state.previous_response_id
    if previous_response_id is None:
        raise SessionResumeError(
            "Cannot resume model continuation without a previous response ID."
        )
    journal.append(
        "model.requested",
        {
            "request_kind": "tool_continuation",
            "turn_index": journal.state.turn_index + 1,
            "previous_response_id": previous_response_id,
            "tool_call_ids": [output["call_id"] for output in tool_outputs],
            "delivery_semantics": "at_least_once_after_unrecorded_response",
            "resumed": True,
        },
    )
    try:
        raw_response = client.create_tool_response(
            config=config,
            previous_response_id=previous_response_id,
            tool_outputs=tool_outputs,
        )
    except Exception as exc:
        raise ResumeModelContextUnavailable(
            "resume_model_context_unavailable: the saved previous response "
            f"{previous_response_id!r} could not be continued."
        ) from exc
    return _record_model_response(journal, raw_response)


def _pending_response_from_state(
    state: AgentSessionState,
) -> NormalizedModelResponse:
    if state.previous_response_id is None:
        raise SessionResumeError(
            "Pending tool calls do not have a previous model response ID."
        )
    return NormalizedModelResponse(
        response_id=state.previous_response_id,
        text="",
        reasoning_summary="",
        function_calls=tuple(
            ModelFunctionCall(
                call_id=call.call_id,
                name=call.name,
                arguments=call.arguments,
            )
            for call in state.pending_tool_calls
        ),
    )


def _last_model_response(
    events: Sequence[SessionEvent],
) -> NormalizedModelResponse:
    event = _last_event(events, "model.responded")
    raw = event.payload.get("response", event.payload)
    if not isinstance(raw, Mapping):
        raise SessionResumeError("Persisted model response is not an object.")
    return normalized_model_response_from_dict(raw)


def _session_started_from_events(
    events: Sequence[SessionEvent],
) -> SessionStarted:
    if not events or events[0].type != "session.started":
        raise SessionResumeError(
            "Session log does not begin with session.started."
        )
    raw = events[0].payload.get("session", events[0].payload)
    if not isinstance(raw, Mapping):
        raise SessionResumeError("Persisted session.started payload is invalid.")
    return session_started_from_dict(raw)


def _resume_config(
    started: SessionStarted,
    workspace: Path,
) -> AgentConfig:
    config = started.config
    model = _resume_string(config, "model")
    reasoning_effort = _resume_string(config, "reasoning_effort")
    if reasoning_effort not in {"none", "low", "medium", "high", "xhigh"}:
        raise SessionResumeError(
            f"Unsupported persisted reasoning effort: {reasoning_effort!r}."
        )
    permission_mode = _resume_string(config, "permission_mode")
    if permission_mode not in {"read-only", "workspace-write"}:
        raise SessionResumeError(
            f"Unsupported persisted permission mode: {permission_mode!r}."
        )
    return AgentConfig(
        workspace=str(workspace),
        model=model,
        reasoning_effort=cast(Any, reasoning_effort),
        max_turns=_resume_positive_int(config, "max_turns"),
        permission_mode=cast(Any, permission_mode),
        auto_approve_commands=_resume_bool(config, "auto_approve_commands"),
        auto_approve_edits=_resume_bool(config, "auto_approve_edits"),
        context_max_files=_resume_positive_int(config, "context_max_files"),
        context_max_bytes_per_file=_resume_positive_int(
            config,
            "context_max_bytes_per_file",
        ),
        max_fix_attempts=_resume_positive_int(config, "max_fix_attempts"),
    )


def _persisted_text(
    store: SessionStore,
    session_id: str,
    value: object,
    label: str,
) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and value.get("stored") is True:
        raw_ref = value.get("artifact")
        if not isinstance(raw_ref, Mapping):
            raise SessionResumeError(
                f"Persisted {label} is missing its artifact reference."
            )
        ref = artifact_ref_from_dict(raw_ref)
        encoding = ref.encoding or "utf-8"
        try:
            return store.get_artifact(session_id, ref).decode(encoding)
        except (LookupError, UnicodeDecodeError) as exc:
            raise SessionResumeError(
                f"Persisted {label} artifact is not decodable text."
            ) from exc
    raise SessionResumeError(
        f"Persisted {label} is unavailable for safe resume."
    )


def _last_event(
    events: Sequence[SessionEvent],
    event_type: SessionEventType,
) -> SessionEvent:
    for event in reversed(events):
        if event.type == event_type:
            return event
    raise SessionResumeError(
        f"Session does not contain a required {event_type} event."
    )


def _resume_string(config: Mapping[str, object], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SessionResumeError(
            f"Persisted session config {key!r} must be a non-empty string."
        )
    return value


def _resume_bool(config: Mapping[str, object], key: str) -> bool:
    value = config.get(key)
    if not isinstance(value, bool):
        raise SessionResumeError(
            f"Persisted session config {key!r} must be a boolean."
        )
    return value


def _resume_positive_int(config: Mapping[str, object], key: str) -> int:
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SessionResumeError(
            f"Persisted session config {key!r} must be a positive integer."
        )
    return value


def _start_session(
    task: str,
    config: AgentConfig,
    *,
    session_store: SessionStore | None,
) -> _SessionJournal:
    workspace = Path(config.workspace).resolve()
    store = session_store or SessionStore(workspace)
    if store.workspace != workspace:
        raise ValueError(
            "session_store workspace must match AgentConfig.workspace."
        )

    git_head = discover_git_head(workspace)
    persisted_config = store.privacy_policy.sanitize_config(config)
    persisted_config["workspace"] = str(workspace)
    guard = WorkspaceGuard(
        workspace=str(workspace),
        git_head=git_head,
        touched_file_hashes={},
    )
    started = SessionStarted(
        task=task,
        workspace=guard.workspace,
        config=persisted_config,
        git_head=git_head,
        workspace_guard=guard,
    )
    session_id = store.create(session_started_to_dict(started))
    state = rebuild_state(store.load(session_id))
    print(f"session: {session_id}")
    return _SessionJournal(store=store, session_id=session_id, state=state)


def _create_initial_model_response(
    task: str,
    config: AgentConfig,
    client: ModelClient,
    journal: _SessionJournal,
) -> NormalizedModelResponse:
    repository_instructions = discover_agent_instructions(config.workspace)
    snapshot = collect_workspace_snapshot(
        config.workspace,
        task,
        max_inventory_files=DEFAULT_MAX_INVENTORY_FILES,
        max_sample_files=config.context_max_files,
        max_bytes_per_file=config.context_max_bytes_per_file,
        max_total_sample_bytes=DEFAULT_MAX_TOTAL_SAMPLE_BYTES,
    )
    workspace_context = format_snapshot(snapshot)
    journal.append(
        "context.created",
        _context_payload(snapshot, workspace_context),
    )

    instructions = build_system_prompt(config, repository_instructions)
    input_text = build_user_prompt(task, workspace_context)
    journal.append(
        "model.requested",
        {
            "request_kind": "initial",
            "turn_index": journal.state.turn_index + 1,
            "previous_response_id": None,
            "instructions": instructions,
            "input": input_text,
            "delivery_semantics": "at_least_once_after_unrecorded_response",
        },
    )
    raw_response = client.create_initial_response(
        config=config,
        instructions=instructions,
        input_text=input_text,
    )
    return _record_model_response(journal, raw_response)


def _create_continuation_model_response(
    config: AgentConfig,
    client: ModelClient,
    journal: _SessionJournal,
    tool_outputs: list[dict[str, Any]],
) -> NormalizedModelResponse:
    previous_response_id = journal.state.previous_response_id
    if previous_response_id is None:
        raise RuntimeError("Cannot continue without a previous model response ID.")
    journal.append(
        "model.requested",
        {
            "request_kind": "tool_continuation",
            "turn_index": journal.state.turn_index + 1,
            "previous_response_id": previous_response_id,
            "tool_call_ids": [output["call_id"] for output in tool_outputs],
            "delivery_semantics": "at_least_once_after_unrecorded_response",
        },
    )
    raw_response = client.create_tool_response(
        config=config,
        previous_response_id=previous_response_id,
        tool_outputs=tool_outputs,
    )
    return _record_model_response(journal, raw_response)


def _record_model_response(
    journal: _SessionJournal,
    raw_response: Any,
) -> NormalizedModelResponse:
    response = normalize_model_response(raw_response)
    pending_calls = tuple(
        PendingToolCall(
            call_id=call.call_id,
            name=call.name,
            arguments=call.arguments,
            effect=get_tool_policy(call.name).effect,
            started=False,
        )
        for call in response.function_calls
    )
    journal.append(
        "model.responded",
        {
            "response": normalized_model_response_to_dict(response),
            "pending_tool_calls": [
                pending_tool_call_to_dict(call) for call in pending_calls
            ],
        },
    )
    journal.checkpoint()
    return response


def _execute_tool_batch(
    config: AgentConfig,
    response: NormalizedModelResponse,
    verification_state: VerificationToolState,
    journal: _SessionJournal,
    approval_handler: ApprovalHandler,
    *,
    fault_injector: FaultInjector | None = None,
    approval_handlers_by_call_id: Mapping[str, ApprovalHandler] | None = None,
) -> list[dict[str, Any]]:
    for call in response.function_calls:
        pending_call = _pending_call(journal.state, call.call_id)
        policy = get_tool_policy(call.name)
        journal.append(
            "tool.started",
            {
                "call_id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
                "effect": pending_call.effect,
                "arguments_sha256": hash_tool_arguments(call.arguments),
                "arguments_summary": summarize_tool_arguments(call.arguments),
                "requires_approval": policy.approval_required,
            },
        )

        before_verification = verification_tool_state_to_dict(verification_state)
        before_history_count = len(verification_state.verification_history)
        print(f"\ntool: {call.name}")
        call_approval_handler = approval_handler
        if approval_handlers_by_call_id is not None:
            call_approval_handler = approval_handlers_by_call_id.get(
                call.call_id,
                approval_handler,
            )
        result = execute_tool(
            config,
            call.name,
            call.arguments,
            state=verification_state,
            approval_handler=call_approval_handler,
            call_id=call.call_id,
        )
        _inject_fault(fault_injector, "after_tool_side_effect")
        print("ok" if result.ok else "failed")
        if result.output:
            print(_truncate_for_console(result.output))

        tool_output = build_persistable_tool_output(
            journal.store,
            journal.session_id,
            call.call_id,
            result,
        )
        after_verification = verification_tool_state_to_dict(verification_state)
        new_verifications = verification_state.verification_history[
            before_history_count:
        ]
        finished_payload: dict[str, object] = {
            "call_id": call.call_id,
            "name": call.name,
            "arguments": call.arguments,
            "effect": pending_call.effect,
            "tool_output": tool_output,
        }
        if before_verification != after_verification:
            finished_payload["verification_state"] = after_verification
        if (
            approval_handlers_by_call_id is not None
            and call.call_id in approval_handlers_by_call_id
        ):
            finished_payload["recovery_retry"] = True
        if result.data is not None:
            touched_hashes = result.data.get("touched_file_hashes")
            if isinstance(touched_hashes, Mapping):
                finished_payload["touched_file_hashes"] = dict(touched_hashes)
            execution_audit = _execution_audit(result.data)
            if execution_audit is not None:
                finished_payload["execution"] = execution_audit
        journal.append("tool.finished", finished_payload)
        _inject_fault(fault_injector, "after_tool_finished")

        for verification in new_verifications:
            journal.append(
                "verification.recorded",
                {
                    "result": verification_result_to_dict(verification),
                    "verification_state": after_verification,
                },
            )
        persisted_verification = verification_tool_state_to_dict(
            verification_tool_state_from_dict(journal.state.verification_state)
        )
        if persisted_verification != after_verification:
            raise RuntimeError(
                "Persisted verification state diverged from the tool state."
            )
        journal.checkpoint()

    if journal.state.pending_tool_calls:
        raise RuntimeError("Tool batch completed with pending calls still present.")
    return pending_outputs_for_model(journal.state.pending_tool_outputs)


def _execution_audit(data: Mapping[str, object]) -> dict[str, object] | None:
    data_type = data.get("type")
    if data_type == "command_result":
        keys = (
            "command",
            "cwd",
            "shell",
            "timeout_ms",
            "exit_code",
            "timed_out",
            "duration_ms",
        )
    elif data_type == "verification_result":
        keys = (
            "command_id",
            "kind",
            "argv",
            "cwd",
            "shell",
            "timeout_ms",
            "exit_code",
            "timed_out",
            "duration_ms",
            "status",
        )
    else:
        return None
    return {key: data[key] for key in keys if key in data}



def _build_final_report(
    response: NormalizedModelResponse,
    verification_state: VerificationToolState,
    *,
    session_id: str,
) -> AgentRunReport:
    answer = response.text
    final_status = _verification_final_status(verification_state)
    if final_status == "failed":
        answer = (
            f"{answer}\n\n{_verification_failure_note(verification_state)}"
        ).strip()
    return AgentRunReport(
        answer=answer,
        verifications=tuple(verification_state.verification_history),
        final_status=final_status,
        session_id=session_id,
    )


def _complete_session(
    journal: _SessionJournal,
    report: AgentRunReport,
) -> None:
    payload = {
        "answer": report.answer,
        "final_status": report.final_status,
        "session_id": report.session_id,
        "verifications": [
            verification_result_to_dict(result) for result in report.verifications
        ],
    }
    report_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    report_artifact = journal.store.put_artifact(
        journal.session_id,
        report_bytes,
        "application/json",
        encoding="utf-8",
    )
    journal.append(
        "session.completed",
        {
            "report": payload,
            "report_artifact": artifact_ref_to_dict(report_artifact),
        },
    )


def _record_terminal_event(
    journal: _SessionJournal,
    event_type: Literal["session.failed", "session.interrupted"],
    payload: Mapping[str, object],
    *,
    original_error: BaseException,
) -> None:
    if journal.state.status != "running":
        return
    try:
        journal.append(event_type, payload)
    except BaseException as persistence_error:
        original_error.add_note(
            "Additionally, the terminal session event could not be persisted: "
            f"{type(persistence_error).__name__}: {persistence_error}"
        )


def _context_payload(
    snapshot: WorkspaceSnapshot,
    workspace_context: str,
) -> dict[str, object]:
    return {
        "workspace_root": snapshot.root,
        "total_file_count": snapshot.total_file_count,
        "omitted_file_count": snapshot.omitted_file_count,
        "inventory": [
            {"path": file.path, "size": file.size} for file in snapshot.files
        ],
        "samples": [
            {
                "path": sample.path,
                "byte_count": len(sample.content.encode("utf-8")),
            }
            for sample in snapshot.samples
        ],
        "formatted_context": workspace_context,
    }


def _pending_call(state: AgentSessionState, call_id: str) -> PendingToolCall:
    for call in state.pending_tool_calls:
        if call.call_id == call_id:
            return call
    raise RuntimeError(f"No pending tool call exists for {call_id!r}.")


def _print_normalized_response(response: NormalizedModelResponse) -> None:
    if response.reasoning_summary:
        print(f"\nreasoning summary:\n{response.reasoning_summary}")
    if response.text:
        print(f"\n{response.text}")


def _verification_final_status(
    state: VerificationToolState,
) -> Literal["passed", "failed", "not_run"]:
    if not state.verification_history:
        return "not_run"

    latest_by_command = _latest_verification_results(state)
    if not all(result.status == "passed" for result in latest_by_command.values()):
        return "failed"
    if not all(
        state.passed_generations.get(command_id) == state.edit_generation
        for command_id in latest_by_command
    ):
        return "failed"
    return "passed"


def _verification_failure_note(state: VerificationToolState) -> str:
    latest_by_command = _latest_verification_results(state)
    messages: list[str] = []

    non_passing = [
        f"{command_id} ({result.status})"
        for command_id, result in latest_by_command.items()
        if result.status != "passed"
    ]
    if non_passing:
        messages.append(f"Verification did not pass: {', '.join(non_passing)}.")

    stale = [
        command_id
        for command_id, result in latest_by_command.items()
        if result.status == "passed"
        and state.passed_generations.get(command_id) != state.edit_generation
    ]
    if stale:
        messages.append(
            "Verification results are stale after the latest edit and were not "
            f"rerun: {', '.join(stale)}."
        )

    return " ".join(messages) or "Verification is incomplete."


def _latest_verification_results(
    state: VerificationToolState,
) -> dict[str, VerificationResult]:
    latest_by_command: dict[str, VerificationResult] = {}
    for result in state.verification_history:
        latest_by_command[result.command_id] = result
    return latest_by_command


def _get_response_id(response: Any) -> str:
    return normalize_model_response(response).response_id


def _find_function_calls(response: Any) -> list[dict[str, str]]:
    return [
        {
            "name": call.name,
            "arguments": call.arguments,
            "call_id": call.call_id,
        }
        for call in normalize_model_response(response).function_calls
    ]


def _get_output_text(response: Any) -> str:
    return normalize_model_response(response).text


def _print_response_messages(response: Any) -> None:
    _print_normalized_response(normalize_model_response(response))


def _inject_fault(
    injector: FaultInjector | None,
    point: FaultPoint,
) -> None:
    if injector is not None:
        injector(point)


def _truncate_for_console(value: str) -> str:
    limit = 2000
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n[console output truncated]"

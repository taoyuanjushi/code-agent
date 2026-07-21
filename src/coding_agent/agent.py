from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from .approvals import (
    ApprovalDecision,
    ApprovalHandler,
    ApprovalInputReader,
    ApprovalRequest,
    ApprovalSource,
    build_default_approval_handler,
    build_resume_recovery_approval_handler,
    create_approval_decision,
    render_approval_request,
    validate_approval_decision,
    validate_resume_recovery_decision,
)
from .context import (
    DEFAULT_MAX_INVENTORY_FILES,
    DEFAULT_MAX_TOTAL_SAMPLE_BYTES,
    collect_workspace_snapshot,
    format_snapshot,
)
from .explanations import (
    ExplanationReadEvidence,
    explanation_read_evidence_from_tool_data,
    explanation_read_evidence_list_from_dict,
    explanation_read_evidence_list_to_dict,
    merge_explanation_read_evidence,
    validate_explanation_answer,
)
from .instructions import discover_agent_instructions
from .model_client import (
    ModelClient,
    OpenAIResponsesClient,
    normalize_model_response,
)
from .prompts import build_system_prompt, build_user_prompt
from .reviews import (
    ReviewResult,
    review_result_from_dict,
    review_result_to_dict,
)
from .security.docker_backend import DockerSandboxBackend
from .security.models import SECURITY_POLICY_VERSION
from .sessions.codec import (
    approval_decision_to_dict,
    approval_request_to_dict,
    artifact_ref_from_dict,
    artifact_ref_to_dict,
    checkpoint_to_dict,
    normalized_model_response_from_dict,
    normalized_model_response_to_dict,
    pending_tool_call_to_dict,
    plan_state_from_dict,
    plan_state_to_dict,
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
from .task_modes import (
    TASK_MODES,
    TaskMode,
    validate_task_mode_configuration,
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
from .ui import UiEmitter, truncate_for_console
from .verification import VerificationResult


@dataclass(frozen=True)
class AgentRunReport:
    answer: str
    verifications: tuple[VerificationResult, ...]
    final_status: Literal["passed", "failed", "not_run"]
    session_id: str | None = None
    review: ReviewResult | None = None


@dataclass
class _ToolSecurityUiState:
    backend: str | None = None
    sandboxed: bool | None = None
    capability_status: str | None = None
    image_digest_status: str | None = None
    cleanup_failures: set[str] = field(default_factory=set)
    sensitive_identifiers: set[str] = field(default_factory=set)


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
    ui_emitter: UiEmitter,
    *,
    validator: Callable[[ApprovalRequest, ApprovalDecision], None] = (
        validate_approval_decision
    ),
    failure_source: ApprovalSource | None = None,
) -> ApprovalHandler:
    def audited(request: ApprovalRequest) -> ApprovalDecision:
        request_event = ui_emitter.emit(
            "approval.requested",
            _approval_request_ui_payload(request),
        )
        decision: ApprovalDecision | None = None
        try:
            if not isinstance(request_event.payload.get("message"), str):
                raise ValueError(
                    "Approval request is too large to display safely."
                )
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
            ui_emitter.emit(
                "approval.decided",
                _approval_decision_ui_payload(denied),
            )
            raise

        journal.append(
            "approval.decided",
            {
                "request": approval_request_to_dict(request),
                "decision": approval_decision_to_dict(decision),
            },
        )
        ui_emitter.emit(
            "approval.decided",
            _approval_decision_ui_payload(decision),
        )
        return decision

    return audited


def _approval_request_ui_payload(
    request: ApprovalRequest,
) -> dict[str, object]:
    try:
        message = render_approval_request(request)
    except ValueError:
        message = f"Approve {request.action}?\n{request.summary}"
    return {
        "call_id": request.call_id,
        "action": request.action,
        "summary": request.summary,
        "message": message,
    }


def _approval_decision_ui_payload(
    decision: ApprovalDecision,
) -> dict[str, object]:
    return {
        "approval_id": decision.approval_id,
        "call_id": decision.call_id,
        "action": decision.action,
        "summary": decision.summary,
        "outcome": decision.outcome,
        "source": decision.source,
    }


class SessionResumeError(RuntimeError):
    """Base error raised when a durable session cannot be resumed safely."""

    code = "session_resume_error"


class SessionAlreadyCompletedError(SessionResumeError):
    code = "session_already_completed"


class ResumeTurnLimitError(SessionResumeError):
    code = "resume_turn_limit_exceeded"


class ResumeModelContextUnavailable(SessionResumeError):
    code = "resume_model_context_unavailable"


class SessionSecurityDriftError(SessionResumeError):
    code = "session_security_drift"


class _AgentTurnLimitError(RuntimeError):
    pass


def _resolve_ui_emitter(ui_emitter: UiEmitter | None) -> UiEmitter:
    if ui_emitter is None:
        return UiEmitter()
    if not isinstance(ui_emitter, UiEmitter):
        raise TypeError("ui_emitter must be a UiEmitter or null.")
    return ui_emitter


def run_agent(
    task: str,
    config: AgentConfig,
    model_client: ModelClient | None = None,
    session_store: SessionStore | None = None,
    approval_handler: ApprovalHandler | None = None,
    fault_injector: FaultInjector | None = None,
    ui_emitter: UiEmitter | None = None,
    stream: bool = True,
    approval_input_reader: ApprovalInputReader | None = None,
) -> str:
    return run_agent_with_report(
        task,
        config,
        model_client=model_client,
        session_store=session_store,
        approval_handler=approval_handler,
        fault_injector=fault_injector,
        ui_emitter=ui_emitter,
        stream=stream,
        approval_input_reader=approval_input_reader,
    ).answer


def run_agent_with_report(
    task: str,
    config: AgentConfig,
    model_client: ModelClient | None = None,
    session_store: SessionStore | None = None,
    approval_handler: ApprovalHandler | None = None,
    fault_injector: FaultInjector | None = None,
    ui_emitter: UiEmitter | None = None,
    stream: bool = True,
    approval_input_reader: ApprovalInputReader | None = None,
) -> AgentRunReport:
    if not isinstance(stream, bool):
        raise TypeError("stream must be a boolean.")
    _validate_agent_task_mode(config)
    emitter = _resolve_ui_emitter(ui_emitter)
    if emitter.next_seq == 1:
        emitter.emit(
            "run.started",
            {
                "workspace": config.workspace,
                "model": config.model,
                "mode": config.permission_mode,
                "task_mode": config.task_mode,
                "sandbox": config.sandbox_mode,
            },
        )
    journal = _start_session(task, config, session_store=session_store)
    audited_approval_handler = _build_audited_approval_handler(
        journal,
        approval_handler
        or build_default_approval_handler(
            config,
            request_writer=lambda _message: None,
            input_reader=approval_input_reader,
        ),
        emitter,
    )

    try:
        client = model_client or OpenAIResponsesClient(
            ui_emitter=emitter,
            stream=stream,
        )
        verification_state = VerificationToolState(
            task=task,
            max_fix_attempts=config.max_fix_attempts,
        )
        emitter.emit(
            "model.started",
            {"request_kind": "initial", "turn_index": 1},
        )
        response = _create_initial_model_response(
            task,
            config,
            client,
            journal,
        )
        _emit_model_finished(
            emitter,
            response,
            task_mode=config.task_mode,
        )
        _inject_fault(fault_injector, "after_model_response")

        for _turn in range(config.max_turns):
            if not response.function_calls:
                report = _build_final_report(
                    response,
                    verification_state,
                    task_mode=config.task_mode,
                    review=journal.state.review,
                    explain_evidence=(
                        _explanation_read_evidence_from_events(
                            journal.store.load(journal.session_id)
                        )
                        if config.task_mode == "explain"
                        else ()
                    ),
                    session_id=journal.session_id,
                )
                _complete_session(journal, report)
                _emit_run_finished(emitter, report)
                return report

            tool_outputs = _execute_tool_batch(
                config,
                response,
                verification_state,
                journal,
                audited_approval_handler,
                emitter,
                fault_injector=fault_injector,
            )
            _inject_fault(fault_injector, "before_model_continuation")
            emitter.emit(
                "model.started",
                {
                    "request_kind": "tool_continuation",
                    "turn_index": journal.state.turn_index + 1,
                },
            )
            response = _create_continuation_model_response(
                config,
                client,
                journal,
                tool_outputs,
            )
            _emit_model_finished(
                emitter,
                response,
                task_mode=config.task_mode,
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
            ui_emitter=emitter,
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
            ui_emitter=emitter,
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
            ui_emitter=emitter,
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
    ui_emitter: UiEmitter | None = None,
    stream: bool = True,
    approval_input_reader: ApprovalInputReader | None = None,
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
        ui_emitter=ui_emitter,
        stream=stream,
        approval_input_reader=approval_input_reader,
    ).answer


def resume_agent_with_report(
    session_id: str,
    workspace: str | Path,
    model_client: ModelClient | None = None,
    session_store: SessionStore | None = None,
    approval_handler: ApprovalHandler | None = None,
    recovery_approval_handler: ApprovalHandler | None = None,
    fault_injector: FaultInjector | None = None,
    ui_emitter: UiEmitter | None = None,
    stream: bool = True,
    approval_input_reader: ApprovalInputReader | None = None,
) -> AgentRunReport:
    """Resume a session without repeating completed tools or ignoring drift."""

    if not isinstance(stream, bool):
        raise TypeError("stream must be a boolean.")
    emitter = _resolve_ui_emitter(ui_emitter)
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
        journal = _SessionJournal(
            store=store,
            session_id=session_id,
            state=state,
        )
        try:
            config = _resume_config(started, workspace_path)
            try:
                _validate_agent_task_mode(config)
            except ValueError as exc:
                raise SessionResumeError(
                    "Persisted task mode configuration is invalid: " + str(exc)
                ) from exc
            if emitter.next_seq == 1:
                emitter.emit(
                    "run.started",
                    _resume_run_started_ui_payload(
                        workspace_path,
                        session_id,
                        config,
                        state,
                    ),
                )
            _validate_resume_security(events, started, config, workspace_path)
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
        except KeyboardInterrupt as exc:
            _prepare_resume_preflight_terminal(journal, original_error=exc)
            _record_terminal_event(
                journal,
                "session.interrupted",
                {
                    "reason": "keyboard_interrupt",
                    "error": exc,
                    "phase": journal.state.phase,
                    "turn_index": journal.state.turn_index,
                    "resumed": True,
                    "preflight": True,
                },
                original_error=exc,
                ui_emitter=emitter,
            )
            raise
        except Exception as exc:
            _prepare_resume_preflight_terminal(journal, original_error=exc)
            _record_terminal_event(
                journal,
                "session.failed",
                {
                    "reason": "preflight_failure",
                    "error": exc,
                    "error_code": getattr(exc, "code", type(exc).__name__),
                    "phase": journal.state.phase,
                    "turn_index": journal.state.turn_index,
                    "resumed": True,
                    "preflight": True,
                },
                original_error=exc,
                ui_emitter=emitter,
            )
            raise

        recovery_call_ids = set(
            find_recovery_reapproval_call_ids(events, state)
        )
        retry_pending_model_request = state.model_request_pending
        previous_status = state.status
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
        if journal.state.plan.items:
            emitter.emit("plan.updated", plan_state_to_dict(journal.state.plan))

        audited_approval_handler = _build_audited_approval_handler(
            journal,
            approval_handler
            or build_default_approval_handler(
                config,
                request_writer=lambda _message: None,
                input_reader=approval_input_reader,
            ),
            emitter,
        )
        audited_recovery_handler = _build_audited_approval_handler(
            journal,
            recovery_approval_handler
            or build_resume_recovery_approval_handler(
                request_writer=lambda _message: None,
                input_reader=approval_input_reader,
            ),
            emitter,
            validator=validate_resume_recovery_decision,
            failure_source="resume_recovery",
        )

        try:
            client = model_client
            recovery_plans = _reconcile_interrupted_sandboxes(
                journal,
                events,
                config,
                recovery_plans,
                emitter,
            )
            recovery_summaries: dict[str, str] = {}
            recovery_call_ids.update(
                _apply_recovery_plans(
                    journal,
                    recovery_plans,
                    emitter,
                    recovery_summaries=recovery_summaries,
                )
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
                emitter,
                stream,
                recovery_call_ids=frozenset(recovery_call_ids),
                recovery_summaries=recovery_summaries,
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
                ui_emitter=emitter,
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
                ui_emitter=emitter,
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
                ui_emitter=emitter,
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
                ui_emitter=emitter,
            )
            raise


def _prepare_resume_preflight_terminal(
    journal: _SessionJournal,
    *,
    original_error: BaseException,
) -> None:
    """Make a terminal resume attempt appendable without consuming a pending request."""

    if journal.state.status == "running":
        return
    previous_status = journal.state.status
    try:
        journal.append(
            "session.resumed",
            {
                "reason": "resume_preflight",
                "previous_status": previous_status,
                "phase": journal.state.phase,
                "turn_index": journal.state.turn_index,
                "retry_pending_model_request": False,
            },
        )
    except BaseException as persistence_error:
        original_error.add_note(
            "Additionally, the failed resume attempt could not reopen the "
            "session for terminal persistence: "
            f"{type(persistence_error).__name__}: {persistence_error}"
        )


def _apply_recovery_plans(
    journal: _SessionJournal,
    plans: Sequence[ToolRecoveryPlan],
    ui_emitter: UiEmitter,
    *,
    recovery_summaries: dict[str, str] | None = None,
) -> set[str]:
    recovery_call_ids: set[str] = set()
    summaries = recovery_summaries if recovery_summaries is not None else {}
    appended = False
    for plan in plans:
        if plan.disposition == "reuse_completed":
            ui_emitter.emit(
                "tool.finished",
                {
                    "call_id": plan.call_id,
                    "name": plan.name,
                    "status": "passed",
                    "duration_ms": 0,
                    "summary": "recovered: reused completed tool output",
                    "output_truncated": False,
                },
            )
            continue
        payload = build_recovery_event_payload(
            plan,
            store=journal.store,
            session_id=journal.session_id,
        )
        journal.append("tool.recovered", payload)
        appended = True
        if plan.disposition == "safe_retry":
            summaries[plan.call_id] = "recovery: safe retry"
        elif plan.requires_explicit_approval:
            recovery_call_ids.add(plan.call_id)
            summaries[plan.call_id] = "recovery: reapproval required"
        elif plan.disposition == "recovered_completed":
            if plan.recovered_result is None:
                raise RuntimeError(
                    "Recovered completion is missing its UI result projection."
                )
            recovered_payload = _tool_finished_ui_payload(
                ModelFunctionCall(
                    call_id=plan.call_id,
                    name=plan.name,
                    arguments="{}",
                ),
                plan.recovered_result,
            )
            recovered_summary = recovered_payload.get("summary")
            recovered_payload["summary"] = (
                "recovered: completed tool result"
                if not isinstance(recovered_summary, str)
                else f"recovered: completed tool result\n{recovered_summary}"
            )
            ui_emitter.emit("tool.finished", recovered_payload)
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
    ui_emitter: UiEmitter,
    stream: bool,
    *,
    recovery_call_ids: frozenset[str],
    recovery_summaries: Mapping[str, str],
    retry_initial_model_request: bool,
    fault_injector: FaultInjector | None,
) -> AgentRunReport:
    initial_retry_pending = retry_initial_model_request
    recovery_ids = set(recovery_call_ids)
    recovery_start_summaries = dict(recovery_summaries)

    while True:
        phase = journal.state.phase
        if phase == "awaiting_initial_model":
            if client is None:
                client = OpenAIResponsesClient(
                    ui_emitter=ui_emitter,
                    stream=stream,
                )
            ui_emitter.emit(
                "model.started",
                {
                    "request_kind": "initial",
                    "turn_index": journal.state.turn_index + 1,
                },
            )
            response = _resume_initial_model_response(
                config,
                client,
                journal,
                retry_persisted_request=initial_retry_pending,
            )
            initial_retry_pending = False
            _inject_fault(fault_injector, "after_model_response")
            if response.function_calls:
                _emit_model_finished(
                    ui_emitter,
                    response,
                    task_mode=config.task_mode,
                )
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
                ui_emitter,
                fault_injector=fault_injector,
                approval_handlers_by_call_id=handlers_by_call_id,
                recovery_summaries_by_call_id=recovery_start_summaries,
            )
            recovery_ids.clear()
            recovery_start_summaries.clear()
            continue

        if phase == "awaiting_model":
            if client is None:
                client = OpenAIResponsesClient(
                    ui_emitter=ui_emitter,
                    stream=stream,
                )
            tool_outputs = pending_outputs_for_model(
                journal.state.pending_tool_outputs
            )
            _inject_fault(fault_injector, "before_model_continuation")
            ui_emitter.emit(
                "model.started",
                {
                    "request_kind": "tool_continuation",
                    "turn_index": journal.state.turn_index + 1,
                },
            )
            response = _resume_continuation_model_response(
                config,
                client,
                journal,
                tool_outputs,
            )
            _inject_fault(fault_injector, "after_model_response")
            if response.function_calls:
                _emit_model_finished(
                    ui_emitter,
                    response,
                    task_mode=config.task_mode,
                )
            continue

        if phase == "finalizing":
            events = journal.store.load(journal.session_id)
            response = _last_model_response(events)
            _emit_model_finished(
                ui_emitter,
                response,
                task_mode=config.task_mode,
            )
            report = _build_final_report(
                response,
                verification_state,
                task_mode=config.task_mode,
                review=journal.state.review,
                explain_evidence=(
                    _explanation_read_evidence_from_events(events)
                    if config.task_mode == "explain"
                    else ()
                ),
                session_id=journal.session_id,
            )
            _complete_session(journal, report)
            _emit_run_finished(ui_emitter, report)
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
    sandbox_mode = config.get("sandbox_mode", "none")
    if sandbox_mode not in {"none", "auto", "docker"}:
        raise SessionResumeError(
            f"Unsupported persisted sandbox mode: {sandbox_mode!r}."
        )
    sandbox_image = config.get("sandbox_image", "python:3.12-slim")
    if not isinstance(sandbox_image, str) or not sandbox_image:
        raise SessionResumeError(
            "Persisted session config 'sandbox_image' must be a non-empty string."
        )
    sandbox_image_digest = config.get("sandbox_image_digest")
    if sandbox_image_digest is not None and (
        not isinstance(sandbox_image_digest, str) or not sandbox_image_digest
    ):
        raise SessionResumeError(
            "Persisted session config 'sandbox_image_digest' must be a string or null."
        )
    full_auto = config.get("full_auto", False)
    if not isinstance(full_auto, bool):
        raise SessionResumeError(
            "Persisted session config 'full_auto' must be a boolean."
        )
    task_mode = config.get("task_mode", "run")
    if not isinstance(task_mode, str) or task_mode not in TASK_MODES:
        raise SessionResumeError(
            f"Unsupported persisted task mode: {task_mode!r}."
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
        sandbox_mode=cast(Any, sandbox_mode),
        sandbox_image=sandbox_image,
        sandbox_image_digest=sandbox_image_digest,
        full_auto=full_auto,
        task_mode=cast(TaskMode, task_mode),
    )


def _resume_run_started_ui_payload(
    workspace: Path,
    session_id: str,
    config: AgentConfig,
    state: AgentSessionState,
) -> dict[str, object]:
    items = state.plan.items
    return {
        "workspace": str(workspace),
        "session_id": session_id,
        "mode": "resume",
        "model": config.model,
        "task_mode": config.task_mode,
        "permission": config.permission_mode,
        "sandbox": config.sandbox_mode,
        "previous_phase": state.phase,
        "previous_status": state.status,
        "plan_progress": {
            "completed": sum(item.status == "completed" for item in items),
            "in_progress": sum(
                item.status == "in_progress" for item in items
            ),
            "pending": sum(item.status == "pending" for item in items),
            "total": len(items),
        },
    }


def _validate_agent_task_mode(config: AgentConfig) -> None:
    validate_task_mode_configuration(
        config.task_mode,
        permission_mode=config.permission_mode,
        auto_approve_edits=config.auto_approve_edits,
        auto_approve_commands=config.auto_approve_commands,
        full_auto=config.full_auto,
    )


def _validate_resume_security(
    events: Sequence[SessionEvent],
    started: SessionStarted,
    config: AgentConfig,
    workspace: Path,
) -> None:
    persisted_version = started.config.get("security_policy_version")
    if persisted_version is not None and (
        isinstance(persisted_version, bool)
        or persisted_version != SECURITY_POLICY_VERSION
    ):
        raise SessionSecurityDriftError(
            "Security policy version changed; refusing to resume."
        )

    digests: set[str] = set()
    used_docker = config.sandbox_mode == "docker"
    for event in events:
        if event.type == "security.policy_evaluated":
            policy = event.payload.get("policy")
            if isinstance(policy, Mapping):
                version = policy.get("policy_version")
                if version != SECURITY_POLICY_VERSION:
                    raise SessionSecurityDriftError(
                        "Persisted command policy version changed; refusing to resume."
                    )
        if not event.type.startswith("sandbox."):
            continue
        used_docker = True
        for source in (
            event.payload,
            event.payload.get("capability"),
            event.payload.get("result"),
        ):
            if isinstance(source, Mapping):
                digest = source.get("image_digest")
                if isinstance(digest, str):
                    digests.add(digest)

    if config.sandbox_image_digest is not None:
        digests.add(config.sandbox_image_digest)
    if len(digests) > 1:
        raise SessionSecurityDriftError(
            "Persisted Docker image digests conflict; refusing to resume."
        )
    if not used_docker:
        return

    capability = DockerSandboxBackend(
        image_reference=config.sandbox_image
    ).probe_capability(workspace)
    expected_digest = next(iter(digests), None)
    if (
        not capability.available
        or capability.backend != "docker"
        or capability.image_digest is None
        or (
            expected_digest is not None
            and capability.image_digest != expected_digest
        )
    ):
        raise SessionSecurityDriftError(
            "Docker backend or image digest changed; refusing to resume."
        )


def _reconcile_interrupted_sandboxes(
    journal: _SessionJournal,
    events: Sequence[SessionEvent],
    config: AgentConfig,
    plans: Sequence[ToolRecoveryPlan],
    ui_emitter: UiEmitter,
) -> tuple[ToolRecoveryPlan, ...]:
    starts = {
        event.payload.get("call_id"): event.payload
        for event in events
        if event.type == "sandbox.started"
        and isinstance(event.payload.get("call_id"), str)
    }
    if not starts:
        return tuple(plans)

    backend = DockerSandboxBackend(image_reference=config.sandbox_image)
    reconciled: list[ToolRecoveryPlan] = []
    for plan in plans:
        started = starts.get(plan.call_id)
        if started is None or plan.effect != "process":
            reconciled.append(plan)
            continue
        container_name = started.get("container_name")
        if not isinstance(container_name, str):
            raise SessionSecurityDriftError(
                f"Interrupted sandbox {plan.call_id!r} has no container name."
            )
        _found, cleaned, error = backend.reconcile_interrupted_container(
            config.workspace,
            container_name,
        )
        if not cleaned:
            journal.append(
                "sandbox.cleanup_failed",
                {
                    "call_id": plan.call_id,
                    "name": plan.name,
                    "cleanup_kind": "container",
                    "reason": error or "Interrupted container cleanup failed.",
                },
            )
            ui_emitter.emit(
                "tool.finished",
                {
                    "call_id": plan.call_id,
                    "name": plan.name,
                    "status": "failed",
                    "duration_ms": 0,
                    "backend": "docker",
                    "sandboxed": True,
                    "summary": "sandbox cleanup failed: container",
                    "output_truncated": False,
                },
            )
            raise SessionSecurityDriftError(
                error or "Interrupted Docker container could not be cleaned."
            )

        safe_auto_retry = (
            config.full_auto
            and started.get("network_mode") == "none"
            and started.get("snapshot_scope") == "temporary"
            and started.get("image_digest") == config.sandbox_image_digest
        )
        reconciled.append(
            replace(
                plan,
                disposition="safe_retry" if safe_auto_retry else plan.disposition,
                reason="sandbox_reconciled" if safe_auto_retry else plan.reason,
                approval_request=(
                    None if safe_auto_retry else plan.approval_request
                ),
            )
        )
    return tuple(reconciled)


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
    ui_emitter: UiEmitter,
    *,
    fault_injector: FaultInjector | None = None,
    approval_handlers_by_call_id: Mapping[str, ApprovalHandler] | None = None,
    recovery_summaries_by_call_id: Mapping[str, str] | None = None,
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
        started_ui_payload: dict[str, object] = {
            "call_id": call.call_id,
            "name": call.name,
        }
        if recovery_summaries_by_call_id is not None:
            recovery_summary = recovery_summaries_by_call_id.get(call.call_id)
            if recovery_summary:
                started_ui_payload["summary"] = recovery_summary
        ui_emitter.emit("tool.started", started_ui_payload)

        before_verification = verification_tool_state_to_dict(verification_state)
        before_history_count = len(verification_state.verification_history)
        call_approval_handler = approval_handler
        if approval_handlers_by_call_id is not None:
            call_approval_handler = approval_handlers_by_call_id.get(
                call.call_id,
                approval_handler,
            )

        security_ui_state = _ToolSecurityUiState()

        def record_security_event(
            event_type: str,
            payload: Mapping[str, object],
        ) -> None:
            journal.append(
                cast(SessionEventType, event_type),
                {
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments_sha256": hash_tool_arguments(call.arguments),
                    **payload,
                },
            )
            _update_security_ui_state(
                security_ui_state,
                event_type,
                payload,
            )

        result = execute_tool(
            config,
            call.name,
            call.arguments,
            state=verification_state,
            plan_state=journal.state.plan,
            review_result=journal.state.review,
            approval_handler=call_approval_handler,
            call_id=call.call_id,
            session_id=journal.session_id,
            security_event_handler=record_security_event,
        )
        _inject_fault(fault_injector, "after_tool_side_effect")

        if call.name == "update_plan" and result.ok:
            if result.data is None or result.data.get("type") != "plan_update":
                raise RuntimeError(
                    "update_plan returned no structured plan update."
                )
            plan_data = result.data.get("plan")
            if not isinstance(plan_data, Mapping):
                raise RuntimeError("update_plan returned an invalid plan payload.")
            updated_plan = plan_state_from_dict(plan_data)
            journal.append(
                "plan.updated",
                {"plan": plan_state_to_dict(updated_plan)},
            )
            ui_emitter.emit(
                "plan.updated",
                plan_state_to_dict(journal.state.plan),
            )

        submitted_review: ReviewResult | None = None
        if call.name == "submit_review" and result.ok:
            if (
                result.data is None
                or result.data.get("type") != "review_submission"
            ):
                raise RuntimeError(
                    "submit_review returned no structured review submission."
                )
            review_data = result.data.get("review")
            if not isinstance(review_data, Mapping):
                raise RuntimeError(
                    "submit_review returned an invalid review payload."
                )
            submitted_review = review_result_from_dict(review_data)

        read_evidence: tuple[ExplanationReadEvidence, ...] | None = None
        if call.name in {"read_file", "read_many_files"} and result.ok:
            try:
                read_evidence = explanation_read_evidence_from_tool_data(
                    result.data
                )
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"{call.name} returned invalid structured read evidence."
                ) from exc

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
        if submitted_review is not None:
            finished_payload["review"] = review_result_to_dict(submitted_review)
        if read_evidence is not None:
            finished_payload["read_evidence"] = (
                explanation_read_evidence_list_to_dict(read_evidence)
            )
        journal.append("tool.finished", finished_payload)
        if submitted_review is not None and journal.state.review != submitted_review:
            raise RuntimeError(
                "Persisted review state diverged from the submitted review."
            )
        ui_emitter.emit(
            "tool.finished",
            _tool_finished_ui_payload(
                call,
                result,
                security_ui_state=security_ui_state,
            ),
        )
        _inject_fault(fault_injector, "after_tool_finished")

        for verification in new_verifications:
            journal.append(
                "verification.recorded",
                {
                    "result": verification_result_to_dict(verification),
                    "verification_state": after_verification,
                },
            )
            ui_emitter.emit(
                "verification.finished",
                {
                    "command_id": verification.command_id,
                    "kind": verification.kind,
                    "status": verification.status,
                    "exit_code": verification.exit_code,
                    "duration_ms": verification.duration_ms,
                    "attempt": verification.attempt,
                    "output_truncated": verification.truncated,
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
    if data_type == "command_batch_result":
        commands = data.get("commands")
        if not isinstance(commands, list):
            return None
        audits = [
            audit
            for command in commands
            if isinstance(command, Mapping)
            and (audit := _execution_audit(command)) is not None
        ]
        return {"commands": audits}

    policy_keys = (
        "policy_version",
        "rule_id",
        "disposition",
        "reasons",
        "normalized_executable",
        "requires_approval",
        "requires_sandbox",
    )
    if data_type == "secure_command_result":
        keys = (
            "command_id",
            "kind",
            "argv",
            "cwd",
            "shell",
            "timeout_ms",
            "backend",
            "sandboxed",
            "network_mode",
            "image_digest",
            "exit_code",
            "timed_out",
            "duration_ms",
            "status",
            "verification_status",
            "output_truncated",
            "omitted_lines",
            "omitted_bytes",
            "error_reason",
            "snapshot",
            "snapshot_cleanup_succeeded",
            "snapshot_cleanup_error",
            "container_cleanup_attempted",
            "container_cleanup_succeeded",
            "container_cleanup_error",
            *policy_keys,
        )
    elif data_type == "command_result":
        keys = (
            "argv",
            "cwd",
            "shell",
            "timeout_ms",
            "exit_code",
            "timed_out",
            "duration_ms",
            "status",
            *policy_keys,
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
            *policy_keys,
        )
    elif data_type == "task_mode_policy_rejection":
        keys = (
            "task_mode",
            "tool_name",
            "status",
            "disposition",
            "requires_approval",
        )
    else:
        return None
    return {key: data[key] for key in keys if key in data}


def _tool_finished_ui_payload(
    call: ModelFunctionCall,
    result: ToolResult,
    *,
    security_ui_state: _ToolSecurityUiState | None = None,
) -> dict[str, object]:
    data = result.data or {}
    state = security_ui_state or _ToolSecurityUiState()
    _update_security_ui_state_from_result(state, data)

    duration = data.get("duration_ms")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or duration < 0
    ):
        duration = 0

    safe_output = _redact_security_console_text(
        result.output,
        state.sensitive_identifiers,
    )
    summary_parts = _security_ui_summary_lines(state)
    if safe_output:
        summary_parts.append(safe_output)
    combined_summary = "\n".join(summary_parts)
    summary = truncate_for_console(combined_summary)

    payload: dict[str, object] = {
        "call_id": call.call_id,
        "name": call.name,
        "status": "passed" if result.ok else "failed",
        "duration_ms": duration,
        "output_truncated": (
            summary != combined_summary or data.get("output_truncated") is True
        ),
    }
    backend = data.get("backend")
    if not isinstance(backend, str) or not backend:
        backend = state.backend
    if isinstance(backend, str) and backend:
        payload["backend"] = backend
    sandboxed = data.get("sandboxed")
    if not isinstance(sandboxed, bool):
        sandboxed = state.sandboxed
    if isinstance(sandboxed, bool):
        payload["sandboxed"] = sandboxed
    if summary:
        payload["summary"] = summary
    return payload


def _update_security_ui_state(
    state: _ToolSecurityUiState,
    event_type: str,
    payload: Mapping[str, object],
) -> None:
    if event_type == "sandbox.capability_checked":
        capability = payload.get("capability")
        if isinstance(capability, Mapping):
            available = capability.get("available")
            state.capability_status = (
                "available" if available is True else "unavailable"
            )
            _update_security_ui_state_from_mapping(state, capability)
            _remember_sensitive_security_value(state, capability.get("reason"))
            if not isinstance(capability.get("image_digest"), str):
                state.image_digest_status = "unavailable"
        return
    if event_type == "sandbox.started":
        state.sandboxed = True
        _update_security_ui_state_from_mapping(state, payload)
        return
    if event_type == "sandbox.finished":
        result = payload.get("result")
        if isinstance(result, Mapping):
            _update_security_ui_state_from_mapping(state, result)
        return
    if event_type == "sandbox.cleanup_failed":
        cleanup_kind = payload.get("cleanup_kind")
        state.cleanup_failures.add(
            cleanup_kind if isinstance(cleanup_kind, str) and cleanup_kind else "sandbox"
        )
        _remember_sensitive_security_value(state, payload.get("reason"))


def _update_security_ui_state_from_result(
    state: _ToolSecurityUiState,
    data: Mapping[str, object],
) -> None:
    _update_security_ui_state_from_mapping(state, data)
    for cleanup_kind, key in (
        ("container", "container_cleanup_succeeded"),
        ("snapshot", "snapshot_cleanup_succeeded"),
    ):
        if data.get(key) is False:
            state.cleanup_failures.add(cleanup_kind)


def _update_security_ui_state_from_mapping(
    state: _ToolSecurityUiState,
    data: Mapping[str, object],
) -> None:
    backend = data.get("backend")
    if isinstance(backend, str) and backend:
        state.backend = backend
    sandboxed = data.get("sandboxed")
    if isinstance(sandboxed, bool):
        state.sandboxed = sandboxed

    image_digest = data.get("image_digest")
    if isinstance(image_digest, str) and image_digest:
        state.image_digest_status = "verified"
        state.sensitive_identifiers.add(image_digest)
    container_name = data.get("container_name")
    if isinstance(container_name, str) and container_name:
        state.sensitive_identifiers.add(container_name)
    for key in (
        "reason",
        "error_reason",
        "snapshot_cleanup_error",
        "container_cleanup_error",
    ):
        _remember_sensitive_security_value(state, data.get(key))


def _remember_sensitive_security_value(
    state: _ToolSecurityUiState,
    value: object,
) -> None:
    if isinstance(value, str) and value:
        state.sensitive_identifiers.add(value)


def _security_ui_summary_lines(state: _ToolSecurityUiState) -> list[str]:
    summary: list[str] = []
    if state.capability_status is not None:
        summary.append(f"sandbox capability {state.capability_status}")
    if state.image_digest_status is not None:
        summary.append(f"sandbox image digest {state.image_digest_status}")
    summary.extend(
        f"sandbox cleanup failed: {cleanup_kind}"
        for cleanup_kind in sorted(state.cleanup_failures)
    )
    return summary


def _redact_security_console_text(
    value: str,
    sensitive_identifiers: set[str],
) -> str:
    redacted = re.sub(
        r"sha256:[0-9a-fA-F]{64}",
        "[image digest redacted]",
        value,
    )
    for identifier in sorted(sensitive_identifiers, key=len, reverse=True):
        replacement = (
            "[image digest redacted]"
            if identifier.startswith("sha256:")
            else "[sandbox detail redacted]"
        )
        redacted = redacted.replace(identifier, replacement)
    return redacted


def _emit_model_finished(
    ui_emitter: UiEmitter,
    response: NormalizedModelResponse,
    *,
    task_mode: TaskMode,
) -> None:
    ui_emitter.emit(
        "model.finished",
        {
            "response_id": response.response_id,
            "text": response.text,
            "reasoning_summary": (
                "" if task_mode == "explain" else response.reasoning_summary
            ),
            "has_tool_calls": bool(response.function_calls),
        },
    )


def _emit_run_finished(
    ui_emitter: UiEmitter,
    report: AgentRunReport,
) -> None:
    ui_emitter.emit(
        "run.finished",
        {
            "status": "completed",
            "final_status": report.final_status,
            "session_id": report.session_id,
            "answer": report.answer,
            "review": (
                review_result_to_dict(report.review)
                if report.review is not None
                else None
            ),
        },
    )



def _explanation_read_evidence_from_events(
    events: Sequence[SessionEvent],
) -> tuple[ExplanationReadEvidence, ...]:
    collected: list[ExplanationReadEvidence] = []
    for event in events:
        if event.type != "tool.finished":
            continue
        payload = event.payload.get("read_evidence")
        if payload is None:
            continue
        try:
            collected.extend(
                explanation_read_evidence_list_from_dict(payload)
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Persisted explain read evidence is invalid at "
                f"session event {event.seq}."
            ) from exc
    return merge_explanation_read_evidence(collected)


def _build_final_report(
    response: NormalizedModelResponse,
    verification_state: VerificationToolState,
    *,
    task_mode: TaskMode,
    review: ReviewResult | None,
    explain_evidence: Sequence[ExplanationReadEvidence],
    session_id: str,
) -> AgentRunReport:
    if task_mode == "review" and review is None:
        raise RuntimeError(
            "review mode requires one successful submit_review call before "
            "the final model response."
        )
    if task_mode == "explain":
        validate_explanation_answer(response.text, explain_evidence)
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
        review=review,
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
        "review": (
            review_result_to_dict(report.review)
            if report.review is not None
            else None
        ),
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
    ui_emitter: UiEmitter | None = None,
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
        return

    if ui_emitter is None:
        return
    ui_payload = _terminal_ui_payload(payload)
    try:
        ui_emitter.emit(
            "run.interrupted"
            if event_type == "session.interrupted"
            else "run.failed",
            ui_payload,
        )
    except BaseException as ui_error:
        original_error.add_note(
            "Additionally, the terminal UI event could not be emitted: "
            f"{type(ui_error).__name__}: {ui_error}"
        )


def _terminal_ui_payload(payload: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in (
        "reason",
        "phase",
        "turn_index",
        "max_turns",
        "resumed",
        "model_request_may_have_succeeded",
    ):
        value = payload.get(key)
        if isinstance(value, (str, int, bool)) and not (
            key == "turn_index" and isinstance(value, bool)
        ):
            result[key] = value
    if "reason" not in result:
        result["reason"] = "interrupted"
    return result


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


def _inject_fault(
    injector: FaultInjector | None,
    point: FaultPoint,
) -> None:
    if injector is not None:
        injector(point)

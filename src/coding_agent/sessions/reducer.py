from __future__ import annotations

import json
from dataclasses import replace
from typing import Mapping, cast

from ..approvals import validate_approval_decision
from ..plans import EMPTY_PLAN, validate_plan_transition
from ..reviews import ReviewResult, review_result_from_dict
from ..security.models import (
    CommandPolicyDecision,
    SandboxCapability,
    SecureExecutionResult,
)
from ..tool_outputs import thaw_json
from ..tool_policy import (
    TOOL_EFFECTS,
    TOOL_POLICIES,
    ToolEffect,
    get_tool_policy,
    hash_tool_arguments,
)
from ..tools import VerificationToolState
from .codec import (
    approval_decision_from_dict,
    approval_request_from_dict,
    checkpoint_from_dict,
    normalized_model_response_from_dict,
    pending_tool_call_from_dict,
    plan_state_from_dict,
    session_started_from_dict,
    verification_result_from_dict,
    verification_tool_state_from_dict,
    verification_tool_state_to_dict,
)
from .models import (
    AgentSessionCheckpoint,
    AgentSessionState,
    ApprovalDecision,
    JsonObject,
    JsonValue,
    ModelFunctionCall,
    NormalizedModelResponse,
    PendingToolCall,
    SessionEvent,
)

class SessionReductionError(ValueError):
    """Raised when an event cannot legally follow the current session state."""


def reduce_event(
    state: AgentSessionState | None,
    event: SessionEvent,
) -> AgentSessionState:
    """Apply one event without performing I/O or mutating either input value."""

    try:
        if state is None:
            return _start_session(event)

        _validate_event_position(state, event)
        if state.status in {"completed", "failed", "interrupted"}:
            if event.type != "session.resumed" or state.status == "completed":
                raise SessionReductionError(
                    f"event {event.type!r} cannot follow terminal status "
                    f"{state.status!r}."
                )

        handlers = {
            "session.resumed": _resume_session,
            "session.completed": _complete_session,
            "session.failed": _fail_session,
            "session.interrupted": _interrupt_session,
            "context.created": _create_context,
            "model.requested": _request_model,
            "model.responded": _record_model_response,
            "tool.started": _start_tool,
            "tool.finished": _finish_tool,
            "tool.recovered": _recover_tool,
            "approval.decided": _record_approval,
            "verification.recorded": _record_verification,
            "plan.updated": _update_plan,
            "security.policy_evaluated": _record_security_event,
            "sandbox.capability_checked": _record_security_event,
            "sandbox.snapshot_created": _record_security_event,
            "sandbox.started": _record_security_event,
            "sandbox.finished": _record_security_event,
            "sandbox.cleanup_failed": _record_security_event,
            "checkpoint.saved": _validate_checkpoint,
        }
        if event.type == "session.started":
            raise SessionReductionError("session.started may only be the first event.")
        handler = handlers.get(event.type)
        if handler is None:
            raise SessionReductionError(f"unsupported reducer event: {event.type}")
        return handler(state, event)
    except SessionReductionError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SessionReductionError(
            f"invalid {event.type} payload at sequence {event.seq}: {exc}"
        ) from exc


def rebuild_state(events: tuple[SessionEvent, ...]) -> AgentSessionState:
    """Deterministically rebuild one session from its complete ordered event log."""

    if not events:
        raise SessionReductionError("cannot rebuild a session from an empty event log.")

    state: AgentSessionState | None = None
    for event in events:
        state = reduce_event(state, event)
    if state is None:  # pragma: no cover - guarded by the non-empty check above.
        raise SessionReductionError("session event reduction produced no state.")
    return state


def _start_session(event: SessionEvent) -> AgentSessionState:
    if event.type != "session.started":
        raise SessionReductionError("the first event must be session.started.")
    if event.seq != 1:
        raise SessionReductionError("session.started must have sequence number 1.")
    if event.prev_hash is not None:
        raise SessionReductionError("session.started cannot have a previous hash.")

    started = session_started_from_dict(
        _domain_payload(event.payload, "session", "started")
    )
    max_fix_attempts = started.config.get("max_fix_attempts", 3)
    verification = VerificationToolState(
        task=started.task,
        max_fix_attempts=_integer(max_fix_attempts, "config.max_fix_attempts"),
    )
    return AgentSessionState(
        session_id=event.session_id,
        task=started.task,
        phase="awaiting_initial_model",
        turn_index=0,
        previous_response_id=None,
        pending_tool_calls=(),
        pending_tool_outputs=(),
        completed_call_ids=frozenset(),
        verification_state=verification_tool_state_to_dict(verification),
        touched_file_hashes=started.workspace_guard.touched_file_hashes,
        plan=EMPTY_PLAN,
        review=None,
        status="running",
        approvals=(),
        context_created=False,
        model_request_pending=False,
        last_seq=event.seq,
        last_event_hash=event.event_hash,
        last_event_type=event.type,
    )


def _validate_event_position(state: AgentSessionState, event: SessionEvent) -> None:
    if event.session_id != state.session_id:
        raise SessionReductionError(
            "cannot reduce events from different session IDs together."
        )
    if state.last_seq == 0:
        return
    expected_seq = state.last_seq + 1
    if event.seq != expected_seq:
        raise SessionReductionError(
            f"expected event sequence {expected_seq}, received {event.seq}."
        )
    if event.prev_hash != state.last_event_hash:
        raise SessionReductionError(
            f"event {event.seq} does not reference the previous event hash."
        )


def _resume_session(
    state: AgentSessionState,
    event: SessionEvent,
) -> AgentSessionState:
    retry_pending = event.payload.get("retry_pending_model_request", False)
    if not isinstance(retry_pending, bool):
        raise SessionReductionError(
            "session.resumed retry_pending_model_request must be a boolean."
        )
    if retry_pending and not state.model_request_pending:
        raise SessionReductionError(
            "session.resumed cannot retry a model request that is not pending."
        )
    return _advance(
        state,
        event,
        status="running",
        model_request_pending=(
            False if retry_pending else state.model_request_pending
        ),
    )


def _create_context(
    state: AgentSessionState,
    event: SessionEvent,
) -> AgentSessionState:
    _require_running(state, event)
    if state.phase != "awaiting_initial_model":
        raise SessionReductionError(
            "context.created is only valid before the initial model response."
        )
    if state.context_created:
        raise SessionReductionError("context.created cannot be recorded twice.")
    if state.model_request_pending:
        raise SessionReductionError(
            "context.created cannot follow an in-flight model request."
        )
    return _advance(state, event, context_created=True)


def _request_model(
    state: AgentSessionState,
    event: SessionEvent,
) -> AgentSessionState:
    _require_running(state, event)
    if state.phase not in {"awaiting_initial_model", "awaiting_model"}:
        raise SessionReductionError(
            f"model.requested is invalid while phase is {state.phase!r}."
        )
    if state.model_request_pending:
        raise SessionReductionError("a model request is already in flight.")
    if state.pending_tool_calls:
        raise SessionReductionError(
            "model.requested requires all pending tool calls to be completed."
        )
    if state.phase == "awaiting_initial_model" and not state.context_created:
        raise SessionReductionError(
            "the initial model request requires context.created first."
        )

    expected_response_id = (
        None if state.phase == "awaiting_initial_model" else state.previous_response_id
    )
    if "previous_response_id" in event.payload:
        actual_response_id = event.payload["previous_response_id"]
        if actual_response_id != expected_response_id:
            raise SessionReductionError(
                "model.requested previous_response_id does not match state."
            )
    if "turn_index" in event.payload:
        requested_turn = _integer(event.payload["turn_index"], "turn_index")
        if requested_turn != state.turn_index + 1:
            raise SessionReductionError(
                "model.requested turn_index must identify the next model turn."
            )
    return _advance(state, event, model_request_pending=True)


def _record_model_response(
    state: AgentSessionState,
    event: SessionEvent,
) -> AgentSessionState:
    _require_running(state, event)
    if not state.model_request_pending:
        raise SessionReductionError(
            "model.responded requires a preceding model.requested event."
        )
    if state.phase not in {"awaiting_initial_model", "awaiting_model"}:
        raise SessionReductionError(
            f"model.responded is invalid while phase is {state.phase!r}."
        )

    response, pending_calls = _parse_model_response(event.payload)
    if (
        state.previous_response_id is not None
        and response.response_id == state.previous_response_id
    ):
        raise SessionReductionError("model response IDs must advance between turns.")

    call_ids = {call.call_id for call in pending_calls}
    reused = sorted(call_ids & state.completed_call_ids)
    if reused:
        raise SessionReductionError(
            "model response reused completed call IDs: " + ", ".join(reused)
        )

    phase = "awaiting_tools" if pending_calls else "finalizing"
    return _advance(
        state,
        event,
        phase=phase,
        turn_index=state.turn_index + 1,
        previous_response_id=response.response_id,
        pending_tool_calls=pending_calls,
        pending_tool_outputs=(),
        model_request_pending=False,
    )


def _start_tool(
    state: AgentSessionState,
    event: SessionEvent,
) -> AgentSessionState:
    _require_running(state, event)
    if state.phase != "awaiting_tools":
        raise SessionReductionError(
            f"tool.started is invalid while phase is {state.phase!r}."
        )

    data = _tool_event_payload(event.payload)
    call_id = _required_string(data, "call_id")
    index, call = _pending_call(state, call_id)
    if call.started:
        raise SessionReductionError(f"tool call {call_id!r} was already started.")
    _validate_tool_identity(call, data)
    _validate_tool_start_metadata(call, data)

    calls = list(state.pending_tool_calls)
    calls[index] = replace(call, started=True)
    return _advance(state, event, pending_tool_calls=tuple(calls))


def _finish_tool(
    state: AgentSessionState,
    event: SessionEvent,
) -> AgentSessionState:
    return _complete_tool_call(state, event, recovered=False)


def _recover_tool(
    state: AgentSessionState,
    event: SessionEvent,
) -> AgentSessionState:
    return _complete_tool_call(state, event, recovered=True)


def _complete_tool_call(
    state: AgentSessionState,
    event: SessionEvent,
    *,
    recovered: bool,
) -> AgentSessionState:
    _require_running(state, event)
    if state.phase != "awaiting_tools":
        raise SessionReductionError(
            f"{event.type} is invalid while phase is {state.phase!r}."
        )

    data = _tool_event_payload(event.payload)
    call_id = _required_string(data, "call_id")
    index, call = _pending_call(state, call_id)
    if not call.started:
        raise SessionReductionError(
            f"tool call {call_id!r} cannot finish before tool.started."
        )
    if call_id in state.completed_call_ids:
        raise SessionReductionError(f"tool call {call_id!r} is already complete.")
    _validate_tool_identity(call, data)

    if recovered and data.get("completed") is False:
        reason = _required_string(data, "reason")
        requires_reapproval = data.get("requires_reapproval", False)
        if not isinstance(requires_reapproval, bool):
            raise SessionReductionError(
                "tool.recovered requires_reapproval must be a boolean."
            )
        if call.effect in {"read_only", "session_only"}:
            if reason != "safe_retry" or requires_reapproval:
                raise SessionReductionError(
                    "read-only and session-only recovery must use safe_retry "
                    "without approval."
                )
        elif call.effect == "process" and reason == "sandbox_reconciled":
            if requires_reapproval:
                raise SessionReductionError(
                    "reconciled sandbox retry must not require approval."
                )
        elif not requires_reapproval:
            raise SessionReductionError(
                "side-effecting recovery retries require explicit reapproval."
            )
        calls = list(state.pending_tool_calls)
        calls[index] = replace(call, started=False)
        approvals = (
            tuple(
                decision
                for decision in state.approvals
                if decision.call_id != call_id
            )
            if call.effect == "process" and reason == "sandbox_reconciled"
            else state.approvals
        )
        return _advance(
            state,
            event,
            pending_tool_calls=tuple(calls),
            approvals=approvals,
        )

    policy = get_tool_policy(call.name)
    call_approvals = tuple(
        decision
        for decision in state.approvals
        if decision.call_id == call_id
    )
    if (
        policy.approval_required
        and not call_approvals
        and not _is_preflight_security_rejection(data)
    ):
        raise SessionReductionError(
            f"tool call {call_id!r} requires approval.decided before completion."
        )
    recovery_retry = data.get("recovery_retry", False)
    if not isinstance(recovery_retry, bool):
        raise SessionReductionError("recovery_retry must be a boolean.")
    if recovery_retry and not any(
        decision.source == "resume_recovery" for decision in call_approvals
    ):
        raise SessionReductionError(
            f"tool call {call_id!r} recovery retry requires a "
            "resume_recovery approval decision."
        )

    tool_output = _extract_tool_output(data, call_id)
    review = _review_from_tool_completion(
        data,
        call,
        tool_output,
        state.review,
    )
    remaining = tuple(
        pending
        for pending in state.pending_tool_calls
        if pending.call_id != call_id
    )
    completed = frozenset((*state.completed_call_ids, call_id))
    verification_state = _verification_state_from_payload(
        data,
        state.verification_state,
    )
    touched_file_hashes = _touched_hashes_from_payload(
        data,
        state.touched_file_hashes,
    )
    phase = "awaiting_model" if not remaining else "awaiting_tools"
    return _advance(
        state,
        event,
        phase=phase,
        pending_tool_calls=remaining,
        pending_tool_outputs=(*state.pending_tool_outputs, tool_output),
        completed_call_ids=completed,
        verification_state=verification_state,
        touched_file_hashes=touched_file_hashes,
        review=review,
    )


def _is_preflight_security_rejection(data: Mapping[str, JsonValue]) -> bool:
    execution = data.get("execution")
    if not isinstance(execution, Mapping):
        return False
    return (
        execution.get("status") in {"denied", "sandbox_unavailable"}
        and execution.get("disposition") in {"deny", "sandbox_required"}
        and execution.get("requires_approval") is False
    )


def _record_approval(
    state: AgentSessionState,
    event: SessionEvent,
) -> AgentSessionState:
    _require_running(state, event)
    if state.phase != "awaiting_tools":
        raise SessionReductionError(
            f"approval.decided is invalid while phase is {state.phase!r}."
        )
    decision = approval_decision_from_dict(
        _domain_payload(event.payload, "decision", "approval")
    )
    request = None
    if "request" in event.payload:
        request = approval_request_from_dict(
            _mapping(event.payload["request"], "approval request")
        )
        validate_approval_decision(request, decision)

    _, call = _pending_call(state, decision.call_id)
    if not call.started:
        raise SessionReductionError(
            "approval.decided requires the corresponding tool.started event."
        )
    policy = get_tool_policy(call.name)
    if not policy.approval_required:
        raise SessionReductionError(
            f"tool call {call.call_id!r} does not require approval."
        )
    if decision.action != call.name:
        raise SessionReductionError(
            "approval action does not match the pending tool name."
        )
    expected_arguments_sha256 = hash_tool_arguments(call.arguments)
    if decision.arguments_sha256 != expected_arguments_sha256:
        raise SessionReductionError(
            "approval arguments_sha256 does not match pending tool arguments."
        )
    existing_for_call = tuple(
        existing
        for existing in state.approvals
        if existing.call_id == decision.call_id
    )
    if existing_for_call:
        recovery_decisions = tuple(
            existing
            for existing in existing_for_call
            if existing.source == "resume_recovery"
        )
        if decision.source != "resume_recovery" or recovery_decisions:
            raise SessionReductionError(
                f"tool call {decision.call_id!r} already has an approval decision."
            )
    if any(
        existing.approval_id == decision.approval_id
        for existing in state.approvals
    ):
        raise SessionReductionError(
            f"approval ID {decision.approval_id!r} was already recorded."
        )
    return _advance(state, event, approvals=(*state.approvals, decision))


def _record_verification(
    state: AgentSessionState,
    event: SessionEvent,
) -> AgentSessionState:
    _require_running(state, event)
    if state.phase not in {"awaiting_tools", "awaiting_model"}:
        raise SessionReductionError(
            f"verification.recorded is invalid while phase is {state.phase!r}."
        )

    full_state = _find_mapping(event.payload, "verification_state", "state")
    if full_state is None and {
        "max_fix_attempts",
        "verification_history",
        "edit_generation",
    }.issubset(event.payload):
        full_state = event.payload
    if full_state is not None:
        verification_state = _normalize_verification_state(full_state)
    else:
        result_data = _domain_payload(event.payload, "result", "verification")
        result_keys = {
            "command_id",
            "kind",
            "status",
            "argv",
            "cwd",
            "exit_code",
            "duration_ms",
            "output",
            "truncated",
            "omitted_lines",
            "omitted_bytes",
            "attempt",
        }
        normalized_result = {
            key: result_data[key]
            for key in result_keys
            if key in result_data
        }
        result = verification_result_from_dict(normalized_result)
        mutable_state = verification_tool_state_from_dict(state.verification_state)
        mutable_state.record_verification(result)
        verification_state = verification_tool_state_to_dict(mutable_state)
    return _advance(state, event, verification_state=verification_state)


def _update_plan(
    state: AgentSessionState,
    event: SessionEvent,
) -> AgentSessionState:
    _require_running(state, event)
    plan = plan_state_from_dict(_domain_payload(event.payload, "plan"))
    if not plan.items:
        raise SessionReductionError("plan.updated requires at least one plan item.")
    validate_plan_transition(state.plan, plan)
    return _advance(state, event, plan=plan)


def _record_security_event(
    state: AgentSessionState,
    event: SessionEvent,
) -> AgentSessionState:
    _require_running(state, event)
    if state.phase != "awaiting_tools":
        raise SessionReductionError(
            f"{event.type} is invalid while phase is {state.phase!r}."
        )
    call_id = _required_string(event.payload, "call_id")
    _, call = _pending_call(state, call_id)
    if not call.started:
        raise SessionReductionError(
            f"{event.type} requires the corresponding tool.started event."
        )

    if event.type == "security.policy_evaluated":
        CommandPolicyDecision.from_dict(
            cast(Mapping[str, object], thaw_json(event.payload["policy"]))
        )
    elif event.type == "sandbox.capability_checked":
        SandboxCapability.from_dict(
            _mapping(event.payload["capability"], "capability")
        )
    elif event.type == "sandbox.snapshot_created":
        snapshot = _mapping(event.payload["snapshot"], "snapshot")
        _required_string(snapshot, "manifest_sha256")
        _integer(snapshot.get("file_count"), "snapshot.file_count")
        _integer(snapshot.get("total_bytes"), "snapshot.total_bytes")
    elif event.type == "sandbox.started":
        if event.payload.get("backend") != "docker":
            raise SessionReductionError("sandbox.started backend must be docker.")
        if event.payload.get("network_mode") != "none":
            raise SessionReductionError("sandbox.started network_mode must be none.")
        _required_string(event.payload, "container_name")
        _required_string(event.payload, "image_digest")
    elif event.type == "sandbox.finished":
        SecureExecutionResult.from_dict(
            cast(Mapping[str, object], thaw_json(event.payload["result"]))
        )
    elif event.type == "sandbox.cleanup_failed":
        _required_string(event.payload, "reason")
    return _advance(state, event)


def _validate_checkpoint(
    state: AgentSessionState,
    event: SessionEvent,
) -> AgentSessionState:
    _require_running(state, event)
    checkpoint = checkpoint_from_dict(
        _domain_payload(event.payload, "checkpoint")
    )
    rebuilt = state.to_checkpoint()
    if checkpoint != rebuilt:
        differences = _checkpoint_differences(rebuilt, checkpoint)
        raise SessionReductionError(
            "checkpoint does not match state rebuilt from events"
            + (f": {', '.join(differences)}" if differences else ".")
        )
    return _advance(state, event)


def _complete_session(
    state: AgentSessionState,
    event: SessionEvent,
) -> AgentSessionState:
    _require_running(state, event)
    if state.phase != "finalizing":
        raise SessionReductionError(
            "session.completed requires a final model response with no tool calls."
        )
    if state.pending_tool_calls or state.model_request_pending:
        raise SessionReductionError(
            "session.completed cannot have pending model or tool work."
        )
    return _advance(
        state,
        event,
        phase="completed",
        status="completed",
    )


def _fail_session(
    state: AgentSessionState,
    event: SessionEvent,
) -> AgentSessionState:
    _require_running(state, event)
    return _advance(state, event, status="failed")


def _interrupt_session(
    state: AgentSessionState,
    event: SessionEvent,
) -> AgentSessionState:
    _require_running(state, event)
    return _advance(state, event, status="interrupted")


def _parse_model_response(
    payload: Mapping[str, JsonValue],
) -> tuple[NormalizedModelResponse, tuple[PendingToolCall, ...]]:
    response_data = _domain_payload(payload, "response")
    response_keys = {"response_id", "text", "reasoning_summary"}
    normalized_data = {
        key: response_data[key]
        for key in response_keys
        if key in response_data
    }
    for non_control_field in ("text", "reasoning_summary"):
        value = normalized_data.get(non_control_field)
        if isinstance(value, Mapping) and _is_artifact_descriptor(value):
            normalized_data[non_control_field] = ""
    raw_function_calls = _sequence(
        response_data.get("function_calls"),
        "function_calls",
    )
    normalized_function_calls: list[dict[str, object]] = []
    inline_effects: dict[str, object] = {}
    for item in raw_function_calls:
        raw_call = _mapping(item, "function_calls item")
        normalized_function_calls.append(
            {
                key: raw_call[key]
                for key in ("call_id", "name", "arguments")
                if key in raw_call
            }
        )
        call_id = raw_call.get("call_id")
        if isinstance(call_id, str) and "effect" in raw_call:
            inline_effects[call_id] = raw_call["effect"]
    normalized_data["function_calls"] = normalized_function_calls
    response = normalized_model_response_from_dict(normalized_data)

    pending_source: object | None = payload.get("pending_tool_calls")
    if pending_source is None:
        pending_source = response_data.get("pending_tool_calls")
    if pending_source is not None:
        raw_calls = _sequence(pending_source, "pending_tool_calls")
        pending_calls = tuple(
            pending_tool_call_from_dict(
                _mapping(item, "pending_tool_calls item")
            )
            for item in raw_calls
        )
        _validate_pending_calls_match_response(pending_calls, response.function_calls)
        return response, pending_calls

    effect_source: object | None = payload.get("tool_effects")
    if effect_source is None:
        effect_source = response_data.get("tool_effects")
    effect_by_call: dict[str, object] = dict(inline_effects)
    if effect_source is not None:
        effect_by_call.update(_mapping(effect_source, "tool_effects"))

    pending_calls = tuple(
        PendingToolCall(
            call_id=call.call_id,
            name=call.name,
            arguments=call.arguments,
            effect=_tool_effect(call, effect_by_call),
            started=False,
        )
        for call in response.function_calls
    )
    return response, pending_calls


def _tool_effect(
    call: ModelFunctionCall,
    effect_by_call: Mapping[str, object],
) -> ToolEffect:
    policy = get_tool_policy(call.name)
    raw_effect = effect_by_call.get(call.call_id, effect_by_call.get(call.name))
    if call.name not in TOOL_POLICIES and raw_effect is not None:
        if not isinstance(raw_effect, str) or raw_effect not in TOOL_EFFECTS:
            raise ValueError(
                f"invalid tool effect for call {call.call_id!r}: {raw_effect!r}"
            )
        return cast(ToolEffect, raw_effect)
    if raw_effect is not None and raw_effect != policy.effect:
        raise ValueError(
            f"tool effect for call {call.call_id!r} does not match the policy registry."
        )
    return policy.effect


def _validate_tool_start_metadata(
    call: PendingToolCall,
    data: Mapping[str, JsonValue],
) -> None:
    arguments_sha256 = data.get("arguments_sha256")
    if arguments_sha256 is not None:
        if arguments_sha256 != hash_tool_arguments(call.arguments):
            raise SessionReductionError(
                "tool.started arguments_sha256 does not match pending arguments."
            )
    requires_approval = data.get("requires_approval")
    if requires_approval is not None:
        if not isinstance(requires_approval, bool):
            raise SessionReductionError(
                "tool.started requires_approval must be a boolean."
            )
        if requires_approval != get_tool_policy(call.name).approval_required:
            raise SessionReductionError(
                "tool.started requires_approval does not match policy."
            )


def _validate_pending_calls_match_response(
    pending_calls: tuple[PendingToolCall, ...],
    response_calls: tuple[ModelFunctionCall, ...],
) -> None:
    if len(pending_calls) != len(response_calls):
        raise SessionReductionError(
            "pending_tool_calls must match model response function_calls."
        )
    for pending, response in zip(pending_calls, response_calls, strict=True):
        if pending.started:
            raise SessionReductionError(
                "new pending tool calls cannot already be marked started."
            )
        if (
            pending.call_id,
            pending.name,
            pending.arguments,
        ) != (
            response.call_id,
            response.name,
            response.arguments,
        ):
            raise SessionReductionError(
                "pending tool call identity differs from the model response."
            )


def _pending_call(
    state: AgentSessionState,
    call_id: str,
) -> tuple[int, PendingToolCall]:
    for index, call in enumerate(state.pending_tool_calls):
        if call.call_id == call_id:
            return index, call
    if call_id in state.completed_call_ids:
        raise SessionReductionError(f"tool call {call_id!r} is already complete.")
    raise SessionReductionError(f"unknown tool call ID: {call_id!r}.")


def _validate_tool_identity(
    call: PendingToolCall,
    data: Mapping[str, JsonValue],
) -> None:
    expected: tuple[tuple[str, object], ...] = (
        ("name", call.name),
        ("arguments", call.arguments),
        ("effect", call.effect),
    )
    for key, value in expected:
        if key in data and data[key] != value:
            raise SessionReductionError(
                f"tool event {key} does not match pending call {call.call_id!r}."
            )


def _tool_event_payload(
    payload: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue]:
    data = _domain_payload(payload, "call", "tool_call")
    if data is payload:
        return data
    merged: dict[str, JsonValue] = {
        key: value
        for key, value in payload.items()
        if key not in {"call", "tool_call"}
    }
    merged.update(data)
    return merged


def _extract_tool_output(
    data: Mapping[str, JsonValue],
    call_id: str,
) -> JsonObject:
    explicit = data.get("tool_output")
    if explicit is not None:
        output = _mapping(explicit, "tool_output")
        output_call_id = output.get("call_id")
        if output_call_id is not None and output_call_id != call_id:
            raise SessionReductionError(
                "tool_output call_id does not match the completed call."
            )
        return cast(JsonObject, output)

    if "output" in data:
        value = data["output"]
    elif "result" in data:
        value = data["result"]
    else:
        raise SessionReductionError(
            "tool completion requires output, result, or tool_output."
        )

    if isinstance(value, Mapping) and value.get("type") == "function_call_output":
        output_call_id = value.get("call_id")
        if output_call_id != call_id:
            raise SessionReductionError(
                "function_call_output call_id does not match the completed call."
            )
        return cast(JsonObject, value)
    return cast(
        JsonObject,
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": value,
        },
    )


def _review_from_tool_completion(
    data: Mapping[str, JsonValue],
    call: PendingToolCall,
    tool_output: JsonObject,
    current: ReviewResult | None,
) -> ReviewResult | None:
    review_data = _find_mapping(data, "review")
    output_ok = _tool_output_ok(tool_output)

    if call.name != "submit_review":
        if review_data is not None:
            raise SessionReductionError(
                "only submit_review tool completions may carry review state."
            )
        return current

    if review_data is None:
        if output_ok is True:
            raise SessionReductionError(
                "successful submit_review completion requires structured review state."
            )
        return current
    if output_ok is not True:
        raise SessionReductionError(
            "failed submit_review completion cannot persist review state."
        )
    if current is not None:
        raise SessionReductionError(
            "a review result has already been submitted for this session."
        )
    return review_result_from_dict(review_data)


def _tool_output_ok(tool_output: JsonObject) -> bool | None:
    raw_output = tool_output.get("output")
    if not isinstance(raw_output, str):
        return None
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    ok = payload.get("ok")
    return ok if isinstance(ok, bool) else None


def _verification_state_from_payload(
    data: Mapping[str, JsonValue],
    current: JsonObject,
) -> JsonObject:
    candidate = _find_mapping(data, "verification_state")
    if candidate is None:
        return current
    return _normalize_verification_state(candidate)


def _normalize_verification_state(data: Mapping[str, object]) -> JsonObject:
    value = verification_tool_state_from_dict(data)
    return cast(JsonObject, verification_tool_state_to_dict(value))


def _touched_hashes_from_payload(
    data: Mapping[str, JsonValue],
    current: JsonObject,
) -> JsonObject:
    candidate = _find_mapping(data, "touched_file_hashes")
    if candidate is None:
        return current
    merged = dict(current)
    merged.update(candidate)
    return cast(JsonObject, merged)


def _checkpoint_differences(
    rebuilt: AgentSessionCheckpoint,
    stored: AgentSessionCheckpoint,
) -> list[str]:
    fields = (
        "phase",
        "turn_index",
        "previous_response_id",
        "pending_tool_calls",
        "pending_tool_outputs",
        "completed_call_ids",
        "verification_state",
        "touched_file_hashes",
        "plan",
        "review",
    )
    return [name for name in fields if getattr(rebuilt, name) != getattr(stored, name)]


def _domain_payload(
    payload: Mapping[str, JsonValue],
    *wrapper_names: str,
) -> Mapping[str, JsonValue]:
    if _is_artifact_descriptor(payload):
        raise SessionReductionError(
            "control-state payload is artifact-backed and cannot be reduced offline."
        )
    for name in wrapper_names:
        if name in payload:
            value = payload[name]
            data = _mapping(value, name)
            if _is_artifact_descriptor(data):
                raise SessionReductionError(
                    f"control-state field {name!r} is artifact-backed and cannot "
                    "be reduced offline."
                )
            return cast(Mapping[str, JsonValue], data)
    return payload


def _find_mapping(
    payload: Mapping[str, JsonValue],
    *names: str,
) -> Mapping[str, object] | None:
    for name in names:
        if name in payload:
            value = payload[name]
            if value is None:
                return None
            data = _mapping(value, name)
            if _is_artifact_descriptor(data):
                raise SessionReductionError(
                    f"control-state field {name!r} is artifact-backed and cannot "
                    "be reduced offline."
                )
            return data
    return None


def _is_artifact_descriptor(value: Mapping[str, object]) -> bool:
    return "artifact" in value and "stored" in value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object.")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{label} keys must be strings.")
    return value


def _sequence(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{label} must be an array.")
    return tuple(value)


def _required_string(data: Mapping[str, JsonValue], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string.")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer.")
    return value


def _require_running(state: AgentSessionState, event: SessionEvent) -> None:
    if state.status != "running":
        raise SessionReductionError(
            f"event {event.type!r} requires a running session."
        )


def _advance(
    state: AgentSessionState,
    event: SessionEvent,
    **changes: object,
) -> AgentSessionState:
    return replace(
        state,
        **changes,
        last_seq=event.seq,
        last_event_hash=event.event_hash,
        last_event_type=event.type,
    )

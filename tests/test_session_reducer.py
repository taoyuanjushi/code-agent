from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from coding_agent.sessions.codec import (
    approval_decision_to_dict,
    approval_request_to_dict,
    checkpoint_to_dict,
    create_session_event,
    normalized_model_response_to_dict,
    pending_tool_call_to_dict,
    session_started_to_dict,
    verification_result_to_dict,
    verification_tool_state_from_dict,
    verification_tool_state_to_dict,
)
from coding_agent.sessions.models import (
    AgentSessionCheckpoint,
    AgentSessionState,
    ApprovalDecision,
    ApprovalRequest,
    ModelFunctionCall,
    NormalizedModelResponse,
    PendingToolCall,
    SessionEvent,
    SessionEventType,
    SessionStarted,
    ToolEffect,
    WorkspaceGuard,
)
from coding_agent.sessions.reducer import (
    SessionReductionError,
    rebuild_state,
    reduce_event,
)
from coding_agent.tool_policy import hash_tool_arguments
from coding_agent.tools import VerificationToolState
from coding_agent.verification import VerificationResult

SESSION_ID = "20260714T031500Z-a1b2c3d4"
OTHER_SESSION_ID = "20260714T031501Z-b1c2d3e4"
TIMESTAMP = "2026-07-14T03:15:04.125Z"
SHA_A = "a" * 64
SHA_B = "b" * 64


def test_session_started_builds_the_initial_immutable_state() -> None:
    event = _event("session.started", _started_payload(), events=())

    state = reduce_event(None, event)

    assert state.session_id == SESSION_ID
    assert state.task == "fix failing tests"
    assert state.status == "running"
    assert state.phase == "awaiting_initial_model"
    assert state.turn_index == 0
    assert state.previous_response_id is None
    assert state.pending_tool_calls == ()
    assert state.pending_tool_outputs == ()
    assert state.completed_call_ids == frozenset()
    assert state.touched_file_hashes == {}
    assert state.last_seq == 1
    verification = verification_tool_state_from_dict(state.verification_state)
    assert verification.task == state.task
    assert verification.max_fix_attempts == 2


def test_rebuild_state_is_deterministic_for_a_complete_tool_loop() -> None:
    events = _complete_session_events()

    first = rebuild_state(tuple(events))
    second = rebuild_state(tuple(events))

    assert first == second
    assert first.status == "completed"
    assert first.phase == "completed"
    assert first.turn_index == 2
    assert first.previous_response_id == "response-2"
    assert first.pending_tool_calls == ()
    assert first.pending_tool_outputs == ()
    assert first.completed_call_ids == frozenset({"call-read"})
    assert first.last_seq == len(events)


def test_reduce_event_does_not_mutate_the_input_state() -> None:
    events, state = _state_awaiting_tool()
    original_call = state.pending_tool_calls[0]
    started_event = _event(
        "tool.started",
        {"call_id": original_call.call_id},
        events=tuple(events),
    )

    next_state = reduce_event(state, started_event)

    assert state.pending_tool_calls[0].started is False
    assert next_state.pending_tool_calls[0].started is True
    assert next_state is not state


def test_checkpoint_must_equal_the_state_rebuilt_from_prior_events() -> None:
    events, state = _state_awaiting_tool()
    mismatched = replace(state.to_checkpoint(), turn_index=99)
    checkpoint_event = _event(
        "checkpoint.saved",
        checkpoint_to_dict(mismatched),
        events=tuple(events),
    )

    with pytest.raises(SessionReductionError, match="turn_index"):
        reduce_event(state, checkpoint_event)


def test_tool_cannot_finish_before_it_starts() -> None:
    events, state = _state_awaiting_tool()
    finished = _event(
        "tool.finished",
        {"call_id": "call-read", "output": {"ok": True}},
        events=tuple(events),
    )

    with pytest.raises(SessionReductionError, match="before tool.started"):
        reduce_event(state, finished)


def test_unknown_and_repeated_tool_call_ids_are_rejected() -> None:
    events, state = _state_awaiting_tool()
    unknown = _event(
        "tool.started",
        {"call_id": "call-unknown"},
        events=tuple(events),
    )
    with pytest.raises(SessionReductionError, match="unknown tool call ID"):
        reduce_event(state, unknown)

    started = _event(
        "tool.started",
        {"call_id": "call-read"},
        events=tuple(events),
    )
    state = reduce_event(state, started)
    events.append(started)
    finished = _event(
        "tool.finished",
        {"call_id": "call-read", "output": {"ok": True}},
        events=tuple(events),
    )
    state = reduce_event(state, finished)
    events.append(finished)
    duplicate = _event(
        "tool.finished",
        {"call_id": "call-read", "output": {"ok": True}},
        events=tuple(events),
    )
    with pytest.raises(SessionReductionError):
        reduce_event(state, duplicate)


def test_approval_requires_a_started_known_call_and_is_preserved() -> None:
    events, state = _state_awaiting_tool(tool_name="apply_patch")
    decision = ApprovalDecision(
        approval_id="approval-1",
        call_id="call-read",
        action="apply_patch",
        summary="Apply one reviewed patch",
        outcome="approved",
        source="interactive",
        decided_at=TIMESTAMP,
        arguments_sha256=hash_tool_arguments('{"path":"src/example.py"}'),
    )
    premature = _event(
        "approval.decided",
        approval_decision_to_dict(decision),
        events=tuple(events),
    )
    with pytest.raises(SessionReductionError, match="tool.started"):
        reduce_event(state, premature)

    started = _event(
        "tool.started",
        {"call_id": "call-read"},
        events=tuple(events),
    )
    state = reduce_event(state, started)
    events.append(started)
    approved = _event(
        "approval.decided",
        approval_decision_to_dict(decision),
        events=tuple(events),
    )

    state = reduce_event(state, approved)

    assert state.approvals == (decision,)


def test_preflight_security_rejection_finishes_without_approval() -> None:
    events, state = _state_awaiting_tool(tool_name="run_command")
    started = _event(
        "tool.started",
        {"call_id": "call-read"},
        events=tuple(events),
    )
    state = reduce_event(state, started)
    events.append(started)
    finished = _event(
        "tool.finished",
        {
            "call_id": "call-read",
            "output": {"ok": False},
            "execution": {
                "status": "denied",
                "disposition": "deny",
                "requires_approval": False,
            },
        },
        events=tuple(events),
    )

    state = reduce_event(state, finished)

    assert state.completed_call_ids == frozenset({"call-read"})
    assert state.approvals == ()


def test_approval_cannot_be_bypassed_by_an_unrelated_failed_execution() -> None:
    events, state = _state_awaiting_tool(tool_name="run_command")
    started = _event(
        "tool.started",
        {"call_id": "call-read"},
        events=tuple(events),
    )
    state = reduce_event(state, started)
    events.append(started)
    finished = _event(
        "tool.finished",
        {
            "call_id": "call-read",
            "output": {"ok": False},
            "execution": {
                "status": "failed",
                "disposition": "approval_required",
                "requires_approval": True,
            },
        },
        events=tuple(events),
    )

    with pytest.raises(SessionReductionError, match="requires approval"):
        reduce_event(state, finished)


def test_approval_rejects_wrong_action_hash_and_duplicate_call_decisions() -> None:
    events, state = _state_awaiting_tool(tool_name="apply_patch")
    started = _event(
        "tool.started",
        {"call_id": "call-read"},
        events=tuple(events),
    )
    state = reduce_event(state, started)
    events.append(started)
    arguments_sha256 = hash_tool_arguments('{"path":"src/example.py"}')
    decision = ApprovalDecision(
        approval_id="approval-1",
        call_id="call-read",
        action="apply_patch",
        summary="Apply one reviewed patch",
        outcome="approved",
        source="interactive",
        decided_at=TIMESTAMP,
        arguments_sha256=arguments_sha256,
    )

    for invalid, message in (
        (replace(decision, action="run_command"), "approval action"),
        (replace(decision, arguments_sha256=SHA_A), "arguments_sha256"),
    ):
        event = _event(
            "approval.decided",
            approval_decision_to_dict(invalid),
            events=tuple(events),
        )
        with pytest.raises(SessionReductionError, match=message):
            reduce_event(state, event)

    approved = _event(
        "approval.decided",
        approval_decision_to_dict(decision),
        events=tuple(events),
    )
    approved_state = reduce_event(state, approved)
    duplicate = _event(
        "approval.decided",
        approval_decision_to_dict(
            replace(decision, approval_id="approval-2")
        ),
        events=(*events, approved),
    )
    with pytest.raises(SessionReductionError, match="already has an approval"):
        reduce_event(approved_state, duplicate)


def test_approval_event_request_must_match_its_decision_and_pending_call() -> None:
    events, state = _state_awaiting_tool(tool_name="apply_patch")
    started = _event(
        "tool.started",
        {"call_id": "call-read"},
        events=tuple(events),
    )
    state = reduce_event(state, started)
    events.append(started)
    arguments_sha256 = hash_tool_arguments('{"path":"src/example.py"}')
    request = ApprovalRequest(
        call_id="call-read",
        action="apply_patch",
        summary="Apply one reviewed patch",
        arguments_sha256=arguments_sha256,
        details={"changed_paths": ("src/example.py",)},
    )
    decision = ApprovalDecision(
        approval_id="approval-1",
        call_id="call-read",
        action="apply_patch",
        summary=request.summary,
        outcome="approved",
        source="interactive",
        decided_at=TIMESTAMP,
        arguments_sha256=arguments_sha256,
    )
    mismatched_request = replace(request, action="run_command")
    event = _event(
        "approval.decided",
        {
            "request": approval_request_to_dict(mismatched_request),
            "decision": approval_decision_to_dict(decision),
        },
        events=tuple(events),
    )

    with pytest.raises(SessionReductionError, match="does not match its request"):
        reduce_event(state, event)


def test_verification_result_rebuilds_the_m3_verification_state() -> None:
    events, state = _state_after_tool()
    result = VerificationResult(
        command_id="python:pytest",
        kind="test",
        status="failed",
        argv=("python", "-m", "pytest"),
        cwd="D:\\\\code\\\\coding",
        exit_code=1,
        duration_ms=120,
        output="tests/test_example.py:10: AssertionError",
        truncated=False,
        omitted_lines=0,
        omitted_bytes=0,
        attempt=1,
    )
    recorded = _event(
        "verification.recorded",
        {"result": verification_result_to_dict(result)},
        events=tuple(events),
    )

    state = reduce_event(state, recorded)
    restored = verification_tool_state_from_dict(state.verification_state)

    assert restored.verification_history == [result]
    assert restored.unresolved_failure_command_id == result.command_id
    assert restored.repair_attempts == 0


def test_tool_completion_can_restore_full_verification_and_touched_hash_state() -> None:
    events, state = _state_awaiting_tool(tool_name="apply_patch")
    started = _event(
        "tool.started",
        {"call_id": "call-read"},
        events=tuple(events),
    )
    state = reduce_event(state, started)
    events.append(started)
    decision = ApprovalDecision(
        approval_id="approval-patch-state",
        call_id="call-read",
        action="apply_patch",
        summary="Apply reviewed patch",
        outcome="approved",
        source="interactive",
        decided_at=TIMESTAMP,
        arguments_sha256=hash_tool_arguments('{"path":"src/example.py"}'),
    )
    approved = _event(
        "approval.decided",
        approval_decision_to_dict(decision),
        events=tuple(events),
    )
    state = reduce_event(state, approved)
    events.append(approved)

    verification = verification_tool_state_from_dict(state.verification_state)
    verification.unresolved_failure_command_id = "python:pytest"
    verification.verification_history.append(
        VerificationResult(
            command_id="python:pytest",
            kind="test",
            status="failed",
            argv=("python", "-m", "pytest"),
            cwd="D:\\\\code\\\\coding",
            exit_code=1,
            duration_ms=100,
            output="failed",
            truncated=False,
            omitted_lines=0,
            omitted_bytes=0,
            attempt=1,
        )
    )
    verification.record_patch_applied()
    finished = _event(
        "tool.finished",
        {
            "call_id": "call-read",
            "output": {"ok": True},
            "verification_state": verification_tool_state_to_dict(verification),
            "touched_file_hashes": {"src/example.py": SHA_B},
        },
        events=tuple(events),
    )

    state = reduce_event(state, finished)
    restored = verification_tool_state_from_dict(state.verification_state)

    assert restored.edit_generation == 1
    assert restored.repair_attempts == 1
    assert restored.after_edit is True
    assert state.touched_file_hashes == {"src/example.py": SHA_B}


def test_tool_recovery_can_complete_or_reset_an_in_flight_call() -> None:
    events, state = _state_awaiting_tool()
    started = _event(
        "tool.started",
        {"call_id": "call-read"},
        events=tuple(events),
    )
    state = reduce_event(state, started)
    events.append(started)

    retry = _event(
        "tool.recovered",
        {
            "call_id": "call-read",
            "completed": False,
            "reason": "safe_retry",
            "requires_reapproval": False,
        },
        events=tuple(events),
    )
    retry_state = reduce_event(state, retry)
    assert retry_state.pending_tool_calls[0].started is False

    recovered = _event(
        "tool.recovered",
        {"call_id": "call-read", "output": {"ok": True}},
        events=tuple(events),
    )
    recovered_state = reduce_event(state, recovered)
    assert recovered_state.phase == "awaiting_model"
    assert recovered_state.pending_tool_calls == ()
    assert recovered_state.completed_call_ids == frozenset({"call-read"})


def test_side_effecting_recovery_reset_requires_explicit_reapproval() -> None:
    events, state = _state_awaiting_tool(tool_name="apply_patch")
    started = _event(
        "tool.started",
        {"call_id": "call-read"},
        events=tuple(events),
    )
    state = reduce_event(state, started)
    events.append(started)

    unsafe_retry = _event(
        "tool.recovered",
        {
            "call_id": "call-read",
            "completed": False,
            "reason": "retry_after_interruption",
            "requires_reapproval": False,
        },
        events=tuple(events),
    )

    with pytest.raises(SessionReductionError, match="explicit reapproval"):
        reduce_event(state, unsafe_retry)

def test_direct_model_payload_can_define_inline_tool_effects() -> None:
    events: list[SessionEvent] = []
    events.append(_event("session.started", _started_payload(), events=()))
    events.append(_event("context.created", {}, events=tuple(events)))
    events.append(
        _event(
            "model.requested",
            {"turn_index": 1, "previous_response_id": None},
            events=tuple(events),
        )
    )
    events.append(
        _event(
            "model.responded",
            {
                "response_id": "response-1",
                "text": "",
                "reasoning_summary": "",
                "function_calls": [
                    {
                        "call_id": "call-custom",
                        "name": "custom_tool",
                        "arguments": "{}",
                        "effect": "workspace_write",
                    }
                ],
            },
            events=tuple(events),
        )
    )

    state = rebuild_state(tuple(events))

    assert state.phase == "awaiting_tools"
    assert state.pending_tool_calls[0].effect == "workspace_write"


def test_artifact_backed_model_text_does_not_block_state_reduction() -> None:
    events: list[SessionEvent] = []
    events.append(_event("session.started", _started_payload(), events=()))
    events.append(_event("context.created", {}, events=tuple(events)))
    events.append(
        _event(
            "model.requested",
            {"turn_index": 1, "previous_response_id": None},
            events=tuple(events),
        )
    )
    response = _response_payload(function_calls=())
    response_data = cast(dict[str, object], response["response"])
    response_data["text"] = {
        "stored": True,
        "artifact": {
            "path": "artifacts/session/response.txt",
            "sha256": SHA_A,
            "byte_count": 100_000,
            "media_type": "text/plain",
            "encoding": "utf-8",
        },
    }
    events.append(
        _event("model.responded", response, events=tuple(events))
    )

    state = rebuild_state(tuple(events))

    assert state.phase == "finalizing"


def test_model_response_without_calls_enters_finalizing_then_completed() -> None:
    events = _initial_model_events(function_calls=())
    state = rebuild_state(tuple(events))
    assert state.phase == "finalizing"

    completed = _event("session.completed", {"answer": "done"}, events=tuple(events))
    state = reduce_event(state, completed)
    assert state.phase == "completed"
    assert state.status == "completed"

    appended = _event("context.created", {}, events=(*events, completed))
    with pytest.raises(SessionReductionError, match="terminal status"):
        reduce_event(state, appended)


def test_interrupted_session_must_resume_before_business_events() -> None:
    events, state = _state_awaiting_tool()
    interrupted = _event(
        "session.interrupted",
        {"reason": "KeyboardInterrupt"},
        events=tuple(events),
    )
    state = reduce_event(state, interrupted)
    events.append(interrupted)
    assert state.status == "interrupted"

    blocked = _event(
        "tool.started",
        {"call_id": "call-read"},
        events=tuple(events),
    )
    with pytest.raises(SessionReductionError, match="terminal status"):
        reduce_event(state, blocked)

    resumed = _event(
        "session.resumed",
        {"reason": "explicit_resume"},
        events=tuple(events),
    )
    state = reduce_event(state, resumed)
    assert state.status == "running"
    assert state.phase == "awaiting_tools"


def test_missing_start_event_empty_log_and_response_without_request_fail() -> None:
    context = _event("context.created", {}, events=())
    with pytest.raises(SessionReductionError, match="first event"):
        reduce_event(None, context)
    with pytest.raises(SessionReductionError, match="empty event log"):
        rebuild_state(())

    started = _event("session.started", _started_payload(), events=())
    state = reduce_event(None, started)
    responded = _event(
        "model.responded",
        _response_payload(function_calls=()),
        events=(started,),
    )
    with pytest.raises(SessionReductionError, match="model.requested"):
        reduce_event(state, responded)


def test_sequence_hash_and_session_identity_are_checked() -> None:
    started = _event("session.started", _started_payload(), events=())
    state = reduce_event(None, started)
    context = _event("context.created", {}, events=(started,))

    with pytest.raises(SessionReductionError, match="sequence"):
        reduce_event(state, replace(context, seq=3))
    with pytest.raises(SessionReductionError, match="previous event hash"):
        reduce_event(state, replace(context, prev_hash=SHA_A))
    with pytest.raises(SessionReductionError, match="different session IDs"):
        reduce_event(state, replace(context, session_id=OTHER_SESSION_ID))


def test_duplicate_context_and_model_request_are_rejected() -> None:
    started = _event("session.started", _started_payload(), events=())
    context = _event("context.created", {}, events=(started,))
    state = rebuild_state((started, context))
    duplicate_context = _event("context.created", {}, events=(started, context))
    with pytest.raises(SessionReductionError, match="cannot be recorded twice"):
        reduce_event(state, duplicate_context)

    requested = _event(
        "model.requested",
        {"turn_index": 1, "previous_response_id": None},
        events=(started, context),
    )
    state = reduce_event(state, requested)
    duplicate_request = _event(
        "model.requested",
        {"turn_index": 1, "previous_response_id": None},
        events=(started, context, requested),
    )
    with pytest.raises(SessionReductionError, match="already in flight"):
        reduce_event(state, duplicate_request)


def test_artifact_backed_control_payload_is_rejected_without_storage_access() -> None:
    artifact_payload = {
        "stored": True,
        "artifact": {
            "path": "artifacts/session/blob",
            "sha256": SHA_A,
            "byte_count": 10,
            "media_type": "application/json",
            "encoding": "utf-8",
        },
    }
    started = _event("session.started", artifact_payload, events=())

    with pytest.raises(SessionReductionError, match="artifact-backed"):
        reduce_event(None, started)


def _complete_session_events() -> list[SessionEvent]:
    events, state = _state_awaiting_tool()
    checkpoint = _event(
        "checkpoint.saved",
        checkpoint_to_dict(state.to_checkpoint()),
        events=tuple(events),
    )
    state = reduce_event(state, checkpoint)
    events.append(checkpoint)

    started = _event(
        "tool.started",
        {"call_id": "call-read", "name": "read_file"},
        events=tuple(events),
    )
    state = reduce_event(state, started)
    events.append(started)
    finished = _event(
        "tool.finished",
        {"call_id": "call-read", "output": {"ok": True, "content": "x = 1"}},
        events=tuple(events),
    )
    state = reduce_event(state, finished)
    events.append(finished)
    checkpoint = _event(
        "checkpoint.saved",
        {"checkpoint": checkpoint_to_dict(state.to_checkpoint())},
        events=tuple(events),
    )
    state = reduce_event(state, checkpoint)
    events.append(checkpoint)

    requested = _event(
        "model.requested",
        {"turn_index": 2, "previous_response_id": "response-1"},
        events=tuple(events),
    )
    state = reduce_event(state, requested)
    events.append(requested)
    responded = _event(
        "model.responded",
        _response_payload(response_id="response-2", function_calls=()),
        events=tuple(events),
    )
    state = reduce_event(state, responded)
    events.append(responded)
    checkpoint = _event(
        "checkpoint.saved",
        checkpoint_to_dict(state.to_checkpoint()),
        events=tuple(events),
    )
    state = reduce_event(state, checkpoint)
    events.append(checkpoint)
    completed = _event(
        "session.completed",
        {"answer": "done"},
        events=tuple(events),
    )
    events.append(completed)
    return events


def _state_awaiting_tool(
    *,
    tool_name: str = "read_file",
) -> tuple[list[SessionEvent], AgentSessionState]:
    call = ModelFunctionCall(
        call_id="call-read",
        name=tool_name,
        arguments='{"path":"src/example.py"}',
    )
    events = _initial_model_events(function_calls=(call,))
    return events, rebuild_state(tuple(events))


def _state_after_tool() -> tuple[list[SessionEvent], AgentSessionState]:
    events, state = _state_awaiting_tool()
    started = _event(
        "tool.started",
        {"call_id": "call-read"},
        events=tuple(events),
    )
    state = reduce_event(state, started)
    events.append(started)
    finished = _event(
        "tool.finished",
        {"call_id": "call-read", "output": {"ok": True}},
        events=tuple(events),
    )
    state = reduce_event(state, finished)
    events.append(finished)
    return events, state


def _initial_model_events(
    *,
    function_calls: tuple[ModelFunctionCall, ...],
) -> list[SessionEvent]:
    events: list[SessionEvent] = []
    events.append(_event("session.started", _started_payload(), events=()))
    events.append(_event("context.created", {"summary": "small"}, events=tuple(events)))
    events.append(
        _event(
            "model.requested",
            {"turn_index": 1, "previous_response_id": None},
            events=tuple(events),
        )
    )
    events.append(
        _event(
            "model.responded",
            _response_payload(function_calls=function_calls),
            events=tuple(events),
        )
    )
    return events



def test_session_resumed_can_retry_an_unrecorded_model_request() -> None:
    events: list[SessionEvent] = []
    events.append(_event("session.started", _started_payload(), events=tuple(events)))
    events.append(_event("context.created", {"files": []}, events=tuple(events)))
    events.append(
        _event(
            "model.requested",
            {"turn_index": 1, "previous_response_id": None},
            events=tuple(events),
        )
    )
    events.append(
        _event(
            "session.failed",
            {"reason": "remote_failure"},
            events=tuple(events),
        )
    )
    failed = rebuild_state(tuple(events))
    assert failed.model_request_pending is True

    resumed_event = _event(
        "session.resumed",
        {"retry_pending_model_request": True},
        events=tuple(events),
    )
    resumed = reduce_event(failed, resumed_event)

    assert resumed.status == "running"
    assert resumed.phase == "awaiting_initial_model"
    assert resumed.model_request_pending is False


def test_session_resumed_rejects_invalid_model_retry_metadata() -> None:
    started_event = _event("session.started", _started_payload(), events=())
    state = reduce_event(None, started_event)

    not_boolean = _event(
        "session.resumed",
        {"retry_pending_model_request": "yes"},
        events=(started_event,),
    )
    with pytest.raises(SessionReductionError, match="must be a boolean"):
        reduce_event(state, not_boolean)

    no_pending_request = _event(
        "session.resumed",
        {"retry_pending_model_request": True},
        events=(started_event,),
    )
    with pytest.raises(SessionReductionError, match="not pending"):
        reduce_event(state, no_pending_request)

def _response_payload(
    *,
    response_id: str = "response-1",
    function_calls: tuple[ModelFunctionCall, ...],
) -> dict[str, object]:
    response = NormalizedModelResponse(
        response_id=response_id,
        text="done" if not function_calls else "",
        reasoning_summary="",
        function_calls=function_calls,
    )
    payload: dict[str, object] = {
        "response": normalized_model_response_to_dict(response),
    }
    if function_calls:
        effect = "workspace_write" if function_calls[0].name == "apply_patch" else "read_only"
        payload["pending_tool_calls"] = [
            pending_tool_call_to_dict(
                PendingToolCall(
                    call_id=call.call_id,
                    name=call.name,
                    arguments=call.arguments,
                    effect=cast(ToolEffect, effect),
                    started=False,
                )
            )
            for call in function_calls
        ]
    return payload


def _started_payload() -> dict[str, object]:
    guard = WorkspaceGuard(
        workspace="D:\\code\\coding",
        git_head=None,
        touched_file_hashes={},
    )
    started = SessionStarted(
        task="fix failing tests",
        workspace=guard.workspace,
        config={
            "workspace": guard.workspace,
            "model": "gpt-test",
            "max_turns": 8,
            "permission_mode": "workspace-write",
            "max_fix_attempts": 2,
        },
        git_head=None,
        workspace_guard=guard,
    )
    return session_started_to_dict(started)


def _event(
    event_type: str,
    payload: dict[str, object],
    *,
    events: tuple[SessionEvent, ...],
) -> SessionEvent:
    previous = events[-1] if events else None
    return create_session_event(
        session_id=SESSION_ID,
        seq=len(events) + 1,
        event_id=f"event-{len(events) + 1:04d}",
        recorded_at=TIMESTAMP,
        event_type=cast(SessionEventType, event_type),
        prev_hash=previous.event_hash if previous is not None else None,
        payload=payload,
    )




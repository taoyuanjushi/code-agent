from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from coding_agent.sessions.models import (
    APPROVAL_OUTCOMES,
    APPROVAL_SOURCES,
    SESSION_CONFIG_FIELDS,
    SESSION_EVENT_TYPES,
    SESSION_PHASES,
    SESSION_SCHEMA_VERSION,
    SESSION_STATUSES,
    TOOL_EFFECTS,
    AgentSessionCheckpoint,
    ApprovalDecision,
    ArtifactRef,
    ModelFunctionCall,
    NormalizedModelResponse,
    PendingToolCall,
    SessionEvent,
    SessionStarted,
    WorkspaceGuard,
)

SESSION_ID = "20260714T031500Z-a1b2c3d4"
TIMESTAMP = "2026-07-14T03:15:04.125Z"
SHA_A = "a" * 64
SHA_B = "b" * 64


def test_session_domain_literal_sets_are_fixed() -> None:
    assert SESSION_STATUSES == {"running", "completed", "failed", "interrupted"}
    assert SESSION_PHASES == {
        "awaiting_initial_model",
        "awaiting_tools",
        "awaiting_model",
        "finalizing",
        "completed",
    }
    assert TOOL_EFFECTS == {"read_only", "workspace_write", "process"}
    assert APPROVAL_OUTCOMES == {"approved", "denied"}
    assert APPROVAL_SOURCES == {
        "interactive",
        "auto_policy",
        "resume_recovery",
    }


def test_session_event_supports_the_complete_m4_event_vocabulary() -> None:
    assert SESSION_EVENT_TYPES == {
        "session.started",
        "session.resumed",
        "session.completed",
        "session.failed",
        "session.interrupted",
        "context.created",
        "model.requested",
        "model.responded",
        "tool.started",
        "tool.finished",
        "tool.recovered",
        "approval.decided",
        "verification.recorded",
        "checkpoint.saved",
    }

    for sequence, event_type in enumerate(sorted(SESSION_EVENT_TYPES), start=1):
        event = _event(seq=sequence, event_type=event_type)
        assert event.type == event_type


def test_session_event_is_deeply_immutable() -> None:
    source_payload = {"nested": {"values": [1, 2]}}
    event = _event(payload=source_payload)

    source_payload["nested"]["values"].append(3)  # type: ignore[index,union-attr]

    assert event.payload["nested"]["values"] == (1, 2)  # type: ignore[index]
    with pytest.raises(TypeError):
        event.payload["new"] = True  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        event.seq = 2  # type: ignore[misc]


@pytest.mark.parametrize("schema_version", [0, 2, -1])
def test_session_event_rejects_unknown_schema_versions(schema_version: int) -> None:
    with pytest.raises(ValueError, match="schema version"):
        _event(schema_version=schema_version)


def test_session_event_rejects_boolean_schema_version() -> None:
    with pytest.raises(TypeError, match="schema_version"):
        _event(schema_version=True)


@pytest.mark.parametrize("seq", [-1, 0, True])
def test_session_event_rejects_non_positive_sequence(seq: Any) -> None:
    with pytest.raises(ValueError, match="seq"):
        _event(seq=seq)


@pytest.mark.parametrize(
    "timestamp",
    [
        "",
        "2026-07-14T03:15:04",
        "2026-07-14T03:15:04+08:00",
        "not-a-timestampZ",
    ],
)
def test_session_event_rejects_non_utc_or_invalid_timestamps(timestamp: str) -> None:
    with pytest.raises(ValueError, match="recorded_at"):
        _event(recorded_at=timestamp)


@pytest.mark.parametrize(
    "session_id",
    ["", "../other", "20260714T031500Z-abc", "20260714T031500Z-A1B2C3D4"],
)
def test_session_event_rejects_invalid_session_ids(session_id: str) -> None:
    with pytest.raises(ValueError, match="session_id"):
        _event(session_id=session_id)


def test_session_event_rejects_unknown_event_type_and_bad_hashes() -> None:
    with pytest.raises(ValueError, match="event_id"):
        _event(event_id="")
    with pytest.raises(ValueError, match="event type"):
        _event(event_type="session.unknown")
    with pytest.raises(ValueError, match="prev_hash"):
        _event(prev_hash="A" * 64)
    with pytest.raises(ValueError, match="event_hash"):
        _event(event_hash="short")


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/session/blob",
        "C:/session/blob",
        r"C:\session\blob",
        "../session/blob",
        "session/../blob",
        "session//blob",
        ".",
    ],
)
def test_artifact_ref_rejects_absolute_or_non_normalized_paths(path: str) -> None:
    with pytest.raises(ValueError, match="artifact path"):
        ArtifactRef(
            path=path,
            sha256=SHA_A,
            byte_count=12,
            media_type="application/json",
            encoding="utf-8",
        )


def test_artifact_ref_validates_hash_count_media_type_and_encoding() -> None:
    artifact = ArtifactRef(
        path=f"{SESSION_ID}/{SHA_A}.blob",
        sha256=SHA_A,
        byte_count=12,
        media_type="application/json",
        encoding="utf-8",
    )

    assert artifact.byte_count == 12
    with pytest.raises(ValueError, match="sha256"):
        ArtifactRef("session/blob", "A" * 64, 1, "text/plain")
    with pytest.raises(ValueError, match="byte_count"):
        ArtifactRef("session/blob", SHA_A, -1, "text/plain")
    with pytest.raises(ValueError, match="media_type"):
        ArtifactRef("session/blob", SHA_A, 1, "")
    with pytest.raises(ValueError, match="encoding"):
        ArtifactRef("session/blob", SHA_A, 1, "text/plain", " ")


def test_workspace_guard_and_session_started_are_immutable() -> None:
    source_hashes = {"src/service.py": SHA_A, "new.py": None}
    guard = WorkspaceGuard(
        workspace="D:/code/project",
        git_head="abc123",
        touched_file_hashes=source_hashes,
    )
    started = SessionStarted(
        task="Fix service",
        workspace="D:/code/project",
        config={"model": "fake-model", "max_turns": 8},
        git_head="abc123",
        workspace_guard=guard,
    )

    source_hashes["src/service.py"] = SHA_B

    assert guard.touched_file_hashes["src/service.py"] == SHA_A
    assert started.config == {"model": "fake-model", "max_turns": 8}
    with pytest.raises(TypeError):
        started.config["model"] = "changed"  # type: ignore[index]


def test_session_started_accepts_only_the_safe_config_whitelist() -> None:
    assert SESSION_CONFIG_FIELDS == {
        "workspace",
        "model",
        "reasoning_effort",
        "max_turns",
        "permission_mode",
        "auto_approve_commands",
        "auto_approve_edits",
        "context_max_files",
        "context_max_bytes_per_file",
        "max_fix_attempts",
    }
    guard = WorkspaceGuard("D:/code/project", None, {})

    with pytest.raises(ValueError, match="unsupported persisted fields"):
        SessionStarted(
            "Task",
            "D:/code/project",
            {"OPENAI_API_KEY": "must-not-persist"},
            None,
            guard,
        )


def test_session_started_requires_matching_workspace_and_git_head() -> None:
    guard = WorkspaceGuard("D:/code/project", "abc123", {})

    with pytest.raises(ValueError, match="workspace must match"):
        SessionStarted("Task", "D:/other", {}, "abc123", guard)
    with pytest.raises(ValueError, match="git_head must match"):
        SessionStarted("Task", "D:/code/project", {}, "def456", guard)
    with pytest.raises(ValueError, match="task"):
        SessionStarted(" ", "D:/code/project", {}, "abc123", guard)


def test_workspace_guard_rejects_non_normalized_workspace() -> None:
    with pytest.raises(ValueError, match="normalized"):
        WorkspaceGuard("D:/code/../project", None, {})
    with pytest.raises(ValueError, match="absolute"):
        WorkspaceGuard(r"\not-a-unc-path", None, {})


def test_workspace_guard_rejects_relative_workspace_and_bad_file_entries() -> None:
    with pytest.raises(ValueError, match="workspace"):
        WorkspaceGuard("relative/project", None, {})
    with pytest.raises(ValueError, match="touched file path"):
        WorkspaceGuard("D:/code/project", None, {"../escape.py": SHA_A})
    with pytest.raises(ValueError, match="hash for"):
        WorkspaceGuard("D:/code/project", None, {"service.py": "bad"})
    with pytest.raises(TypeError, match="string or null"):
        WorkspaceGuard("D:/code/project", None, {"service.py": 123})


def test_normalized_model_response_contains_only_project_domain_values() -> None:
    call = ModelFunctionCall("call-1", "search_text", '{"query":"refund"}')
    response = NormalizedModelResponse(
        response_id="response-1",
        text="I will inspect the target.",
        reasoning_summary="Search before reading.",
        function_calls=(call,),
    )

    assert response.function_calls == (call,)
    with pytest.raises(ValueError, match="unique"):
        NormalizedModelResponse(
            "response-1",
            "",
            "",
            (call, call),
        )
    with pytest.raises(TypeError, match="tuple"):
        NormalizedModelResponse("response-1", "", "", [call])  # type: ignore[arg-type]


def test_pending_tool_call_validates_effect_started_and_identifiers() -> None:
    call = PendingToolCall(
        call_id="call-1",
        name="apply_patch",
        arguments="{}",
        effect="workspace_write",
        started=True,
    )

    assert call.effect == "workspace_write"
    with pytest.raises(ValueError, match="tool effect"):
        PendingToolCall("call-1", "tool", "{}", "network", False)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="boolean"):
        PendingToolCall("call-1", "tool", "{}", "read_only", 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="call_id"):
        PendingToolCall("", "tool", "{}", "read_only", False)


def test_checkpoint_freezes_nested_state_and_enforces_call_id_invariants() -> None:
    call = PendingToolCall("call-1", "search_text", "{}", "read_only", False)
    verification_state = {"history": [{"status": "failed"}]}
    checkpoint = AgentSessionCheckpoint(
        phase="awaiting_tools",
        turn_index=1,
        previous_response_id="response-1",
        pending_tool_calls=(call,),
        pending_tool_outputs=({"type": "function_call_output"},),
        completed_call_ids=frozenset({"call-0"}),
        verification_state=verification_state,
        touched_file_hashes={"src/service.py": SHA_A},
    )

    verification_state["history"].append({"status": "passed"})

    assert checkpoint.verification_state["history"] == (
        {"status": "failed"},
    )
    with pytest.raises(TypeError):
        checkpoint.verification_state["new"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="disjoint"):
        AgentSessionCheckpoint(
            "awaiting_tools",
            1,
            "response-1",
            (call,),
            (),
            frozenset({"call-1"}),
            {},
            {},
        )


def test_checkpoint_rejects_invalid_phase_turn_and_phase_specific_state() -> None:
    call = PendingToolCall("call-1", "search_text", "{}", "read_only", False)

    with pytest.raises(ValueError, match="session phase"):
        _checkpoint(phase="unknown")
    with pytest.raises(ValueError, match="turn_index"):
        _checkpoint(turn_index=-1)
    with pytest.raises(ValueError, match="previous response"):
        _checkpoint(phase="awaiting_initial_model", previous_response_id="response-1")
    with pytest.raises(ValueError, match="completed checkpoint"):
        _checkpoint(phase="completed", pending_tool_calls=(call,))


def test_approval_decision_validates_all_security_relevant_fields() -> None:
    decision = ApprovalDecision(
        approval_id="approval-1",
        call_id="call-1",
        action="apply_patch",
        summary="Update src/service.py",
        outcome="approved",
        source="interactive",
        decided_at=TIMESTAMP,
        arguments_sha256=SHA_A,
    )

    assert decision.outcome == "approved"
    assert decision.source == "interactive"

    for field_name in ("approval_id", "call_id", "action", "summary"):
        values = {
            "approval_id": "approval-1",
            "call_id": "call-1",
            "action": "apply_patch",
            "summary": "Update src/service.py",
        }
        values[field_name] = ""
        expected_label = {
            "approval_id": "approval_id",
            "call_id": "call_id",
            "action": "approval action",
            "summary": "approval summary",
        }[field_name]
        with pytest.raises(ValueError, match=expected_label):
            ApprovalDecision(
                **values,
                outcome="approved",
                source="interactive",
                decided_at=TIMESTAMP,
                arguments_sha256=SHA_A,
            )

    with pytest.raises(ValueError, match="outcome"):
        _approval(outcome="unknown")
    with pytest.raises(ValueError, match="source"):
        _approval(source="unknown")
    with pytest.raises(ValueError, match="decided_at"):
        _approval(decided_at="2026-07-14T11:15:04+08:00")
    with pytest.raises(ValueError, match="arguments_sha256"):
        _approval(arguments_sha256="bad")


def test_json_domain_fields_reject_non_json_and_non_finite_values() -> None:
    with pytest.raises(TypeError, match="JSON-compatible"):
        _event(payload={"bad": object()})
    with pytest.raises(ValueError, match="finite JSON number"):
        _event(payload={"bad": float("nan")})
    with pytest.raises(TypeError, match="keys must be strings"):
        _event(payload={1: "bad"})  # type: ignore[dict-item]


def _event(
    *,
    schema_version: Any = SESSION_SCHEMA_VERSION,
    session_id: str = SESSION_ID,
    seq: Any = 1,
    event_id: str = "event-1",
    recorded_at: str = TIMESTAMP,
    event_type: Any = "session.started",
    prev_hash: str | None = None,
    payload: Any = None,
    event_hash: str = SHA_A,
) -> SessionEvent:
    return SessionEvent(
        schema_version=schema_version,
        session_id=session_id,
        seq=seq,
        event_id=event_id,
        recorded_at=recorded_at,
        type=event_type,
        prev_hash=prev_hash,
        payload={} if payload is None else payload,
        event_hash=event_hash,
    )


def _checkpoint(**overrides: Any) -> AgentSessionCheckpoint:
    values: dict[str, Any] = {
        "phase": "awaiting_model",
        "turn_index": 0,
        "previous_response_id": None,
        "pending_tool_calls": (),
        "pending_tool_outputs": (),
        "completed_call_ids": frozenset(),
        "verification_state": {},
        "touched_file_hashes": {},
    }
    values.update(overrides)
    return AgentSessionCheckpoint(**values)


def _approval(**overrides: Any) -> ApprovalDecision:
    values: dict[str, Any] = {
        "approval_id": "approval-1",
        "call_id": "call-1",
        "action": "apply_patch",
        "summary": "Update service",
        "outcome": "approved",
        "source": "interactive",
        "decided_at": TIMESTAMP,
        "arguments_sha256": SHA_A,
    }
    values.update(overrides)
    return ApprovalDecision(**values)

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Callable

import pytest

from coding_agent.sessions.codec import (
    SessionCodecError,
    approval_decision_from_dict,
    approval_decision_to_dict,
    artifact_ref_from_dict,
    artifact_ref_to_dict,
    calculate_event_hash,
    canonical_json,
    canonical_json_bytes,
    checkpoint_from_dict,
    checkpoint_to_dict,
    create_session_event,
    decode_event,
    encode_event,
    migrate_event,
    model_function_call_from_dict,
    model_function_call_to_dict,
    normalized_model_response_from_dict,
    normalized_model_response_to_dict,
    pending_tool_call_from_dict,
    pending_tool_call_to_dict,
    session_event_from_dict,
    session_event_to_dict,
    session_started_from_dict,
    session_started_to_dict,
    verification_command_from_dict,
    verification_command_to_dict,
    verification_discovery_from_dict,
    verification_discovery_to_dict,
    verification_result_from_dict,
    verification_result_to_dict,
    verification_tool_state_from_dict,
    verification_tool_state_to_dict,
    verify_event_chain,
    workspace_guard_from_dict,
    workspace_guard_to_dict,
)
from coding_agent.sessions.models import (
    SESSION_SCHEMA_VERSION,
    AgentSessionCheckpoint,
    ApprovalDecision,
    ArtifactRef,
    ModelFunctionCall,
    NormalizedModelResponse,
    PendingToolCall,
    SessionStarted,
    WorkspaceGuard,
)
from coding_agent.tools import VerificationToolState
from coding_agent.verification import (
    VerificationCommand,
    VerificationDiscoveryResult,
    VerificationResult,
)

SESSION_ID = "20260714T031500Z-a1b2c3d4"
OTHER_SESSION_ID = "20260714T031501Z-b1c2d3e4"
TIMESTAMP = "2026-07-14T03:15:04.125Z"
SHA_A = "a" * 64
SHA_B = "b" * 64


def test_canonical_json_is_unicode_stable_compact_and_rejects_nan() -> None:
    value = {"z": "中文", "a": [2, 1]}

    assert canonical_json(value) == '{"a":[2,1],"z":"中文"}'
    assert canonical_json_bytes(value) == canonical_json(value).encode("utf-8")
    assert " " not in canonical_json(value)

    with pytest.raises(ValueError, match="canonical JSON-compatible"):
        canonical_json({"invalid": float("nan")})


def test_session_domain_models_have_explicit_round_trip_codecs(tmp_path: Path) -> None:
    workspace = str(tmp_path.resolve())
    artifact = ArtifactRef(
        path=f"{SESSION_ID}/{SHA_A}.blob",
        sha256=SHA_A,
        byte_count=12,
        media_type="application/json",
        encoding="utf-8",
    )
    guard = WorkspaceGuard(
        workspace=workspace,
        git_head="abc123",
        touched_file_hashes={"src/app.py": SHA_A, "src/new.py": None},
    )
    started = SessionStarted(
        task="修复 Unicode 行为",
        workspace=workspace,
        config={"workspace": workspace, "model": "gpt-test", "max_turns": 8},
        git_head="abc123",
        workspace_guard=guard,
    )
    function_call = ModelFunctionCall(
        call_id="call-1",
        name="read_file",
        arguments='{"path":"src/app.py"}',
    )
    response = NormalizedModelResponse(
        response_id="response-1",
        text="读取目标文件",
        reasoning_summary="先搜索，再读取。",
        function_calls=(function_call,),
    )
    pending = PendingToolCall(
        call_id="call-1",
        name="read_file",
        arguments=function_call.arguments,
        effect="read_only",
        started=True,
    )
    checkpoint = AgentSessionCheckpoint(
        phase="awaiting_tools",
        turn_index=2,
        previous_response_id="response-1",
        pending_tool_calls=(pending,),
        pending_tool_outputs=({"call_id": "call-2", "items": [1, "二"]},),
        completed_call_ids=frozenset({"call-3", "call-2"}),
        verification_state={"repair_attempts": 1, "commands": ["python:pytest"]},
        touched_file_hashes={"src/app.py": SHA_B},
    )
    approval = ApprovalDecision(
        approval_id="approval-1",
        call_id="call-4",
        action="apply_patch",
        summary="更新实现",
        outcome="approved",
        source="interactive",
        decided_at=TIMESTAMP,
        arguments_sha256=SHA_B,
    )

    round_trips: tuple[
        tuple[object, Callable[[object], dict[str, object]], Callable[[dict[str, object]], object]],
        ...,
    ] = (
        (artifact, artifact_ref_to_dict, artifact_ref_from_dict),
        (guard, workspace_guard_to_dict, workspace_guard_from_dict),
        (started, session_started_to_dict, session_started_from_dict),
        (function_call, model_function_call_to_dict, model_function_call_from_dict),
        (
            response,
            normalized_model_response_to_dict,
            normalized_model_response_from_dict,
        ),
        (pending, pending_tool_call_to_dict, pending_tool_call_from_dict),
        (checkpoint, checkpoint_to_dict, checkpoint_from_dict),
        (approval, approval_decision_to_dict, approval_decision_from_dict),
    )

    for value, to_dict, from_dict in round_trips:
        encoded = to_dict(value)
        assert from_dict(json.loads(canonical_json(encoded))) == value

    assert checkpoint_to_dict(checkpoint)["completed_call_ids"] == ["call-2", "call-3"]


def test_event_creation_encoding_decoding_and_hashing_round_trip() -> None:
    event = _event(payload={"message": "你好", "nested": {"values": [2, 1]}})

    raw = encode_event(event)

    assert not raw.endswith(b"\n")
    assert raw == canonical_json_bytes(session_event_to_dict(event))
    assert event.event_hash == calculate_event_hash(event)
    assert decode_event(raw, source="events.jsonl", line_number=4) == event
    assert session_event_from_dict(json.loads(raw)) == event


def test_decode_event_rejects_payload_tampering() -> None:
    data = session_event_to_dict(_event())
    data["payload"] = {"tampered": True}

    with pytest.raises(SessionCodecError, match="event_hash does not match"):
        decode_event(canonical_json_bytes(data))


def test_decode_errors_report_source_and_line_number() -> None:
    with pytest.raises(SessionCodecError, match=r"events\.jsonl:7:.*UTF-8"):
        decode_event(b"\xff", source="events.jsonl", line_number=7)

    with pytest.raises(SessionCodecError, match=r"events\.jsonl:11: invalid JSON"):
        decode_event(b'{\n"schema_version":}', source="events.jsonl", line_number=10)


def test_decode_event_rejects_unknown_schema_version_without_guessing() -> None:
    data = session_event_to_dict(_event())
    data["schema_version"] = 2
    data["event_hash"] = calculate_event_hash(data)

    with pytest.raises(
        SessionCodecError,
        match=r"events\.jsonl:9: unsupported session schema version 2",
    ):
        decode_event(canonical_json_bytes(data), source="events.jsonl", line_number=9)


def test_event_codec_rejects_missing_and_unknown_fields() -> None:
    missing = session_event_to_dict(_event())
    missing.pop("payload")
    missing["event_hash"] = calculate_event_hash(missing)

    with pytest.raises(SessionCodecError, match="missing fields: payload"):
        decode_event(canonical_json_bytes(missing))

    unknown = session_event_to_dict(_event())
    unknown["future_field"] = True
    unknown["event_hash"] = calculate_event_hash(unknown)

    with pytest.raises(SessionCodecError, match="unknown fields: future_field"):
        decode_event(canonical_json_bytes(unknown))


def test_event_chain_accepts_contiguous_events() -> None:
    first = _event(seq=1, event_id="event-1")
    second = _event(
        seq=2,
        event_id="event-2",
        event_type="context.created",
        prev_hash=first.event_hash,
    )
    third = _event(
        seq=3,
        event_id="event-3",
        event_type="model.requested",
        prev_hash=second.event_hash,
    )

    verify_event_chain((first, second, third), source="events.jsonl")


@pytest.mark.parametrize(
    ("events_factory", "message", "line_number"),
    [
        (
            lambda first, second: (first, replace(second, seq=3)),
            "expected seq 2, found 3",
            2,
        ),
        (
            lambda first, second: (
                first,
                _event(
                    session_id=OTHER_SESSION_ID,
                    seq=2,
                    event_id="other-2",
                    prev_hash=first.event_hash,
                ),
            ),
            "different session",
            2,
        ),
        (
            lambda first, second: (
                first,
                _event(seq=2, event_id="event-2", prev_hash=SHA_B),
            ),
            "prev_hash does not match",
            2,
        ),
        (
            lambda first, second: (first, replace(second, event_hash=SHA_B)),
            "event_hash does not match",
            2,
        ),
    ],
)
def test_event_chain_rejects_corruption(
    events_factory: Callable,
    message: str,
    line_number: int,
) -> None:
    first = _event(seq=1, event_id="event-1")
    second = _event(
        seq=2,
        event_id="event-2",
        prev_hash=first.event_hash,
    )

    with pytest.raises(
        SessionCodecError,
        match=rf"events\.jsonl:{line_number}:.*{message}",
    ):
        verify_event_chain(events_factory(first, second), source="events.jsonl")


def test_migrate_event_exposes_an_explicit_no_migration_boundary() -> None:
    with pytest.raises(SessionCodecError, match="no migration is available"):
        migrate_event({"schema_version": 9}, 9)


def test_verification_models_round_trip_through_explicit_codecs(
    tmp_path: Path,
) -> None:
    command, discovery = _verification_discovery(tmp_path)

    assert verification_command_from_dict(
        json.loads(canonical_json(verification_command_to_dict(command)))
    ) == command
    assert verification_discovery_from_dict(
        json.loads(canonical_json(verification_discovery_to_dict(discovery)))
    ) == discovery

    for status, exit_code in (
        ("passed", 0),
        ("failed", 1),
        ("timed_out", None),
        ("not_found", None),
        ("error", None),
    ):
        result = _verification_result(
            tmp_path,
            status=status,
            exit_code=exit_code,
        )
        assert verification_result_from_dict(
            json.loads(canonical_json(verification_result_to_dict(result)))
        ) == result


def test_verification_tool_state_round_trip_preserves_resume_fields(
    tmp_path: Path,
) -> None:
    command, discovery = _verification_discovery(tmp_path)
    state_with_discovery = VerificationToolState(task="修复测试", max_fix_attempts=2)
    state_with_discovery.discovery = discovery

    assert verification_tool_state_from_dict(
        json.loads(canonical_json(verification_tool_state_to_dict(state_with_discovery)))
    ) == state_with_discovery

    state = VerificationToolState(task="修复测试", max_fix_attempts=2)
    state.record_verification(
        _verification_result(
            tmp_path,
            command_id="python:ruff",
            kind="lint",
            status="passed",
            exit_code=0,
        )
    )
    state.record_verification(
        _verification_result(
            tmp_path,
            command_id=command.id,
            status="failed",
            exit_code=1,
        )
    )
    state.record_patch_applied()

    encoded = verification_tool_state_to_dict(state)
    restored = verification_tool_state_from_dict(json.loads(canonical_json(encoded)))

    assert restored == state
    assert restored.unresolved_failure_command_id == command.id
    assert restored.repair_attempts == 1
    assert restored.after_edit is True
    assert restored.edit_generation == 1
    assert restored.passed_generations == {"python:ruff": 0}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda data: data.update({"repair_attempts": 1}),
            "repair_attempts must be zero",
        ),
        (
            lambda data: data.update(
                {
                    "unresolved_failure_command_id": "python:missing",
                    "repair_attempts": 0,
                }
            ),
            "latest failed verification",
        ),
        (
            lambda data: data.update(
                {"edit_generation": 0, "passed_generations": {"python:pytest": 1}}
            ),
            "passed generation",
        ),
    ],
)
def test_verification_tool_state_rejects_inconsistent_persisted_state(
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    state = VerificationToolState(task="fix", max_fix_attempts=2)
    data = verification_tool_state_to_dict(state)
    mutate(data)

    with pytest.raises((TypeError, ValueError), match=message):
        verification_tool_state_from_dict(data)


def test_from_dict_rejects_unknown_fields_and_revalidates_domain_values(
    tmp_path: Path,
) -> None:
    command, _ = _verification_discovery(tmp_path)
    command_data = verification_command_to_dict(command)
    command_data["unexpected"] = True

    with pytest.raises(ValueError, match="unknown fields: unexpected"):
        verification_command_from_dict(command_data)

    result_data = verification_result_to_dict(
        _verification_result(tmp_path, status="passed", exit_code=0)
    )
    result_data["exit_code"] = 1

    with pytest.raises(ValueError, match="passed results must have exit_code=0"):
        verification_result_from_dict(result_data)


def _event(
    *,
    session_id: str = SESSION_ID,
    seq: int = 1,
    event_id: str = "event-1",
    event_type: str = "session.started",
    prev_hash: str | None = None,
    payload: dict[str, object] | None = None,
):
    return create_session_event(
        session_id=session_id,
        seq=seq,
        event_id=event_id,
        recorded_at=TIMESTAMP,
        event_type=event_type,  # type: ignore[arg-type]
        prev_hash=prev_hash,
        payload={} if payload is None else payload,
    )


def _verification_discovery(
    tmp_path: Path,
) -> tuple[VerificationCommand, VerificationDiscoveryResult]:
    workspace = str(tmp_path.resolve())
    command = VerificationCommand(
        id="python:pytest",
        kind="test",
        argv=("python", "-m", "pytest", "-q"),
        cwd=workspace,
        source="pyproject.toml",
        available=True,
        reason="Configured pytest suite",
    )
    return command, VerificationDiscoveryResult(
        workspace=workspace,
        commands=(command,),
        warnings=("optional linter unavailable",),
        errors=(),
    )


def _verification_result(
    tmp_path: Path,
    *,
    command_id: str = "python:pytest",
    kind: str = "test",
    status: str,
    exit_code: int | None,
) -> VerificationResult:
    return VerificationResult(
        command_id=command_id,
        kind=kind,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        argv=("python", "-m", "pytest", "-q"),
        cwd=str(tmp_path.resolve()),
        exit_code=exit_code,
        duration_ms=25,
        output="failure context" if status == "failed" else "",
        truncated=False,
        omitted_lines=0,
        omitted_bytes=0,
        attempt=1,
    )

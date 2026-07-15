"""M4 acceptance metrics and recovery scenario contracts.

This module intentionally does not import future session production code. Step 1
fixes the observable persistence, resume, replay, and audit behavior that later M4
implementation steps must satisfy without changing ``src/coding_agent/``.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

M4_SCHEMA_VERSION = 1
M4_INLINE_PAYLOAD_MAX_BYTES = 64 * 1024
M4_ARTIFACT_MAX_BYTES = 4 * 1024 * 1024
M4_SESSION_ID_MIN_RANDOM_HEX_LENGTH = 8
M4_MAX_SESSION_WRITERS = 1

M4_SESSION_ID_PATTERN = re.compile(
    rf"^\d{{8}}T\d{{6}}Z-[0-9a-f]{{{M4_SESSION_ID_MIN_RANDOM_HEX_LENGTH},}}$"
)
M4_REQUIRED_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "session_id",
        "seq",
        "event_id",
        "recorded_at",
        "type",
        "prev_hash",
        "payload",
        "event_hash",
    }
)
M4_REQUIRED_AUDIT_EVENTS = frozenset(
    {
        "session.started",
        "context.created",
        "model.requested",
        "model.responded",
        "tool.started",
        "approval.decided",
        "tool.finished",
        "verification.recorded",
        "checkpoint.saved",
        "session.completed",
    }
)
M4_NEW_SESSION_STARTUP_OBSERVABLES = (
    "session.created",
    "session_id.printed",
    "model.requested",
)
M4_NEW_SESSION_EVENT_PREFIX = (
    "session.started",
    "context.created",
    "model.requested",
)
M4_SIDE_EFFECT_BOUNDARY = (
    "tool.started",
    "approval.decided",
    "side_effect",
    "tool.finished",
    "checkpoint.saved",
)
M4_RECOVERABLE_LOG_DAMAGE = frozenset({"incomplete_final_line"})
M4_FATAL_LOG_DAMAGE = frozenset(
    {
        "invalid_utf8",
        "invalid_complete_json_line",
        "middle_line_corruption",
        "non_contiguous_sequence",
        "hash_chain_mismatch",
        "unknown_schema_version",
    }
)
M4_TOOL_RECOVERY_POLICY = {
    "read_only": "safe_retry",
    "workspace_write": "reconcile_file_hashes",
    "process": "explicit_reapproval",
}
M4_UNFINISHED_PROCESS_TOOLS = frozenset({"run_command", "run_verification"})
M4_AUTO_APPROVE_BYPASSES_RECOVERY = False
M4_PATCH_RESUME_SCENARIO = (
    "verification.failed",
    "patch.side_effect_applied",
    "process.interrupted_before_tool_finished",
    "patch.recovered_from_after_hash",
    "verification.passed",
)
M4_REPLAY_EXTERNAL_CALL_LIMITS = {
    "model_client_constructions": 0,
    "model_calls": 0,
    "tool_calls": 0,
    "subprocess_calls": 0,
    "input_calls": 0,
}
M4_REPLAY_REQUIRES_API_KEY = False
M4_RESUME_REJECTION_REASONS = frozenset(
    {
        "workspace_mismatch",
        "session_id_path_traversal",
        "touched_file_drift",
        "concurrent_writer",
    }
)
M4_FORBIDDEN_PERSISTED_FIELDS = frozenset(
    {
        "OPENAI_API_KEY",
        "environment",
        "authorization",
        "request_headers",
    }
)
M4_SENSITIVE_ENV_SUFFIXES = ("_TOKEN", "_SECRET", "_PASSWORD")
M4_SENSITIVE_VALUE_FIXTURES = (
    "sk-m4-acceptance-secret",
    "m4-token-value",
    "m4-password-value",
)
M4_REQUIRED_REGRESSION_TESTS = frozenset(
    {
        "tests/test_integration.py",
        "tests/test_m2_integration.py",
        "tests/test_m3_integration.py",
        "tests/test_m3_acceptance.py",
    }
)


def test_m4_numeric_acceptance_limits_are_fixed() -> None:
    assert M4_SCHEMA_VERSION == 1
    assert M4_INLINE_PAYLOAD_MAX_BYTES == 65_536
    assert M4_ARTIFACT_MAX_BYTES == 4_194_304
    assert M4_SESSION_ID_MIN_RANDOM_HEX_LENGTH == 8
    assert M4_MAX_SESSION_WRITERS == 1
    assert M4_INLINE_PAYLOAD_MAX_BYTES < M4_ARTIFACT_MAX_BYTES


def test_new_session_is_durable_and_printed_before_the_first_model_request() -> None:
    assert M4_NEW_SESSION_STARTUP_OBSERVABLES == (
        "session.created",
        "session_id.printed",
        "model.requested",
    )
    assert M4_NEW_SESSION_EVENT_PREFIX == (
        "session.started",
        "context.created",
        "model.requested",
    )
    assert M4_NEW_SESSION_STARTUP_OBSERVABLES.index("session_id.printed") < (
        M4_NEW_SESSION_STARTUP_OBSERVABLES.index("model.requested")
    )
    assert M4_NEW_SESSION_EVENT_PREFIX.index("session.started") < (
        M4_NEW_SESSION_EVENT_PREFIX.index("model.requested")
    )
    assert M4_SESSION_ID_PATTERN.fullmatch("20260714T031500Z-a1b2c3d4")
    assert not M4_SESSION_ID_PATTERN.fullmatch("../sessions/other")
    assert not M4_SESSION_ID_PATTERN.fullmatch("20260714T031500Z-abc")


def test_jsonl_event_envelope_sequence_and_hash_chain_contract() -> None:
    events = _reference_event_chain(
        "20260714T031500Z-a1b2c3d4",
        M4_NEW_SESSION_EVENT_PREFIX,
    )
    encoded = b"".join(
        _canonical_json(event).encode("utf-8") + b"\n" for event in events
    )

    assert encoded.endswith(b"\n")
    decoded = [json.loads(line) for line in encoded.decode("utf-8").splitlines()]
    assert [event["seq"] for event in decoded] == [1, 2, 3]

    previous_hash: str | None = None
    for event in decoded:
        assert set(event) == M4_REQUIRED_EVENT_FIELDS
        assert event["schema_version"] == M4_SCHEMA_VERSION
        assert event["prev_hash"] == previous_hash
        assert event["event_hash"] == _calculate_event_hash(event)
        previous_hash = event["event_hash"]


def test_required_audit_facts_and_side_effect_order_are_fixed() -> None:
    assert M4_REQUIRED_AUDIT_EVENTS == {
        "session.started",
        "context.created",
        "model.requested",
        "model.responded",
        "tool.started",
        "approval.decided",
        "tool.finished",
        "verification.recorded",
        "checkpoint.saved",
        "session.completed",
    }
    assert M4_SIDE_EFFECT_BOUNDARY == (
        "tool.started",
        "approval.decided",
        "side_effect",
        "tool.finished",
        "checkpoint.saved",
    )
    assert M4_SIDE_EFFECT_BOUNDARY.index("approval.decided") < (
        M4_SIDE_EFFECT_BOUNDARY.index("side_effect")
    )


def test_only_an_incomplete_final_jsonl_line_is_recoverable() -> None:
    assert M4_RECOVERABLE_LOG_DAMAGE == {"incomplete_final_line"}
    assert M4_FATAL_LOG_DAMAGE == {
        "invalid_utf8",
        "invalid_complete_json_line",
        "middle_line_corruption",
        "non_contiguous_sequence",
        "hash_chain_mismatch",
        "unknown_schema_version",
    }
    assert M4_RECOVERABLE_LOG_DAMAGE.isdisjoint(M4_FATAL_LOG_DAMAGE)


def test_interrupted_patch_is_reconciled_and_applied_only_once() -> None:
    assert M4_PATCH_RESUME_SCENARIO == (
        "verification.failed",
        "patch.side_effect_applied",
        "process.interrupted_before_tool_finished",
        "patch.recovered_from_after_hash",
        "verification.passed",
    )
    assert M4_PATCH_RESUME_SCENARIO.count("patch.side_effect_applied") == 1
    assert M4_PATCH_RESUME_SCENARIO.index("patch.recovered_from_after_hash") < (
        M4_PATCH_RESUME_SCENARIO.index("verification.passed")
    )


def test_unfinished_process_tools_require_explicit_reapproval() -> None:
    assert M4_UNFINISHED_PROCESS_TOOLS == {"run_command", "run_verification"}
    assert M4_AUTO_APPROVE_BYPASSES_RECOVERY is False
    assert M4_TOOL_RECOVERY_POLICY == {
        "read_only": "safe_retry",
        "workspace_write": "reconcile_file_hashes",
        "process": "explicit_reapproval",
    }
    assert M4_TOOL_RECOVERY_POLICY["process"] != "safe_retry"
    assert M4_TOOL_RECOVERY_POLICY["process"] != "auto_policy"


def test_replay_is_offline_and_has_zero_side_effect_budget() -> None:
    assert M4_REPLAY_REQUIRES_API_KEY is False
    assert M4_REPLAY_EXTERNAL_CALL_LIMITS == {
        "model_client_constructions": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "subprocess_calls": 0,
        "input_calls": 0,
    }
    assert all(limit == 0 for limit in M4_REPLAY_EXTERNAL_CALL_LIMITS.values())


def test_resume_safety_rejection_contract_is_fixed() -> None:
    assert M4_RESUME_REJECTION_REASONS == {
        "workspace_mismatch",
        "session_id_path_traversal",
        "touched_file_drift",
        "concurrent_writer",
    }


def test_sensitive_values_are_outside_the_persistence_contract() -> None:
    assert M4_FORBIDDEN_PERSISTED_FIELDS == {
        "OPENAI_API_KEY",
        "environment",
        "authorization",
        "request_headers",
    }
    assert M4_SENSITIVE_ENV_SUFFIXES == ("_TOKEN", "_SECRET", "_PASSWORD")
    assert all(M4_SENSITIVE_VALUE_FIXTURES)
    assert "OPENAI_API_KEY" not in M4_REQUIRED_EVENT_FIELDS
    assert "environment" not in M4_REQUIRED_EVENT_FIELDS


def test_m1_to_m3_regression_suites_remain_part_of_m4_acceptance() -> None:
    repository_root = Path(__file__).resolve().parents[1]

    assert M4_REQUIRED_REGRESSION_TESTS == {
        "tests/test_integration.py",
        "tests/test_m2_integration.py",
        "tests/test_m3_integration.py",
        "tests/test_m3_acceptance.py",
    }
    assert all(
        (repository_root / relative_path).is_file()
        for relative_path in M4_REQUIRED_REGRESSION_TESTS
    )


def _reference_event_chain(
    session_id: str,
    event_types: tuple[str, ...],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    previous_hash: str | None = None
    for sequence, event_type in enumerate(event_types, start=1):
        event: dict[str, Any] = {
            "schema_version": M4_SCHEMA_VERSION,
            "session_id": session_id,
            "seq": sequence,
            "event_id": f"event-{sequence:04d}",
            "recorded_at": f"2026-07-14T03:15:{sequence:02d}.000Z",
            "type": event_type,
            "prev_hash": previous_hash,
            "payload": {"contract_fixture": True},
        }
        event["event_hash"] = _calculate_event_hash(event)
        events.append(event)
        previous_hash = str(event["event_hash"])
    return events


def _calculate_event_hash(event: dict[str, Any]) -> str:
    hashable = {key: value for key, value in event.items() if key != "event_hash"}
    return hashlib.sha256(_canonical_json(hashable).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

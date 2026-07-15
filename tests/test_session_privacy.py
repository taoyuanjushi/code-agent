from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from coding_agent.sessions.codec import (
    artifact_ref_from_dict,
    canonical_json_bytes,
    session_event_to_dict,
)
from coding_agent.sessions.privacy import (
    ARTIFACT_MAX_BYTES,
    INLINE_PAYLOAD_MAX_BYTES,
    REDACTION_MARKER,
    SessionPrivacyPolicy,
)
from coding_agent.sessions.store import ArtifactTooLargeError, SessionStore
from coding_agent.types import AgentConfig


class BearerAuth:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def __repr__(self) -> str:
        return f"BearerAuth(secret={self.secret})"


class RequestHeaders(dict[str, str]):
    pass


def _config(tmp_path: Path, *, model: str = "gpt-test") -> AgentConfig:
    return AgentConfig(
        workspace=str(tmp_path.resolve()),
        model=model,
        reasoning_effort="medium",
        max_turns=8,
        permission_mode="workspace-write",
        auto_approve_commands=False,
        auto_approve_edits=True,
        context_max_files=6,
        context_max_bytes_per_file=8_000,
        max_fix_attempts=3,
    )


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def test_privacy_policy_defaults_and_limits_are_fixed() -> None:
    policy = SessionPrivacyPolicy()

    assert policy.inline_max_bytes == INLINE_PAYLOAD_MAX_BYTES == 65_536
    assert policy.artifact_max_bytes == ARTIFACT_MAX_BYTES == 4_194_304

    with pytest.raises(ValueError, match="inline_max_bytes"):
        SessionPrivacyPolicy(inline_max_bytes=0)
    with pytest.raises(ValueError, match="artifact_max_bytes"):
        SessionPrivacyPolicy(artifact_max_bytes=0)
    with pytest.raises(ValueError, match="smaller"):
        SessionPrivacyPolicy(inline_max_bytes=32, artifact_max_bytes=32)
    with pytest.raises(TypeError, match="artifact_writer"):
        policy.sanitize_payload({}, artifact_writer=object())  # type: ignore[arg-type]


def test_sanitize_config_uses_an_explicit_allowlist_and_redacts_known_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-session-config-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("UNRELATED_SETTING", "public-value")
    policy = SessionPrivacyPolicy()

    result = policy.sanitize_config(
        _config(tmp_path, model=f"model-{secret}")
    )

    assert result == {
        "workspace": str(tmp_path.resolve()),
        "model": f"model-{REDACTION_MARKER}",
        "reasoning_effort": "medium",
        "max_turns": 8,
        "permission_mode": "workspace-write",
        "auto_approve_commands": False,
        "auto_approve_edits": True,
        "context_max_files": 6,
        "context_max_bytes_per_file": 8_000,
        "max_fix_attempts": 3,
    }
    serialized = json.dumps(result, sort_keys=True)
    assert secret not in serialized
    assert "UNRELATED_SETTING" not in serialized
    assert "public-value" not in serialized


def test_current_process_environment_is_never_enumerated_into_a_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISIBLE_BUT_PRIVATE_SETTING", "do-not-persist")
    policy = SessionPrivacyPolicy()

    result = _mapping(policy.sanitize_payload({"variables": os.environ}))

    assert result["variables"] == {
        "summary_type": "process_context",
        "stored": False,
        "redacted": True,
    }
    assert "do-not-persist" not in json.dumps(result, sort_keys=True)


def test_store_removes_sensitive_fields_and_summarizes_unsafe_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = "sk-session-persist-secret"
    token = "session-token-value"
    password = "session-password-value"
    monkeypatch.setenv("OPENAI_API_KEY", api_key)
    monkeypatch.setenv("SERVICE_TOKEN", token)
    monkeypatch.setenv("DATABASE_PASSWORD", password)
    store = SessionStore(tmp_path)

    session_id = store.create(
        {
            "task": f"inspect with {api_key}",
            "config": _config(tmp_path, model=f"model-{token}"),
            "OPENAI_API_KEY": api_key,
            "environment": {"SAFE": "visible"},
            "request_headers": {"Authorization": f"Bearer {token}"},
            "authorization": f"Bearer {token}",
            "nested": {
                "password": password,
                "message": f"failed with {password}",
            },
            "error": RuntimeError(f"request failed: {api_key}"),
            "auth_object": BearerAuth(token),
            "transport": RequestHeaders({"Authorization": token}),
        }
    )
    event = store.load(session_id)[0]
    raw = (store.sessions_dir / f"{session_id}.jsonl").read_text(
        encoding="utf-8"
    )

    for forbidden in (
        api_key,
        token,
        password,
        "OPENAI_API_KEY",
        '"environment"',
        '"request_headers"',
        '"authorization"',
        "visible",
    ):
        assert forbidden not in raw
    assert event.payload["task"] == f"inspect with {REDACTION_MARKER}"

    config = _mapping(event.payload["config"])
    assert config["model"] == f"model-{REDACTION_MARKER}"

    nested = _mapping(event.payload["nested"])
    assert nested["message"] == f"failed with {REDACTION_MARKER}"
    assert "password" not in nested
    assert _mapping(nested["_privacy"])["secret_field_count"] == 1

    error = _mapping(event.payload["error"])
    assert error["summary_type"] == "exception"
    assert error["message"] == f"request failed: {REDACTION_MARKER}"

    auth = _mapping(event.payload["auth_object"])
    assert auth == {
        "summary_type": "authentication_or_header_object",
        "redacted": True,
        "object_type": "BearerAuth",
    }
    transport = _mapping(event.payload["transport"])
    assert transport == {
        "summary_type": "authentication_or_header_object",
        "redacted": True,
        "object_type": "RequestHeaders",
    }
    privacy = _mapping(event.payload["_privacy"])
    assert privacy["secret_field_count"] == 2
    assert privacy["header_object_count"] == 1
    assert privacy["process_context_count"] == 1


def test_large_tool_output_is_redacted_and_stored_as_a_text_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "large-output-secret"
    monkeypatch.setenv("BUILD_SECRET", secret)
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "capture output"})
    output = "prefix-" + secret + "\n" + ("x" * INLINE_PAYLOAD_MAX_BYTES)

    event = store.append(
        session_id,
        "tool.finished",
        {"call_id": "call-1", "output": output},
    )

    descriptor = _mapping(event.payload["output"])
    assert descriptor["stored"] is True
    assert descriptor["original_byte_count"] == len(output.encode("utf-8"))
    assert "prefix" not in str(descriptor["summary"])

    ref = artifact_ref_from_dict(_mapping(descriptor["artifact"]))
    assert ref.media_type == "text/plain"
    assert ref.encoding == "utf-8"
    content = store.get_artifact(session_id, ref).decode("utf-8")
    assert secret not in content
    assert REDACTION_MARKER in content
    assert content.endswith("x" * INLINE_PAYLOAD_MAX_BYTES)

    raw = (store.sessions_dir / f"{session_id}.jsonl").read_bytes()
    assert secret.encode() not in raw
    assert output.encode() not in raw
    inline_payload = canonical_json_bytes(
        session_event_to_dict(event)["payload"]
    )
    assert len(inline_payload) <= INLINE_PAYLOAD_MAX_BYTES


def test_large_diff_uses_diff_media_type(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "capture diff"})
    diff = "--- a/file.py\n+++ b/file.py\n" + ("+changed\n" * 9_000)

    event = store.append(session_id, "tool.started", {"patch": diff})

    descriptor = _mapping(event.payload["patch"])
    ref = artifact_ref_from_dict(_mapping(descriptor["artifact"]))
    assert ref.media_type == "text/x-diff"
    assert store.get_artifact(session_id, ref) == diff.encode("utf-8")


def test_oversized_value_is_recorded_as_omitted_without_writing_an_artifact(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "reject oversized output"})
    output = "x" * (ARTIFACT_MAX_BYTES + 1)

    event = store.append(session_id, "tool.finished", {"output": output})

    descriptor = _mapping(event.payload["output"])
    assert descriptor == {
        "stored": False,
        "original_byte_count": ARTIFACT_MAX_BYTES + 1,
        "reason": "exceeds_artifact_max_bytes",
        "summary": "tool output omitted because it exceeds the artifact limit",
    }
    artifact_dir = store.artifacts_dir / session_id
    assert not artifact_dir.exists() or not list(artifact_dir.glob("*.blob"))
    raw = (store.sessions_dir / f"{session_id}.jsonl").read_bytes()
    assert output.encode() not in raw


def test_aggregate_inline_payload_is_externalized_when_small_fields_exceed_budget(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "aggregate payload"})
    payload = {f"field_{index}": "x" * 1_000 for index in range(70)}

    event = store.append(session_id, "context.created", payload)

    assert event.payload["stored"] is True
    ref = artifact_ref_from_dict(_mapping(event.payload["artifact"]))
    stored_payload = json.loads(store.get_artifact(session_id, ref))
    assert stored_payload == payload
    inline_payload = canonical_json_bytes(
        session_event_to_dict(event)["payload"]
    )
    assert len(inline_payload) <= INLINE_PAYLOAD_MAX_BYTES


def test_binary_payloads_are_artifactized_and_known_secret_bytes_are_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "binary-token"
    monkeypatch.setenv("SERVICE_TOKEN", secret)
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "binary"})

    event = store.append(
        session_id,
        "tool.finished",
        {"data": b"before-" + secret.encode() + b"-after"},
    )

    descriptor = _mapping(event.payload["data"])
    ref = artifact_ref_from_dict(_mapping(descriptor["artifact"]))
    assert ref.media_type == "application/octet-stream"
    assert ref.encoding is None
    assert store.get_artifact(session_id, ref) == (
        b"before-" + REDACTION_MARKER.encode() + b"-after"
    )


def test_direct_artifact_writes_enforce_limits_encoding_and_redaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "key"
    monkeypatch.setenv("DIRECT_SECRET", secret)
    policy = SessionPrivacyPolicy(inline_max_bytes=16, artifact_max_bytes=32)
    store = SessionStore(tmp_path, privacy_policy=policy)
    session_id = store.create({"task": "x"})

    ref = store.put_artifact(
        session_id,
        b"before-key",
        "text/plain",
        encoding="utf-8",
    )
    assert ref.encoding == "utf-8"
    assert store.get_artifact(session_id, ref) == b"before-[REDACTED]"

    with pytest.raises(ArtifactTooLargeError, match="artifact_max_bytes"):
        store.put_artifact(session_id, b"x" * 33, "application/octet-stream")


def test_tail_repair_diagnostic_artifacts_are_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "tail-secret-value"
    monkeypatch.setenv("TAIL_SECRET", secret)
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "repair"})
    event_path = store.sessions_dir / f"{session_id}.jsonl"
    tail = f'{{"partial":"{secret}'.encode()
    with event_path.open("ab") as stream:
        stream.write(tail)

    resumed = store.load(session_id, repair_tail=True)[-1]

    diagnostic = artifact_ref_from_dict(
        _mapping(resumed.payload["diagnostic_artifact"])
    )
    assert resumed.payload["discarded_bytes"] == len(tail)
    assert store.get_artifact(session_id, diagnostic) == (
        b'{"partial":"' + REDACTION_MARKER.encode()
    )


def test_sanitize_payload_without_storage_reports_that_large_data_was_not_stored(
) -> None:
    policy = SessionPrivacyPolicy()

    result = _mapping(
        policy.sanitize_payload({"traceback": "x" * (INLINE_PAYLOAD_MAX_BYTES + 1)})
    )
    traceback = _mapping(result["traceback"])

    assert traceback["stored"] is False
    assert traceback["reason"] == "artifact_writer_unavailable"
    assert traceback["original_byte_count"] == INLINE_PAYLOAD_MAX_BYTES + 1

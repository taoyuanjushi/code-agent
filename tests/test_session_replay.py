from __future__ import annotations

import builtins
import json
import subprocess
from pathlib import Path

import pytest

import coding_agent.agent as agent_module
import coding_agent.cli as cli_module
from coding_agent.approvals import ApprovalRequest, create_approval_decision
from coding_agent.cli import main
from coding_agent.sessions.codec import (
    approval_decision_to_dict,
    approval_request_to_dict,
    artifact_ref_to_dict,
)
from coding_agent.sessions.replay import (
    build_approval_query_payload,
    build_session_replay_payload,
    rebuild_approval_projection,
)
from coding_agent.sessions.store import (
    ReadOnlySessionStoreError,
    SessionStore,
)


def _snapshot_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _append_approval(
    store: SessionStore,
    session_id: str,
    *,
    call_id: str,
    action: str,
    approved: bool,
    source: str = "interactive",
) -> None:
    request = ApprovalRequest(
        call_id=call_id,
        action=action,
        summary=f"approve {action}",
        arguments_sha256="a" * 64,
        details={"target": "example.py"},
    )
    decision = create_approval_decision(
        request,
        approved=approved,
        source=source,  # type: ignore[arg-type]
    )
    store.append(
        session_id,
        "approval.decided",
        {
            "request": approval_request_to_dict(request),
            "decision": approval_decision_to_dict(decision),
        },
    )


def test_read_only_store_never_creates_or_modifies_session_files(
    tmp_path: Path,
) -> None:
    empty_workspace = tmp_path / "empty"
    empty_workspace.mkdir()

    empty_reader = SessionStore(empty_workspace, read_only=True)

    assert empty_reader.list_sessions() == ()
    assert not (empty_workspace / ".coding-agent").exists()
    with pytest.raises(ReadOnlySessionStoreError):
        empty_reader.create({"task": "must fail"})

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    writer = SessionStore(workspace)
    session_id = writer.create({"task": "read-only replay", "workspace": str(workspace)})
    artifact = writer.put_artifact(
        session_id,
        b"artifact body",
        "text/plain",
        encoding="utf-8",
    )
    before = _snapshot_files(workspace)

    reader = SessionStore(workspace, read_only=True)

    assert reader.list_sessions()[0].session_id == session_id
    assert reader.load(session_id)[0].type == "session.started"
    assert reader.get_artifact(session_id, artifact) == b"artifact body"
    with pytest.raises(ReadOnlySessionStoreError):
        reader.append(session_id, "session.interrupted", {"reason": "must fail"})
    with pytest.raises(ReadOnlySessionStoreError):
        reader.put_artifact(session_id, b"x", "text/plain", encoding="utf-8")
    with pytest.raises(ReadOnlySessionStoreError):
        reader.load(session_id, repair_tail=True)
    with pytest.raises(ReadOnlySessionStoreError):
        with reader.exclusive_writer(session_id):
            pass

    assert _snapshot_files(workspace) == before


def test_replay_builds_auditable_summary_without_exposing_payloads(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "fix tests", "workspace": str(tmp_path)})
    store.append(
        session_id,
        "model.requested",
        {"request_kind": "initial", "turn_index": 1},
    )
    store.append(
        session_id,
        "model.responded",
        {
            "response": {
                "response_id": "resp_123",
                "text": "",
                "reasoning_summary": "",
                "function_calls": [],
            }
        },
    )
    store.append(
        session_id,
        "tool.started",
        {
            "call_id": "call-1",
            "name": "apply_patch",
            "arguments_sha256": "a" * 64,
        },
    )
    _append_approval(
        store,
        session_id,
        call_id="call-1",
        action="apply_patch",
        approved=True,
    )
    store.append(
        session_id,
        "tool.finished",
        {
            "call_id": "call-1",
            "name": "apply_patch",
            "tool_output": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": json.dumps(
                    {
                        "ok": True,
                        "output": "full diff must stay hidden by default",
                        "data": None,
                    }
                ),
            },
        },
    )
    store.append(
        session_id,
        "verification.recorded",
        {
            "result": {
                "command_id": "python:pytest",
                "kind": "test",
                "status": "failed",
                "attempt": 1,
                "duration_ms": 25,
                "exit_code": 1,
                "output": "large failure output must stay hidden by default",
            }
        },
    )
    store.append(
        session_id,
        "session.completed",
        {"report": {"answer": "done", "final_status": "failed"}},
    )

    payload = build_session_replay_payload(
        SessionStore(tmp_path, read_only=True),
        session_id,
    )

    assert payload["schema_version"] == 2
    assert payload["kind"] == "session_replay"
    assert payload["session"]["status"] == "completed"
    assert payload["session"]["final_status"] == "failed"
    assert payload["verifications"] == [
        {
            "seq": 7,
            "recorded_at": payload["verifications"][0]["recorded_at"],
            "command_id": "python:pytest",
            "kind": "test",
            "status": "failed",
            "attempt": 1,
            "duration_ms": 25,
            "exit_code": 1,
        }
    ]
    assert payload["approvals"][0]["action"] == "apply_patch"
    assert payload["approvals"][0]["outcome"] == "approved"
    finished = next(
        item for item in payload["timeline"] if item["type"] == "tool.finished"
    )
    assert finished["summary"] == "tool apply_patch -> approved(interactive) -> ok"
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "full diff must stay hidden" not in encoded
    assert "large failure output must stay hidden" not in encoded
    assert all("payload" not in item for item in payload["timeline"])


def test_verbose_replay_reads_artifacts_and_marks_missing_artifacts(
    tmp_path: Path,
) -> None:
    writer = SessionStore(tmp_path)
    session_id = writer.create({"task": "inspect artifact", "workspace": str(tmp_path)})
    artifact = writer.put_artifact(
        session_id,
        b"verbose artifact content",
        "text/plain",
        encoding="utf-8",
    )
    writer.append(
        session_id,
        "context.created",
        {
            "workspace_context": {
                "stored": True,
                "original_byte_count": artifact.byte_count,
                "summary": "context stored as artifact",
                "artifact": artifact_ref_to_dict(artifact),
            }
        },
    )

    reader = SessionStore(tmp_path, read_only=True)
    payload = build_session_replay_payload(reader, session_id, verbose=True)
    context_event = next(
        item for item in payload["timeline"] if item["type"] == "context.created"
    )

    artifact_payload = context_event["payload"]["workspace_context"]
    assert artifact_payload["artifact_content"] == {
        "available": True,
        "media_type": "text/plain",
        "encoding": "utf-8",
        "text": "verbose artifact content",
    }

    artifact_path = writer.artifacts_dir / artifact.path
    artifact_path.unlink()
    missing = build_session_replay_payload(reader, session_id, verbose=True)
    missing_event = next(
        item for item in missing["timeline"] if item["type"] == "context.created"
    )
    missing_content = missing_event["payload"]["workspace_context"][
        "artifact_content"
    ]
    assert missing_content["available"] is False
    assert missing_content["reason"] == "artifact_missing"


def test_approval_projection_rebuild_and_filters_use_session_events(
    tmp_path: Path,
) -> None:
    writer = SessionStore(tmp_path)
    first = writer.create({"task": "first", "workspace": str(tmp_path)})
    second = writer.create({"task": "second", "workspace": str(tmp_path)})
    _append_approval(
        writer,
        first,
        call_id="call-patch",
        action="apply_patch",
        approved=False,
    )
    _append_approval(
        writer,
        second,
        call_id="call-command",
        action="run_command",
        approved=True,
        source="auto_policy",
    )
    reader = SessionStore(tmp_path, read_only=True)

    first_projection = rebuild_approval_projection(reader.load(first))
    payload = build_approval_query_payload(
        reader,
        session_ids=(first, second),
        selector="all",
        action="apply_patch",
        outcome="denied",
    )

    assert len(first_projection) == 1
    assert first_projection[0]["session_id"] == first
    assert payload["schema_version"] == 1
    assert payload["kind"] == "approval_list"
    assert payload["filters"] == {
        "session": "all",
        "action": "apply_patch",
        "outcome": "denied",
    }
    assert len(payload["approvals"]) == 1
    assert payload["approvals"][0]["session_id"] == first
    assert payload["approvals"][0]["outcome"] == "denied"


def test_cli_replay_and_approval_queries_are_offline_and_zero_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer = SessionStore(tmp_path)
    session_id = writer.create({"task": "offline replay", "workspace": str(tmp_path)})
    _append_approval(
        writer,
        session_id,
        call_id="call-1",
        action="apply_patch",
        approved=True,
    )
    before = _snapshot_files(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("offline replay must not invoke models, tools, subprocesses, or input")

    monkeypatch.setattr(cli_module, "run_agent_with_report", forbidden)
    monkeypatch.setattr(cli_module, "resume_agent_with_report", forbidden)
    monkeypatch.setattr(agent_module, "OpenAIResponsesClient", forbidden)
    monkeypatch.setattr(agent_module, "execute_tool", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(builtins, "input", forbidden)

    replay_exit = main(
        ["--workspace", str(tmp_path), "--replay", "latest", "--json"]
    )
    replay_payload = json.loads(capsys.readouterr().out)
    approvals_exit = main(
        [
            "--workspace",
            str(tmp_path),
            "--approvals",
            "latest",
            "--approval-action",
            "apply_patch",
            "--approval-outcome",
            "approved",
            "--json",
        ]
    )
    approvals_payload = json.loads(capsys.readouterr().out)

    assert replay_exit == 0
    assert replay_payload["kind"] == "session_replay"
    assert approvals_exit == 0
    assert approvals_payload["kind"] == "approval_list"
    assert approvals_payload["approvals"][0]["session_id"] == session_id
    assert _snapshot_files(tmp_path) == before

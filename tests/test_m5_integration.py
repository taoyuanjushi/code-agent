"""End-to-end M5 safety checks across tools, sessions, and replay."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.process_fakes import patch_tools_runner

from coding_agent.agent import run_agent_with_report
from coding_agent.sessions.replay import build_session_replay_payload
from coding_agent.sessions.store import SessionStore
from coding_agent.types import AgentConfig


def _config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=2,
        permission_mode="workspace-write",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=6,
        context_max_bytes_per_file=4_000,
    )


class _UnsafeRequestsClient:
    def __init__(self) -> None:
        self.tool_results: list[dict[str, Any]] = []

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        return {
            "id": "response-unsafe",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-command",
                    "name": "run_command",
                    "arguments": json.dumps(
                        {"argv": ["git", "reset", "--hard", "HEAD"]}
                    ),
                },
                {
                    "type": "function_call",
                    "call_id": "call-secret",
                    "name": "read_file",
                    "arguments": json.dumps({"path": ".env"}),
                },
            ],
        }

    def create_tool_response(
        self,
        *,
        tool_outputs: list[dict[str, Any]],
        **_kwargs: object,
    ) -> dict[str, object]:
        self.tool_results = [json.loads(item["output"]) for item in tool_outputs]
        return {
            "id": "response-final",
            "output": [],
            "output_text": "Unsafe operations were blocked.",
        }


def test_denials_are_side_effect_free_persisted_and_offline_replayable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-m5-never-persist"
    (tmp_path / ".env").write_text(f"OPENAI_API_KEY={secret}\n", encoding="utf-8")
    subprocess_calls = 0

    def forbidden_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal subprocess_calls
        subprocess_calls += 1
        raise AssertionError("denied operations must not start subprocesses")

    patch_tools_runner(monkeypatch, forbidden_run)
    store = SessionStore(tmp_path)
    client = _UnsafeRequestsClient()

    report = run_agent_with_report(
        "Exercise the M5 safety boundary.",
        _config(tmp_path),
        model_client=client,
        session_store=store,
    )

    assert report.answer == "Unsafe operations were blocked."
    assert subprocess_calls == 0
    assert [result["ok"] for result in client.tool_results] == [False, False]
    assert secret not in json.dumps(client.tool_results)

    events = store.load(report.session_id)
    assert "security.policy_evaluated" in {event.type for event in events}
    assert sum(event.type == "tool.finished" for event in events) == 2

    persisted = b"".join(
        path.read_bytes()
        for path in (tmp_path / ".coding-agent").rglob("*")
        if path.is_file()
    )
    assert secret.encode() not in persisted

    replay = build_session_replay_payload(
        SessionStore(tmp_path, read_only=True),
        report.session_id,
    )
    assert replay["session"]["status"] == "completed"  # type: ignore[index]
    assert len(replay["timeline"]) == len(events)  # type: ignore[arg-type]

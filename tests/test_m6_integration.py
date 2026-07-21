from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from coding_agent.agent import run_agent_with_report
from coding_agent.security.models import (
    CommandPolicyDecision,
    CommandSpec,
    ExecutionLimits,
)
from coding_agent.security.process_runner import HostProcessRunner
from coding_agent.sessions.replay import build_session_replay_payload
from coding_agent.sessions.store import SessionStore
from coding_agent.types import AgentConfig
from coding_agent.ui import JsonlRenderer, TerminalRenderer, UiEmitter, UiEvent


class _TrackingStringIO(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


def _config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace.resolve()),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="read-only",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=8,
        context_max_bytes_per_file=8_000,
    )


def test_streaming_approval_and_secret_redaction_share_one_event_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-m6-step14-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("NO_COLOR", "1")
    terminal_stdout = _TrackingStringIO()
    terminal_stderr = _TrackingStringIO()
    jsonl_stdout = _TrackingStringIO()
    jsonl_stderr = _TrackingStringIO()
    terminal = TerminalRenderer(
        stdout=terminal_stdout,
        stderr=terminal_stderr,
        stdin=io.StringIO(),
        is_tty=True,
    )
    jsonl = JsonlRenderer(stdout=jsonl_stdout, stderr=jsonl_stderr)
    observed: list[UiEvent] = []

    def render(event: UiEvent) -> None:
        observed.append(event)
        terminal(event)
        jsonl(event)

    emitter = UiEmitter(render)
    emitter.emit("run.started", {"mode": "run"})
    emitter.emit("model.output.delta", {"text": "partial output"})
    emitter.emit(
        "approval.requested",
        {
            "call_id": "call-approval",
            "action": "apply_patch",
            "arguments_sha256": "a" * 64,
            "message": "Approve patch?",
        },
    )
    emitter.emit(
        "approval.decided",
        {
            "call_id": "call-approval",
            "action": "apply_patch",
            "arguments_sha256": "a" * 64,
            "decision": "approved",
        },
    )
    emitter.emit(
        "tool.finished",
        {
            "call_id": "call-tool",
            "name": "run_command",
            "status": "failed",
            "duration_ms": 1,
            "summary": f"tool failed while using {secret}",
            "output_truncated": False,
        },
    )
    emitter.emit("run.failed", {"message": f"request failed for {secret}"})

    terminal_text = terminal_stdout.getvalue() + terminal_stderr.getvalue()
    jsonl_text = jsonl_stdout.getvalue()
    decoded = [json.loads(line) for line in jsonl_text.splitlines()]

    assert "partial output\nApprove patch?" in terminal_stdout.getvalue()
    assert terminal_stdout.flush_count >= len(observed)
    assert "\x1b" not in terminal_text
    assert secret not in terminal_text
    assert secret not in jsonl_text
    assert jsonl_stderr.getvalue() == ""
    assert len(decoded) == len(observed)
    assert [event["seq"] for event in decoded] == list(
        range(1, len(observed) + 1)
    )
    requested = next(
        event for event in decoded if event["type"] == "approval.requested"
    )
    decided = next(
        event for event in decoded if event["type"] == "approval.decided"
    )
    for field in ("call_id", "action", "arguments_sha256"):
        assert requested["payload"][field] == decided["payload"][field]


@pytest.mark.parametrize("platform_name", ["posix", "nt"])
def test_cross_platform_host_runner_preserves_argv_without_shell(
    tmp_path: Path,
    platform_name: str,
) -> None:
    captured: dict[str, object] = {}
    argv = (
        sys.executable,
        "-c",
        "print('not executed')",
        "value with spaces",
        "a&b",
        "$(touch should-not-exist)",
    )

    class _Process:
        pid = 1234
        returncode = 0
        stdout = io.BytesIO(b"ok\n")
        stderr = io.BytesIO()

        def wait(self, timeout: float | None = None) -> int:
            captured["wait_timeout"] = timeout
            return 0

        def kill(self) -> None:
            raise AssertionError("successful fake process must not be killed")

    def popen_factory(received_argv: list[str], **kwargs: object) -> _Process:
        captured["argv"] = received_argv
        captured.update(kwargs)
        return _Process()

    command = CommandSpec(
        argv=argv,
        cwd=".",
        source="internal",
        purpose="cross-platform argv acceptance",
        limits=ExecutionLimits(
            timeout_ms=30_000,
            max_output_bytes=32 * 1024,
            max_output_lines=200,
        ),
    )
    decision = CommandPolicyDecision(
        disposition="allow_host",
        rule_id="test.allow-host",
        reasons=("acceptance test",),
        normalized_executable="python",
        requires_approval=False,
        requires_sandbox=False,
    )

    result = HostProcessRunner(
        popen_factory=popen_factory,
        platform_name=platform_name,
    ).run(tmp_path, command, decision, environment={})

    assert result.status == "passed"
    assert captured["argv"] == list(argv)
    assert captured["shell"] is False
    assert result.to_dict()["argv"] == list(argv)
    assert result.to_dict()["shell"] is False
    if platform_name == "posix":
        assert captured["start_new_session"] is True
        assert "creationflags" not in captured
    else:
        assert "start_new_session" not in captured
        assert captured["creationflags"] == getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0,
        )


class _InterruptingClient:
    def create_initial_response(self, **_kwargs: object) -> object:
        raise KeyboardInterrupt("cancel model stream")

    def create_tool_response(self, **_kwargs: object) -> object:
        raise AssertionError("interrupted initial request has no continuation")


def test_model_interrupt_is_durable_and_immediately_replayable(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)

    with pytest.raises(KeyboardInterrupt, match="cancel model stream"):
        run_agent_with_report(
            "interrupt the model",
            _config(tmp_path),
            model_client=_InterruptingClient(),
            session_store=store,
            stream=False,
        )

    session_id = store.list_sessions()[0].session_id
    replay = build_session_replay_payload(
        SessionStore(tmp_path, read_only=True),
        session_id,
    )

    terminal = replay["terminal"]
    assert isinstance(terminal, dict)
    assert terminal["status"] == "interrupted"
    assert terminal["event_type"] == "session.interrupted"
    assert terminal["reason"] == "keyboard_interrupt"
    assert isinstance(terminal["seq"], int)
    assert isinstance(terminal["recorded_at"], str)
    assert replay["session"]["status"] == "interrupted"

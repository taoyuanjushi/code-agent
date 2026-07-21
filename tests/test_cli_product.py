from __future__ import annotations

import json
from pathlib import Path

import pytest

import coding_agent.cli as cli_module
from coding_agent.agent import AgentRunReport
from coding_agent.sessions.store import SessionStore
from coding_agent.ui import UiEmitter


def _fake_successful_run(
    task: str,
    config: object,
    **kwargs: object,
) -> AgentRunReport:
    del task, config
    emitter = kwargs["ui_emitter"]
    assert isinstance(emitter, UiEmitter)
    emitter.emit("model.output.delta", {"text": "streamed answer"})
    emitter.emit(
        "tool.finished",
        {
            "call_id": "call-product",
            "name": "read_file",
            "status": "completed",
            "duration_ms": 2,
            "summary": "read completed",
            "output_truncated": False,
        },
    )
    report = AgentRunReport(
        answer="streamed answer",
        verifications=(),
        final_status="not_run",
        session_id="session-product",
    )
    emitter.emit(
        "run.finished",
        {
            "status": "completed",
            "final_status": report.final_status,
            "session_id": report.session_id,
            "answer": report.answer,
        },
    )
    return report


def test_jsonl_cli_stdout_contains_only_complete_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli_module, "run_agent_with_report", _fake_successful_run)

    exit_code = cli_module.main(
        [
            "--workspace",
            str(tmp_path),
            "--sandbox",
            "none",
            "--output",
            "jsonl",
            "inspect",
        ]
    )

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    events = [json.loads(line) for line in lines]

    assert exit_code == 0
    assert captured.err == ""
    assert len(lines) == 4
    assert [event["type"] for event in events] == [
        "run.started",
        "model.output.delta",
        "tool.finished",
        "run.finished",
    ]
    assert [event["seq"] for event in events] == [1, 2, 3, 4]
    assert all(
        set(event) == {"schema_version", "seq", "type", "payload"}
        for event in events
    )
    assert "\x1b" not in captured.out


def test_redirected_human_cli_is_line_oriented_and_control_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli_module, "run_agent_with_report", _fake_successful_run)

    exit_code = cli_module.main(
        [
            "--workspace",
            str(tmp_path),
            "--sandbox",
            "none",
            "--output",
            "human",
            "--no-color",
            "inspect",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert "run.started" in captured.out
    assert "model.output.delta" in captured.out
    assert "tool.finished" in captured.out
    assert "run.finished" in captured.out
    assert all(token not in captured.out for token in ("\x1b", "\r", "\b"))
    assert all(line for line in captured.out.splitlines())


def test_jsonl_keyboard_interrupt_returns_130_with_one_terminal_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupt(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt("stop")

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli_module, "run_agent_with_report", interrupt)

    exit_code = cli_module.main(
        [
            "--workspace",
            str(tmp_path),
            "--sandbox",
            "none",
            "--output",
            "jsonl",
            "inspect",
        ]
    )

    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines()]

    assert exit_code == 130
    assert captured.err == ""
    assert [event["type"] for event in events] == [
        "run.started",
        "run.interrupted",
    ]


def test_legacy_session_without_mode_or_plan_remains_replayable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create(
        {
            "task": "legacy session",
            "workspace": str(tmp_path.resolve()),
        }
    )

    exit_code = cli_module.main(
        [
            "--workspace",
            str(tmp_path),
            "--replay",
            session_id,
            "--json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert payload["session"]["session_id"] == session_id
    assert payload["plan"] == {"explanation": "", "items": []}
    assert payload["plan_updates"] == []
    assert payload["timeline"][0]["type"] == "session.started"

from __future__ import annotations

import builtins
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import coding_agent.cli as cli_module
from coding_agent.agent import AgentRunReport, run_agent_with_report
from coding_agent.sessions.store import SessionStore
from coding_agent.ui import UI_SCHEMA_VERSION, UiEmitter


def _add_patch(path: str = "jsonl-created.txt") -> str:
    return "\n".join(
        [
            "--- /dev/null",
            f"+++ b/{path}",
            "@@ -0,0 +1 @@",
            "+created through JSONL approval",
            "",
        ]
    )


class _ApprovalThenFinalClient:
    def __init__(self, emitter: UiEmitter, patch: str) -> None:
        self.emitter = emitter
        self.patch = patch

    def create_initial_response(self, **_kwargs: object) -> dict[str, Any]:
        self.emitter.emit("model.output.delta", {"text": "live delta"})
        return {
            "id": "response-jsonl-tool",
            "output": [
                {
                    "type": "function_call",
                    "name": "apply_patch",
                    "arguments": json.dumps({"patch": self.patch}),
                    "call_id": "call-jsonl-approval",
                }
            ],
        }

    def create_tool_response(self, **_kwargs: object) -> dict[str, object]:
        return {
            "id": "response-jsonl-final",
            "output": [],
            "output_text": "JSONL task complete",
        }


def _jsonl_lines(output: str) -> list[dict[str, object]]:
    lines = output.splitlines()
    assert lines
    return [json.loads(line) for line in lines]


def test_parser_accepts_live_output_and_color_options() -> None:
    parser = cli_module.build_parser()

    human = parser.parse_args(["--output", "human", "task"])
    jsonl = parser.parse_args(
        ["--output", "jsonl", "--no-color", "--no-stream", "task"]
    )

    assert human.output == "human"
    assert human.no_color is False
    assert jsonl.output == "jsonl"
    assert jsonl.no_color is True
    assert jsonl.no_stream is True


def test_new_task_jsonl_streams_events_and_routes_approval_prompt_to_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, object] = {}
    patch = _add_patch()

    def run_with_fake_client(
        task: str,
        config,
        **kwargs: object,
    ) -> AgentRunReport:
        observed["task"] = task
        observed["stream"] = kwargs["stream"]
        observed["approval_input_reader"] = kwargs.get(
            "approval_input_reader"
        )
        emitter = kwargs["ui_emitter"]
        assert isinstance(emitter, UiEmitter)
        return run_agent_with_report(
            task,
            config,
            model_client=_ApprovalThenFinalClient(emitter, patch),
            ui_emitter=emitter,
            stream=kwargs["stream"],
            approval_input_reader=kwargs.get("approval_input_reader"),
        )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli_module, "run_agent_with_report", run_with_fake_client)
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    monkeypatch.setattr(
        builtins,
        "input",
        lambda *_args, **_kwargs: pytest.fail(
            "JSONL approval must read through the injected stdin reader"
        ),
    )

    exit_code = cli_module.main(
        [
            "--workspace",
            str(tmp_path),
            "--sandbox",
            "none",
            "--write",
            "--output",
            "jsonl",
            "apply the patch",
        ]
    )

    captured = capsys.readouterr()
    events = _jsonl_lines(captured.out)
    event_types = [event["type"] for event in events]

    assert exit_code == 0
    assert observed["task"] == "apply the patch"
    assert observed["stream"] is True
    assert callable(observed["approval_input_reader"])
    assert [event["seq"] for event in events] == list(
        range(1, len(events) + 1)
    )
    assert all(
        event["schema_version"] == UI_SCHEMA_VERSION for event in events
    )
    assert event_types[0] == "run.started"
    assert "model.output.delta" in event_types
    assert event_types.index("approval.requested") < event_types.index(
        "approval.decided"
    )
    assert event_types[-1] == "run.finished"
    assert "\x1b[" not in captured.out
    assert "Apply patch? [y/N] " in captured.err
    assert "Apply patch in" not in captured.err
    assert (tmp_path / "jsonl-created.txt").read_text(encoding="utf-8") == (
        "created through JSONL approval\n"
    )

    requested = next(
        event for event in events if event["type"] == "approval.requested"
    )
    decided = next(
        event for event in events if event["type"] == "approval.decided"
    )
    finished = events[-1]
    assert requested["payload"]["action"] == "apply_patch"  # type: ignore[index]
    assert decided["payload"]["outcome"] == "approved"  # type: ignore[index]
    assert finished["payload"]["status"] == "completed"  # type: ignore[index]
    assert finished["payload"]["final_status"] == "not_run"  # type: ignore[index]
    assert finished["payload"]["answer"] == "JSONL task complete"  # type: ignore[index]
    assert isinstance(finished["payload"]["session_id"], str)  # type: ignore[index]


def test_jsonl_respects_explicit_no_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, object] = {}

    def fake_run(task: str, config, **kwargs: object) -> AgentRunReport:
        observed.update(kwargs)
        emitter = kwargs["ui_emitter"]
        assert isinstance(emitter, UiEmitter)
        report = AgentRunReport(
            answer="done",
            verifications=(),
            final_status="not_run",
            session_id="session-no-stream",
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

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli_module, "run_agent_with_report", fake_run)

    exit_code = cli_module.main(
        [
            "--workspace",
            str(tmp_path),
            "--sandbox",
            "none",
            "--output",
            "jsonl",
            "--no-stream",
            "finish without streaming",
        ]
    )

    events = _jsonl_lines(capsys.readouterr().out)
    assert exit_code == 0
    assert observed["stream"] is False
    assert callable(observed["approval_input_reader"])
    assert [event["type"] for event in events] == [
        "run.started",
        "run.finished",
    ]


def test_resume_supports_jsonl_without_changing_session_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create(
        {"task": "resume in JSONL", "workspace": str(tmp_path)}
    )
    observed: dict[str, object] = {}

    def fake_resume(
        selected_session_id: str,
        workspace: Path,
        *,
        session_store: SessionStore,
        ui_emitter: UiEmitter,
        stream: bool,
        approval_input_reader,
    ) -> AgentRunReport:
        observed.update(
            {
                "session_id": selected_session_id,
                "workspace": workspace,
                "store": session_store,
                "stream": stream,
                "approval_input_reader": approval_input_reader,
            }
        )
        report = AgentRunReport(
            answer="resumed",
            verifications=(),
            final_status="not_run",
            session_id=selected_session_id,
        )
        ui_emitter.emit(
            "run.finished",
            {
                "status": "completed",
                "final_status": report.final_status,
                "session_id": report.session_id,
                "answer": report.answer,
            },
        )
        return report

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli_module, "resume_agent_with_report", fake_resume)

    exit_code = cli_module.main(
        [
            "--workspace",
            str(tmp_path),
            "--resume",
            "latest",
            "--output",
            "jsonl",
        ]
    )

    captured = capsys.readouterr()
    events = _jsonl_lines(captured.out)
    assert exit_code == 0
    assert observed["session_id"] == session_id
    assert observed["workspace"] == tmp_path.resolve()
    assert observed["stream"] is True
    assert callable(observed["approval_input_reader"])
    assert events[0]["type"] == "run.started"
    assert events[0]["payload"]["session_id"] == session_id  # type: ignore[index]
    assert events[-1]["payload"]["session_id"] == session_id  # type: ignore[index]
    assert captured.err == ""


def test_no_color_disables_human_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_renderer(*, color_enabled: bool):
        observed["color_enabled"] = color_enabled
        return lambda _event: None

    monkeypatch.setattr(cli_module, "TerminalRenderer", fake_renderer)
    args = cli_module.build_parser().parse_args(["--no-color", "task"])

    emitter = cli_module._build_live_emitter(args)
    emitter.emit("run.started", {})

    assert observed["color_enabled"] is False


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--list-sessions", "--output", "human"], "--output is only"),
        (["--replay", "latest", "--output", "jsonl"], "--output is only"),
        (["--approvals", "--no-color"], "--no-color is only"),
        (["--json", "task"], "--json is only supported"),
        (
            ["--resume", "latest", "--json"],
            "--json is only supported",
        ),
    ],
)
def test_live_and_query_output_options_are_not_mixed(
    argv: list[str],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli_module.main(argv)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert message in captured.err


def test_query_json_remains_one_json_document(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli_module.main(
        ["--workspace", str(tmp_path), "--list-sessions", "--json"]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["workspace"] == str(tmp_path.resolve())
    assert payload["sessions"] == []
    assert captured.out.count("\n") == 1
    assert captured.err == ""

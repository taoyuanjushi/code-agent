from __future__ import annotations

import json
from pathlib import Path

import pytest

import coding_agent.cli as cli_module
from coding_agent.agent import AgentRunReport
from coding_agent.cli import build_parser, main
from coding_agent.sessions.query import resolve_session_selector
from coding_agent.sessions.store import SessionStore, SessionSummary


def test_parser_accepts_session_modes_without_a_task() -> None:
    parser = build_parser()

    resume = parser.parse_args(["--resume", "latest"])
    replay = parser.parse_args(["--replay", "latest", "--json"])
    listing = parser.parse_args(["--list-sessions"])
    approvals = parser.parse_args(
        [
            "--approvals",
            "latest",
            "--approval-action",
            "apply_patch",
            "--approval-outcome",
            "approved",
            "--json",
        ]
    )
    all_approvals = parser.parse_args(["--approvals"])

    assert resume.task == []
    assert resume.resume == "latest"
    assert replay.replay == "latest"
    assert replay.json is True
    assert listing.list_sessions is True
    assert approvals.approvals == "latest"
    assert approvals.approval_action == "apply_patch"
    assert approvals.approval_outcome == "approved"
    assert approvals.json is True
    assert all_approvals.approvals == "all"


def test_parser_rejects_multiple_session_modes() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--resume", "latest", "--list-sessions"])
    with pytest.raises(SystemExit) as approvals_exc:
        parser.parse_args(["--replay", "latest", "--approvals"])

    assert exc_info.value.code == 2
    assert approvals_exc.value.code == 2


def test_list_sessions_runs_without_api_key_or_agent_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "离线列表", "workspace": str(tmp_path)})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        cli_module,
        "run_agent_with_report",
        lambda *_args, **_kwargs: pytest.fail("new agent must not run"),
    )
    monkeypatch.setattr(
        cli_module,
        "resume_agent_with_report",
        lambda *_args, **_kwargs: pytest.fail("resume agent must not run"),
    )

    exit_code = main(["--workspace", str(tmp_path), "--list-sessions", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["kind"] == "session_list"
    assert payload["workspace"] == str(tmp_path.resolve())
    assert payload["sessions"][0]["session_id"] == session_id
    assert payload["sessions"][0]["task"] == "离线列表"


def test_replay_runs_without_api_key_and_emits_metadata_only_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "offline replay", "workspace": str(tmp_path)})
    store.append(session_id, "session.interrupted", {"reason": "test"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        cli_module,
        "run_agent_with_report",
        lambda *_args, **_kwargs: pytest.fail("new agent must not run"),
    )
    monkeypatch.setattr(
        cli_module,
        "resume_agent_with_report",
        lambda *_args, **_kwargs: pytest.fail("resume agent must not run"),
    )

    exit_code = main(
        ["--workspace", str(tmp_path), "--replay", session_id, "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "session_replay"
    assert payload["session"]["session_id"] == session_id
    assert payload["schema_version"] == 2
    assert payload["session"]["status"] == "interrupted"
    assert payload["session"]["event_count"] == 2
    assert payload["verbose"] is False
    assert [item["type"] for item in payload["timeline"]] == [
        "session.started",
        "session.interrupted",
    ]
    assert [item["summary"] for item in payload["timeline"]] == [
        "session started",
        "session interrupted -> test",
    ]
    assert all("payload" not in item for item in payload["timeline"])


def test_latest_uses_updated_event_time_then_session_id() -> None:
    older = SessionSummary(
        session_id="20260715T010000Z-aaaaaaaa",
        task="older",
        status="running",
        event_count=1,
        started_at="2026-07-15T01:00:00.000Z",
        updated_at="2026-07-15T01:10:00.000Z",
        last_event_type="session.started",
    )
    tied_low = SessionSummary(
        session_id="20260715T020000Z-bbbbbbbb",
        task="tie low",
        status="running",
        event_count=2,
        started_at="2026-07-15T02:00:00.000Z",
        updated_at="2026-07-15T03:00:00.000Z",
        last_event_type="checkpoint.saved",
    )
    tied_high = SessionSummary(
        session_id="20260715T020000Z-cccccccc",
        task="tie high",
        status="running",
        event_count=2,
        started_at="2026-07-15T02:00:00.000Z",
        updated_at="2026-07-15T03:00:00.000Z",
        last_event_type="checkpoint.saved",
    )

    class FakeStore:
        workspace = Path("C:/workspace")

        def list_sessions(self) -> tuple[SessionSummary, ...]:
            return tied_low, older, tied_high

    assert resolve_session_selector(FakeStore(), "latest") == tied_high.session_id


def test_resume_emits_resolved_session_before_calling_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "resume me", "workspace": str(tmp_path)})
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed: dict[str, object] = {}

    def fake_resume(
        selected_session_id: str,
        workspace: Path,
        *,
        session_store: SessionStore,
        ui_emitter,
        stream: bool,
    ) -> AgentRunReport:
        printed_before_call = capsys.readouterr().out
        assert "run.started" in printed_before_call
        assert session_id in printed_before_call
        observed["session_id"] = selected_session_id
        observed["workspace"] = workspace
        observed["store"] = session_store
        observed["stream"] = stream
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

    monkeypatch.setattr(cli_module, "resume_agent_with_report", fake_resume)

    exit_code = main(
        ["--workspace", str(tmp_path), "--resume", "latest", "--no-stream"]
    )

    assert exit_code == 0
    assert observed["session_id"] == session_id
    assert observed["workspace"] == tmp_path.resolve()
    assert isinstance(observed["store"], SessionStore)
    assert observed["stream"] is False
    final_output = capsys.readouterr().out
    assert "resumed" in final_output
    assert "run.finished" in final_output


def test_resume_rejects_new_task_permission_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        cli_module,
        "resume_agent_with_report",
        lambda *_args, **_kwargs: pytest.fail("resume must not start"),
    )

    exit_code = main(
        ["--workspace", str(tmp_path), "--resume", "latest", "--write"]
    )

    assert exit_code == 2
    assert "--write may only be used when starting a new task" in capsys.readouterr().err


def test_session_mode_rejects_a_positional_task(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    exit_code = main(["--resume", "latest", "unexpected task"])

    assert exit_code == 2
    assert "A task cannot be combined" in capsys.readouterr().err


def test_cli_requires_exactly_one_new_task_or_session_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    exit_code = main([])

    assert exit_code == 2
    assert "Provide a task or one of" in capsys.readouterr().err


def test_query_filters_are_restricted_to_their_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    verbose_exit = main(
        ["--workspace", str(tmp_path), "--list-sessions", "--verbose"]
    )
    verbose_error = capsys.readouterr().err
    approval_exit = main(
        [
            "--workspace",
            str(tmp_path),
            "--replay",
            "latest",
            "--approval-action",
            "apply_patch",
        ]
    )
    approval_error = capsys.readouterr().err

    assert verbose_exit == 2
    assert "--verbose is only supported with --replay" in verbose_error
    assert approval_exit == 2
    assert "only supported with --approvals" in approval_error

from __future__ import annotations

import io
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

import coding_agent.agent as agent_module
import coding_agent.cli as cli_module
from coding_agent.agent import (
    SessionSecurityDriftError,
    resume_agent_with_report,
    run_agent_with_report,
)
from coding_agent.security.process_runner import HostProcessRunner
from coding_agent.sessions.reducer import rebuild_state
from coding_agent.sessions.replay import build_session_replay_payload
from coding_agent.sessions.store import SessionStore
from coding_agent.types import AgentConfig
from coding_agent.ui import UiEmitter


def _config(
    workspace: Path,
    *,
    task_mode: str = "run",
) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace.resolve()),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=5,
        permission_mode="read-only",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=6,
        context_max_bytes_per_file=4_000,
        sandbox_mode="none",
        full_auto=False,
        task_mode=cast(Any, task_mode),
    )


def _function_call(
    call_id: str,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
    }


def _final_response(response_id: str = "response-final") -> dict[str, object]:
    return {
        "id": response_id,
        "output": [],
        "output_text": "done",
    }


def _latest_session_id(store: SessionStore) -> str:
    return store.list_sessions()[0].session_id


class _FinalClient:
    def __init__(self, response_id: str = "response-final") -> None:
        self.response_id = response_id
        self.initial_calls = 0

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        self.initial_calls += 1
        return _final_response(self.response_id)

    def create_tool_response(self, **_kwargs: object) -> object:
        raise AssertionError("no continuation should be requested")


class _NoModelCallsClient:
    def create_initial_response(self, **_kwargs: object) -> object:
        raise AssertionError("preflight failure must not call the model")

    def create_tool_response(self, **_kwargs: object) -> object:
        raise AssertionError("preflight failure must not call the model")


class _PlanThenFinalClient:
    def __init__(self) -> None:
        self.initial_calls = 0
        self.continuation_calls = 0

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        self.initial_calls += 1
        return {
            "id": "response-plan",
            "output": [
                _function_call(
                    "call-plan",
                    "update_plan",
                    {
                        "explanation": "Track resume progress",
                        "items": [
                            {"step": "inspect state", "status": "in_progress"},
                            {"step": "finish work", "status": "pending"},
                        ],
                    },
                )
            ],
        }

    def create_tool_response(self, **_kwargs: object) -> dict[str, object]:
        self.continuation_calls += 1
        return _final_response("response-after-plan")


class _ContinuationFinalClient:
    def __init__(self) -> None:
        self.initial_calls = 0
        self.continuation_calls = 0

    def create_initial_response(self, **_kwargs: object) -> object:
        self.initial_calls += 1
        raise AssertionError("resume must continue the persisted response")

    def create_tool_response(self, **_kwargs: object) -> dict[str, object]:
        self.continuation_calls += 1
        return _final_response("response-resumed")


class _PlanReviewClient:
    def __init__(self) -> None:
        self.continuation_calls = 0

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        return {
            "id": "response-plan",
            "output": [
                _function_call(
                    "call-plan",
                    "update_plan",
                    {
                        "explanation": "Review the durable changes",
                        "items": [
                            {"step": "inspect diff", "status": "completed"},
                            {"step": "submit findings", "status": "in_progress"},
                        ],
                    },
                )
            ],
        }

    def create_tool_response(self, **_kwargs: object) -> dict[str, object]:
        self.continuation_calls += 1
        if self.continuation_calls == 1:
            return {
                "id": "response-review",
                "output": [
                    _function_call(
                        "call-review",
                        "submit_review",
                        {
                            "summary": "Reviewed the current workspace.",
                            "findings": [
                                {
                                    "severity": "high",
                                    "path": "example.py",
                                    "line": 2,
                                    "title": "Unchecked operation",
                                    "detail": "Validate the operation before using it.",
                                }
                            ],
                        },
                    )
                ],
            }
        return {
            "id": "response-final",
            "output": [],
            "output_text": "Structured review submitted.",
        }


class _ListThenFinalClient:
    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        return {
            "id": "response-list",
            "output": [_function_call("call-list", "list_files", {"path": "."})],
        }

    def create_tool_response(self, **_kwargs: object) -> dict[str, object]:
        return _final_response("response-list-final")


def test_usage_and_configuration_errors_exit_two_without_starting_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        cli_module,
        "run_agent_with_report",
        lambda *_args, **_kwargs: pytest.fail("agent must not start"),
    )

    usage_exit = cli_module.main(["--workspace", str(tmp_path)])
    usage_error = capsys.readouterr().err
    config_exit = cli_module.main(
        ["--workspace", str(tmp_path), "--sandbox", "none", "inspect"]
    )
    config_error = capsys.readouterr().err

    assert usage_exit == 2
    assert "Provide a task" in usage_error
    assert config_exit == 2
    assert "OPENAI_API_KEY is not set" in config_error
    assert not (tmp_path / ".coding-agent" / "sessions").exists()


def test_resume_preflight_failure_is_durably_failed_without_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)

    def interrupt_after_response(point: str) -> None:
        if point == "after_model_response":
            raise KeyboardInterrupt("pause")

    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "pause before completion",
            _config(tmp_path),
            model_client=_FinalClient(),
            session_store=store,
            fault_injector=interrupt_after_response,
        )
    session_id = _latest_session_id(store)

    def reject_preflight(*_args: object, **_kwargs: object) -> None:
        raise SessionSecurityDriftError("security policy changed")

    monkeypatch.setattr(agent_module, "_validate_resume_security", reject_preflight)

    with pytest.raises(SessionSecurityDriftError, match="policy changed"):
        resume_agent_with_report(
            session_id,
            tmp_path,
            model_client=_NoModelCallsClient(),
            session_store=store,
        )

    events = store.load(session_id)
    assert [event.type for event in events[-2:]] == [
        "session.resumed",
        "session.failed",
    ]
    assert events[-2].payload["reason"] == "resume_preflight"
    assert events[-2].payload["retry_pending_model_request"] is False
    assert events[-1].payload["reason"] == "preflight_failure"
    assert events[-1].payload["error_code"] == "session_security_drift"
    assert rebuild_state(events).status == "failed"


def test_interrupted_unrecorded_model_response_retries_at_least_once(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)

    class InterruptingClient:
        def create_initial_response(self, **_kwargs: object) -> object:
            raise KeyboardInterrupt("stream interrupted before completion")

        def create_tool_response(self, **_kwargs: object) -> object:
            raise AssertionError("no continuation should be requested")

    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "inspect",
            _config(tmp_path),
            model_client=InterruptingClient(),
            session_store=store,
        )

    session_id = _latest_session_id(store)
    interrupted_events = store.load(session_id)
    original_request = next(
        event for event in interrupted_events if event.type == "model.requested"
    )
    assert not any(event.type == "model.responded" for event in interrupted_events)
    assert rebuild_state(interrupted_events).model_request_pending is True

    report = resume_agent_with_report(
        session_id,
        tmp_path,
        model_client=_FinalClient("response-retried"),
        session_store=store,
    )

    assert report.answer == "done"
    events = store.load(session_id)
    requests = [event for event in events if event.type == "model.requested"]
    responses = [event for event in events if event.type == "model.responded"]
    assert len(requests) == 2
    assert requests[-1].payload["retry_of_seq"] == original_request.seq
    assert (
        requests[-1].payload["delivery_semantics"]
        == "at_least_once_after_unrecorded_response"
    )
    assert len(responses) == 1
    response = responses[0].payload["response"]
    assert isinstance(response, Mapping)
    assert response["response_id"] == "response-retried"
    assert rebuild_state(events).status == "completed"


def test_resume_banner_reports_mode_plan_security_and_previous_phase(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    interrupted = False

    def interrupt_after_plan(point: str) -> None:
        nonlocal interrupted
        if point == "after_tool_finished" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("pause after plan")

    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "plan work",
            _config(tmp_path),
            model_client=_PlanThenFinalClient(),
            session_store=store,
            fault_injector=interrupt_after_plan,
        )

    events = []
    resumed_client = _ContinuationFinalClient()
    resume_agent_with_report(
        _latest_session_id(store),
        tmp_path,
        model_client=resumed_client,
        session_store=store,
        ui_emitter=UiEmitter(events.append),
    )

    started = events[0]
    assert started.type == "run.started"
    assert started.payload["task_mode"] == "run"
    assert started.payload["permission"] == "read-only"
    assert started.payload["sandbox"] == "none"
    assert started.payload["previous_status"] == "interrupted"
    assert isinstance(started.payload["previous_phase"], str)
    assert started.payload["plan_progress"] == {
        "completed": 0,
        "in_progress": 1,
        "pending": 1,
        "total": 2,
    }
    assert resumed_client.initial_calls == 0
    assert resumed_client.continuation_calls == 1


def test_replay_projects_plan_review_and_durable_terminal_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "example.py").write_text("first\nsecond\n", encoding="utf-8")
    store = SessionStore(tmp_path)
    run_agent_with_report(
        "review changes",
        _config(tmp_path, task_mode="review"),
        model_client=_PlanReviewClient(),
        session_store=store,
    )

    payload = build_session_replay_payload(
        SessionStore(tmp_path, read_only=True),
        _latest_session_id(store),
    )

    plan_updates = payload["plan_updates"]
    review = payload["review"]
    terminal = payload["terminal"]
    timeline = payload["timeline"]
    assert isinstance(plan_updates, list)
    assert isinstance(review, dict)
    assert isinstance(terminal, dict)
    assert isinstance(timeline, list)
    assert payload["schema_version"] == 2
    assert len(plan_updates) == 1
    assert review["findings"][0]["path"] == "example.py"
    assert terminal["status"] == "completed"
    assert terminal["event_type"] == "session.completed"
    assert all(item["type"] != "model.output.delta" for item in timeline)

    cli_module._print_session_replay(payload, as_json=False)
    output = capsys.readouterr().out
    assert "terminal: completed (session.completed)" in output
    assert "verification status: not_run" in output
    assert "plan updates:" in output
    assert "[x] inspect diff" in output
    assert "review:" in output
    assert "[high] example.py:2 Unchecked operation" in output


def test_broken_stdout_keeps_agent_facts_and_does_not_retry_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    tool_calls = 0
    output_calls = 0
    real_execute_tool = agent_module.execute_tool

    def count_execute_tool(*args: object, **kwargs: object):
        nonlocal tool_calls
        tool_calls += 1
        return real_execute_tool(*args, **kwargs)

    def broken_output(_event: object) -> None:
        nonlocal output_calls
        output_calls += 1
        raise BrokenPipeError("consumer closed stdout")

    monkeypatch.setattr(agent_module, "execute_tool", count_execute_tool)
    emitter = UiEmitter(broken_output)

    report = run_agent_with_report(
        "list files",
        _config(tmp_path),
        model_client=_ListThenFinalClient(),
        session_store=store,
        ui_emitter=emitter,
    )

    events = store.load(_latest_session_id(store))
    assert report.answer == "done"
    assert emitter.output_closed is True
    assert output_calls == 1
    assert tool_calls == 1
    assert sum(event.type == "tool.started" for event in events) == 1
    assert sum(event.type == "tool.finished" for event in events) == 1
    assert events[-1].type == "session.completed"


def test_cli_returns_zero_when_live_stdout_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitter = UiEmitter(
        lambda _event: (_ for _ in ()).throw(BrokenPipeError("closed"))
    )
    calls = 0

    def fake_run(*_args: object, ui_emitter: UiEmitter, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        ui_emitter.emit("run.finished", {"answer": "done"})
        return object()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli_module, "_build_live_emitter", lambda _args: emitter)
    monkeypatch.setattr(cli_module, "run_agent_with_report", fake_run)
    monkeypatch.setattr(cli_module, "_silence_broken_stdout", lambda: None)

    exit_code = cli_module.main(
        ["--workspace", str(tmp_path), "--sandbox", "none", "inspect"]
    )

    assert exit_code == 0
    assert calls == 1
    assert emitter.output_closed is True


@pytest.mark.parametrize(
    ("failure", "expected_exit", "terminal_type"),
    [
        (RuntimeError("agent failed"), 1, "run.failed"),
        (KeyboardInterrupt("stop"), 130, "run.interrupted"),
    ],
)
def test_cli_maps_runtime_failure_and_interrupt_to_stable_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected_exit: int,
    terminal_type: str,
) -> None:
    events: list[object] = []
    emitter = UiEmitter(events.append)

    def fail_run(*_args: object, **_kwargs: object) -> object:
        raise failure

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli_module, "_build_live_emitter", lambda _args: emitter)
    monkeypatch.setattr(cli_module, "run_agent_with_report", fail_run)

    exit_code = cli_module.main(
        ["--workspace", str(tmp_path), "--sandbox", "none", "inspect"]
    )

    assert exit_code == expected_exit
    assert [getattr(event, "type") for event in events] == [
        "run.started",
        terminal_type,
    ]


def test_host_process_interrupt_terminates_tree_before_reraising(
    tmp_path: Path,
) -> None:
    waits = 0
    kills = 0
    terminated: list[int] = []

    class InterruptedProcess:
        pid = 4321
        returncode: int | None = None
        stdout = io.BytesIO(b"partial stdout\n")
        stderr = io.BytesIO(b"partial stderr\n")

        def wait(self, timeout: float | None = None) -> int:
            nonlocal waits
            waits += 1
            if waits == 1:
                raise KeyboardInterrupt("stop command")
            self.returncode = -15
            return -15

        def kill(self) -> None:
            nonlocal kills
            kills += 1
            self.returncode = -9

    process = InterruptedProcess()

    def terminate(child, _cwd, _environment):
        terminated.append(child.pid)
        return True, None

    runner = HostProcessRunner(
        popen_factory=lambda *_args, **_kwargs: process,
        tree_terminator=terminate,
    )

    from coding_agent.security.models import (
        CommandPolicyDecision,
        CommandSpec,
        ExecutionLimits,
    )

    command = CommandSpec(
        argv=(sys.executable, "--version"),
        cwd=".",
        source="internal",
        purpose="interrupt cleanup test",
        limits=ExecutionLimits(
            timeout_ms=30_000,
            max_output_bytes=4_000,
            max_output_lines=100,
        ),
    )
    decision = CommandPolicyDecision(
        disposition="allow_host",
        rule_id="test.allow_host",
        reasons=("test decision",),
        normalized_executable="python",
        requires_approval=False,
        requires_sandbox=False,
    )

    with pytest.raises(KeyboardInterrupt, match="stop command"):
        runner.run(tmp_path, command, decision)

    assert terminated == [4321]
    assert waits == 2
    assert kills == 0
    assert process.stdout.closed is True
    assert process.stderr.closed is True

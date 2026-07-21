from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

import coding_agent.agent as agent_module
import coding_agent.cli as cli_module
from coding_agent.agent import (
    FaultPoint,
    resume_agent_with_report,
    run_agent_with_report,
)
from coding_agent.approvals import (
    ApprovalRequest,
    create_approval_decision,
)
from coding_agent.security.models import SandboxCapability
from coding_agent.sessions.store import SessionStore
from coding_agent.types import AgentConfig, ToolResult
from coding_agent.ui import TerminalRenderer, UiEmitter, UiEvent
from coding_agent.verification import VerificationResult


def _config(
    workspace: Path,
    *,
    auto_approve_edits: bool = False,
) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="workspace-write",
        auto_approve_commands=False,
        auto_approve_edits=auto_approve_edits,
        context_max_files=6,
        context_max_bytes_per_file=4_000,
    )


class _ToolThenFinalClient:
    def __init__(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        call_id: str = "call-step6",
    ) -> None:
        self.name = name
        self.arguments = arguments
        self.call_id = call_id
        self.initial_calls = 0
        self.continuation_calls = 0

    def create_initial_response(self, **_kwargs: object) -> dict[str, Any]:
        self.initial_calls += 1
        return {
            "id": "response-tools",
            "output": [
                {
                    "type": "function_call",
                    "name": self.name,
                    "arguments": json.dumps(self.arguments),
                    "call_id": self.call_id,
                }
            ],
        }

    def create_tool_response(self, **_kwargs: object) -> dict[str, object]:
        self.continuation_calls += 1
        return {
            "id": "response-final",
            "output": [],
            "output_text": "done",
        }


def _interrupt_at(target: FaultPoint):
    def inject(point: FaultPoint) -> None:
        if point == target:
            raise KeyboardInterrupt(target)

    return inject


def _add_patch(path: str = "created.txt", content: str = "created\n") -> str:
    lines = content.rstrip("\n").split("\n")
    return "\n".join(
        [
            "--- /dev/null",
            f"+++ b/{path}",
            f"@@ -0,0 +1,{len(lines)} @@",
            *(f"+{line}" for line in lines),
            "",
        ]
    )


def _modify_patch(path: str, before: str, after: str) -> str:
    return "\n".join(
        [
            f"--- a/{path}",
            f"+++ b/{path}",
            "@@ -1 +1 @@",
            f"-{before}",
            f"+{after}",
            "",
        ]
    )


@pytest.mark.parametrize("ok", [True, False])
def test_tool_events_wrap_durable_completion_and_apply_console_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ok: bool,
) -> None:
    timeline: list[str] = []
    ui_events: list[UiEvent] = []
    store = SessionStore(tmp_path)
    original_append = store.append

    def recording_append(session_id, event_type, payload):
        event = original_append(session_id, event_type, payload)
        timeline.append(f"durable:{event_type}")
        return event

    def record_ui(event: UiEvent) -> None:
        timeline.append(f"ui:{event.type}")
        ui_events.append(event)

    output = ("success-output-" if ok else "failure-output-") * 220

    def fake_execute_tool(*_args: object, **_kwargs: object) -> ToolResult:
        timeline.append("tool:execute")
        assert timeline.index("durable:tool.started") < timeline.index(
            "ui:tool.started"
        ) < timeline.index("tool:execute")
        return ToolResult(
            ok=ok,
            output=output,
            data={
                "type": "secure_command_result",
                "duration_ms": 37,
                "backend": "docker",
                "sandboxed": True,
                "output_truncated": False,
            },
        )

    monkeypatch.setattr(store, "append", recording_append)
    monkeypatch.setattr(agent_module, "execute_tool", fake_execute_tool)

    run_agent_with_report(
        "exercise tool UI",
        _config(tmp_path),
        model_client=_ToolThenFinalClient("list_files", {"path": "."}),
        session_store=store,
        ui_emitter=UiEmitter(record_ui),
    )

    assert timeline.index("tool:execute") < timeline.index(
        "durable:tool.finished"
    ) < timeline.index("ui:tool.finished")
    finished = next(event for event in ui_events if event.type == "tool.finished")
    assert finished.payload["call_id"] == "call-step6"
    assert finished.payload["name"] == "list_files"
    assert finished.payload["status"] == ("passed" if ok else "failed")
    assert finished.payload["duration_ms"] == 37
    assert finished.payload["backend"] == "docker"
    assert finished.payload["sandboxed"] is True
    assert finished.payload["output_truncated"] is True
    assert finished.payload["summary"].endswith("[console output truncated]")
    assert len(finished.payload["summary"]) < len(output)


def test_approval_ui_order_tracks_the_durable_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: list[str] = []
    ui_events: list[UiEvent] = []
    store = SessionStore(tmp_path)
    original_append = store.append
    patch = _add_patch()

    def recording_append(session_id, event_type, payload):
        event = original_append(session_id, event_type, payload)
        timeline.append(f"durable:{event_type}")
        return event

    def record_ui(event: UiEvent) -> None:
        timeline.append(f"ui:{event.type}")
        ui_events.append(event)

    def approve(request: ApprovalRequest):
        timeline.append("approval:handler")
        assert timeline[-2] == "ui:approval.requested"
        return create_approval_decision(
            request,
            approved=True,
            source="interactive",
        )

    monkeypatch.setattr(store, "append", recording_append)
    run_agent_with_report(
        "create a file",
        _config(tmp_path),
        model_client=_ToolThenFinalClient("apply_patch", {"patch": patch}),
        session_store=store,
        approval_handler=approve,
        ui_emitter=UiEmitter(record_ui),
    )

    assert timeline.index("ui:approval.requested") < timeline.index(
        "approval:handler"
    ) < timeline.index("durable:approval.decided") < timeline.index(
        "ui:approval.decided"
    )
    requested = next(
        event for event in ui_events if event.type == "approval.requested"
    )
    decided = next(
        event for event in ui_events if event.type == "approval.decided"
    )
    assert patch in requested.payload["message"]
    assert decided.payload["call_id"] == requested.payload["call_id"]
    assert decided.payload["outcome"] == "approved"
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "created\n"


def test_verification_finished_follows_the_durable_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: list[str] = []
    ui_events: list[UiEvent] = []
    store = SessionStore(tmp_path)
    original_append = store.append

    def recording_append(session_id, event_type, payload):
        event = original_append(session_id, event_type, payload)
        timeline.append(f"durable:{event_type}")
        return event

    def record_ui(event: UiEvent) -> None:
        timeline.append(f"ui:{event.type}")
        ui_events.append(event)

    verification = VerificationResult(
        command_id="python:pytest",
        kind="test",
        status="failed",
        argv=("pytest", "-q"),
        cwd=str(tmp_path.resolve()),
        exit_code=1,
        duration_ms=91,
        output="1 failed",
        truncated=True,
        omitted_lines=4,
        omitted_bytes=80,
        attempt=2,
    )

    def fake_execute_tool(
        *_args: object,
        state,
        **_kwargs: object,
    ) -> ToolResult:
        state.record_verification(verification)
        return ToolResult(
            ok=False,
            output="1 failed",
            data={
                "type": "verification_result",
                "duration_ms": verification.duration_ms,
            },
        )

    monkeypatch.setattr(store, "append", recording_append)
    monkeypatch.setattr(agent_module, "execute_tool", fake_execute_tool)

    run_agent_with_report(
        "run verification",
        _config(tmp_path),
        model_client=_ToolThenFinalClient(
            "list_files",
            {"path": "."},
            call_id="call-verification",
        ),
        session_store=store,
        ui_emitter=UiEmitter(record_ui),
    )

    assert timeline.index("durable:verification.recorded") < timeline.index(
        "ui:verification.finished"
    )
    event = next(
        item for item in ui_events if item.type == "verification.finished"
    )
    assert event.payload == {
        "command_id": "python:pytest",
        "kind": "test",
        "status": "failed",
        "exit_code": 1,
        "duration_ms": 91,
        "attempt": 2,
        "output_truncated": True,
    }


@pytest.mark.parametrize(
    ("fault_point", "event_type", "expected_summary"),
    [
        ("after_tool_side_effect", "tool.started", "recovery: safe retry"),
    ],
)
def test_resume_projects_retry_summary(
    tmp_path: Path,
    fault_point: FaultPoint,
    event_type: str,
    expected_summary: str,
) -> None:
    store = SessionStore(tmp_path)
    client = _ToolThenFinalClient("list_files", {"path": "."})
    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "list files",
            _config(tmp_path),
            model_client=client,
            session_store=store,
            fault_injector=_interrupt_at(fault_point),
        )

    ui_events: list[UiEvent] = []
    report = resume_agent_with_report(
        store.list_sessions()[0].session_id,
        tmp_path,
        model_client=client,
        session_store=store,
        ui_emitter=UiEmitter(ui_events.append),
    )

    assert report.answer == "done"
    projected = next(
        event
        for event in ui_events
        if event.type == event_type
        and event.payload.get("call_id") == "call-step6"
    )
    assert projected.payload["summary"] == expected_summary


def test_resume_projects_reapproval_summary_before_retry(
    tmp_path: Path,
) -> None:
    target = tmp_path / "value.txt"
    target.write_text("before\n", encoding="utf-8")
    patch = _modify_patch("value.txt", "before", "after")
    store = SessionStore(tmp_path)
    client = _ToolThenFinalClient("apply_patch", {"patch": patch})

    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "edit the file",
            _config(tmp_path, auto_approve_edits=True),
            model_client=client,
            session_store=store,
            fault_injector=_interrupt_at("after_tool_side_effect"),
        )
    assert target.read_text(encoding="utf-8") == "after\n"
    target.write_text("before\n", encoding="utf-8")

    def approve_recovery(request: ApprovalRequest):
        return create_approval_decision(
            request,
            approved=True,
            source="resume_recovery",
        )

    ui_events: list[UiEvent] = []
    report = resume_agent_with_report(
        store.list_sessions()[0].session_id,
        tmp_path,
        model_client=client,
        session_store=store,
        recovery_approval_handler=approve_recovery,
        ui_emitter=UiEmitter(ui_events.append),
    )

    assert report.answer == "done"
    started = next(
        event
        for event in ui_events
        if event.type == "tool.started"
        and event.payload.get("call_id") == "call-step6"
    )
    assert started.payload["summary"] == "recovery: reapproval required"
    assert any(event.type == "approval.requested" for event in ui_events)
    assert target.read_text(encoding="utf-8") == "after\n"


def test_resume_projects_recovered_completion_summary(
    tmp_path: Path,
) -> None:
    target = tmp_path / "value.txt"
    target.write_text("before\n", encoding="utf-8")
    patch = _modify_patch("value.txt", "before", "after")
    store = SessionStore(tmp_path)
    client = _ToolThenFinalClient("apply_patch", {"patch": patch})

    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "edit the file",
            _config(tmp_path, auto_approve_edits=True),
            model_client=client,
            session_store=store,
            fault_injector=_interrupt_at("after_tool_side_effect"),
        )

    ui_events: list[UiEvent] = []
    report = resume_agent_with_report(
        store.list_sessions()[0].session_id,
        tmp_path,
        model_client=client,
        session_store=store,
        ui_emitter=UiEmitter(ui_events.append),
    )

    assert report.answer == "done"
    finished = next(
        event
        for event in ui_events
        if event.type == "tool.finished"
        and event.payload.get("call_id") == "call-step6"
    )
    assert finished.payload["summary"].startswith(
        "recovered: completed tool result"
    )
    assert target.read_text(encoding="utf-8") == "after\n"


def test_sandbox_ui_only_contains_redacted_security_summaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "sha256:" + "a" * 64
    container_name = "coding-agent-secret-container"
    capability_reason = "daemon details must stay private"
    cleanup_error = "cleanup command exposed private host details"
    snapshot_error = "snapshot path C:/private/workspace"
    capability = SandboxCapability(
        backend="docker",
        available=False,
        reason=capability_reason,
        image_reference="python:3.12-slim",
        image_digest=None,
    )

    def fake_execute_tool(
        *_args: object,
        security_event_handler,
        **_kwargs: object,
    ) -> ToolResult:
        security_event_handler(
            "sandbox.capability_checked",
            {"capability": capability.to_dict()},
        )
        security_event_handler(
            "sandbox.started",
            {
                "backend": "docker",
                "container_name": container_name,
                "image_digest": digest,
                "network_mode": "none",
            },
        )
        security_event_handler(
            "sandbox.cleanup_failed",
            {
                "cleanup_kind": "container",
                "reason": cleanup_error,
            },
        )
        return ToolResult(
            ok=False,
            output="\n".join(
                [
                    digest,
                    container_name,
                    capability_reason,
                    cleanup_error,
                    snapshot_error,
                ]
            ),
            data={
                "type": "secure_command_result",
                "duration_ms": 8,
                "backend": "docker",
                "sandboxed": True,
                "image_digest": digest,
                "snapshot_cleanup_succeeded": False,
                "snapshot_cleanup_error": snapshot_error,
            },
        )

    monkeypatch.setattr(agent_module, "execute_tool", fake_execute_tool)
    ui_events: list[UiEvent] = []
    store = SessionStore(tmp_path)
    run_agent_with_report(
        "exercise sandbox projection",
        _config(tmp_path),
        model_client=_ToolThenFinalClient("list_files", {"path": "."}),
        session_store=store,
        ui_emitter=UiEmitter(ui_events.append),
    )

    serialized_ui = json.dumps(
        [event.to_dict() for event in ui_events],
        ensure_ascii=False,
    )
    for sensitive in (
        digest,
        container_name,
        capability_reason,
        cleanup_error,
        snapshot_error,
    ):
        assert sensitive not in serialized_ui

    finished = next(event for event in ui_events if event.type == "tool.finished")
    summary = finished.payload["summary"]
    assert "sandbox capability unavailable" in summary
    assert "sandbox image digest verified" in summary
    assert "sandbox cleanup failed: container" in summary
    assert "sandbox cleanup failed: snapshot" in summary
    assert finished.payload["backend"] == "docker"
    assert finished.payload["sandboxed"] is True

    stdout = io.StringIO()
    stderr = io.StringIO()
    renderer = TerminalRenderer(
        stdout=stdout,
        stderr=stderr,
        is_tty=True,
        color_enabled=False,
    )
    renderer(finished)
    rendered = stdout.getvalue() + stderr.getvalue()
    for sensitive in (
        digest,
        container_name,
        capability_reason,
        cleanup_error,
        snapshot_error,
    ):
        assert sensitive not in rendered

    durable_text = repr(
        store.load(store.list_sessions()[0].session_id)
    )
    assert digest in durable_text
    assert cleanup_error in durable_text


@pytest.mark.parametrize(
    ("raised", "durable_type", "ui_type", "expected_status"),
    [
        (KeyboardInterrupt("stop"), "session.interrupted", "run.interrupted", "interrupted"),
        (RuntimeError("private failure"), "session.failed", "run.failed", "failed"),
    ],
)
def test_terminal_events_are_durable_redacted_and_emitted_once(
    tmp_path: Path,
    raised: BaseException,
    durable_type: str,
    ui_type: str,
    expected_status: str,
) -> None:
    class FailingClient:
        def create_initial_response(self, **_kwargs: object) -> object:
            raise raised

        def create_tool_response(self, **_kwargs: object) -> object:
            raise AssertionError

    store = SessionStore(tmp_path)
    ui_events: list[UiEvent] = []
    with pytest.raises(type(raised)):
        run_agent_with_report(
            "fail safely",
            _config(tmp_path),
            model_client=FailingClient(),
            session_store=store,
            ui_emitter=UiEmitter(ui_events.append),
        )

    session_id = store.list_sessions()[0].session_id
    durable = store.load(session_id)
    assert durable[-1].type == durable_type
    assert store.list_sessions()[0].status == expected_status
    terminal = [event for event in ui_events if event.type == ui_type]
    assert len(terminal) == 1
    assert "private failure" not in repr(terminal[0].payload)
    assert "stop" not in repr(terminal[0].payload)


def test_ui_emitter_tracks_terminal_error_projection() -> None:
    emitter = UiEmitter()
    assert emitter.terminal_event_emitted is False
    emitter.emit("run.failed", {"reason": "exception"})
    assert emitter.terminal_event_emitted is True


@pytest.mark.parametrize(
    ("raised", "ui_type", "exit_code"),
    [
        (RuntimeError("agent failed"), "run.failed", 1),
        (KeyboardInterrupt("agent stopped"), "run.interrupted", 130),
    ],
)
def test_cli_does_not_duplicate_agent_terminal_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raised: BaseException,
    ui_type: str,
    exit_code: int,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def fake_run(*_args: object, ui_emitter: UiEmitter, **_kwargs: object):
        ui_emitter.emit(ui_type, {"reason": "test"})
        raise raised

    monkeypatch.setattr(cli_module, "run_agent_with_report", fake_run)

    result = cli_module.main(
        [
            "--workspace",
            str(tmp_path),
            "--sandbox",
            "none",
            "inspect",
        ]
    )

    captured = capsys.readouterr()
    terminal_output = captured.out + captured.err
    assert result == exit_code
    assert terminal_output.count(ui_type) == 1

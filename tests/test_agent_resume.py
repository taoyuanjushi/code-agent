from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import coding_agent.agent as agent_module
import coding_agent.sessions.workspace_guard as workspace_guard_module
import coding_agent.tools as tools_module
from coding_agent.agent import (
    FaultPoint,
    ResumeModelContextUnavailable,
    ResumeTurnLimitError,
    SessionAlreadyCompletedError,
    resume_agent_with_report,
    run_agent_with_report,
)
from coding_agent.approvals import ApprovalRequest, create_approval_decision
from coding_agent.sessions.codec import session_started_from_dict
from coding_agent.sessions.reducer import rebuild_state
from coding_agent.sessions.store import (
    ConcurrentSessionWriteError,
    SessionStore,
)
from coding_agent.sessions.workspace_guard import (
    GitHeadMismatchError,
    TouchedFileDriftError,
    WorkspaceMismatchError,
    validate_workspace_guard,
)
from coding_agent.tool_outputs import pending_outputs_for_model
from coding_agent.types import AgentConfig, ToolResult
from coding_agent.verification import VerificationResult


def _config(
    workspace: Path,
    *,
    max_turns: int = 4,
    max_fix_attempts: int = 2,
    auto_approve_edits: bool = False,
    auto_approve_commands: bool = False,
) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=max_turns,
        permission_mode="workspace-write",
        auto_approve_commands=auto_approve_commands,
        auto_approve_edits=auto_approve_edits,
        context_max_files=6,
        context_max_bytes_per_file=4_000,
        max_fix_attempts=max_fix_attempts,
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


def _tool_response(
    response_id: str,
    *calls: dict[str, object],
) -> dict[str, object]:
    return {"id": response_id, "output": list(calls)}


def _final_response(response_id: str = "response-final") -> dict[str, object]:
    return {"id": response_id, "output": [], "output_text": "done"}


def _interrupt_at(target: FaultPoint, *, occurrence: int = 1):
    seen = 0

    def inject(point: FaultPoint) -> None:
        nonlocal seen
        if point != target:
            return
        seen += 1
        if seen == occurrence:
            raise KeyboardInterrupt(f"{target}:{occurrence}")

    return inject


def _latest_session_id(store: SessionStore) -> str:
    return store.list_sessions()[0].session_id


class _ToolThenFinalClient:
    def __init__(self, *calls: dict[str, object]) -> None:
        self.calls = calls
        self.initial_count = 0
        self.continuation_count = 0
        self.received_tool_outputs: list[dict[str, Any]] | None = None

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        self.initial_count += 1
        return _tool_response("response-tools", *self.calls)

    def create_tool_response(
        self,
        *,
        tool_outputs: list[dict[str, Any]],
        **_kwargs: object,
    ) -> dict[str, object]:
        self.continuation_count += 1
        self.received_tool_outputs = tool_outputs
        return _final_response()


class _FinalClient:
    def __init__(self, response_id: str = "response-final") -> None:
        self.response_id = response_id
        self.initial_count = 0

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        self.initial_count += 1
        return _final_response(self.response_id)

    def create_tool_response(self, **_kwargs: object) -> object:
        raise AssertionError("no continuation should be requested")


class _NoModelCallsClient:
    def create_initial_response(self, **_kwargs: object) -> object:
        raise AssertionError("resume must not create an initial response")

    def create_tool_response(self, **_kwargs: object) -> object:
        raise AssertionError("resume must not create a continuation response")


def test_resume_retries_the_exact_persisted_initial_request(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    original_request: dict[str, str] = {}

    class FailingClient:
        def create_initial_response(
            self,
            *,
            instructions: str,
            input_text: str,
            **_kwargs: object,
        ) -> object:
            original_request.update(
                instructions=instructions,
                input_text=input_text,
            )
            raise RuntimeError("remote response was not recorded")

        def create_tool_response(self, **_kwargs: object) -> object:
            raise AssertionError

    with pytest.raises(RuntimeError, match="not recorded"):
        run_agent_with_report(
            "inspect the workspace",
            _config(tmp_path),
            model_client=FailingClient(),
            session_store=store,
        )

    session_id = _latest_session_id(store)
    retried_request: dict[str, str] = {}

    class RetryClient(_FinalClient):
        def create_initial_response(
            self,
            *,
            instructions: str,
            input_text: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            retried_request.update(
                instructions=instructions,
                input_text=input_text,
            )
            return _final_response("response-retry")

    report = resume_agent_with_report(
        session_id,
        tmp_path,
        model_client=RetryClient(),
        session_store=store,
    )

    assert report.answer == "done"
    assert retried_request == original_request
    events = store.load(session_id)
    resumed = next(event for event in events if event.type == "session.resumed")
    assert resumed.payload["retry_pending_model_request"] is True
    requests = [event for event in events if event.type == "model.requested"]
    assert requests[-1].payload["retry_of_seq"] == requests[0].seq
    assert rebuild_state(events).status == "completed"


def test_resume_awaiting_tools_executes_only_pending_calls(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    client = _ToolThenFinalClient(
        _function_call("call-list", "list_files", {"path": "."})
    )

    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "list files",
            _config(tmp_path),
            model_client=client,
            session_store=store,
            fault_injector=_interrupt_at("after_model_response"),
        )

    report = resume_agent_with_report(
        _latest_session_id(store),
        tmp_path,
        model_client=client,
        session_store=store,
    )

    assert report.answer == "done"
    assert client.initial_count == 1
    assert client.continuation_count == 1


def test_resume_awaiting_model_reuses_persisted_outputs_without_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    client = _ToolThenFinalClient(
        _function_call("call-list", "list_files", {"path": "."})
    )
    real_execute_tool = agent_module.execute_tool
    executions = 0

    def counting_execute_tool(*args: object, **kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return real_execute_tool(*args, **kwargs)

    monkeypatch.setattr(agent_module, "execute_tool", counting_execute_tool)
    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "list files",
            _config(tmp_path),
            model_client=client,
            session_store=store,
            fault_injector=_interrupt_at("after_tool_finished"),
        )

    session_id = _latest_session_id(store)
    interrupted = rebuild_state(store.load(session_id))
    expected_outputs = pending_outputs_for_model(interrupted.pending_tool_outputs)

    report = resume_agent_with_report(
        session_id,
        tmp_path,
        model_client=client,
        session_store=store,
    )

    assert report.answer == "done"
    assert executions == 1
    assert client.received_tool_outputs == expected_outputs


def test_resume_finalizing_completes_without_model_or_tool_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "finish immediately",
            _config(tmp_path),
            model_client=_FinalClient(),
            session_store=store,
            fault_injector=_interrupt_at("after_model_response"),
        )

    monkeypatch.setattr(
        agent_module,
        "execute_tool",
        lambda *_args, **_kwargs: pytest.fail("tool execution is not allowed"),
    )
    monkeypatch.setattr(
        agent_module,
        "OpenAIResponsesClient",
        lambda: pytest.fail("finalizing resume must not create a model client"),
    )
    report = resume_agent_with_report(
        _latest_session_id(store),
        tmp_path,
        session_store=store,
    )

    assert report.answer == "done"
    assert rebuild_state(store.load(report.session_id or "")).status == "completed"


def test_completed_session_rejects_resume(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    report = run_agent_with_report(
        "finish",
        _config(tmp_path),
        model_client=_FinalClient(),
        session_store=store,
    )

    with pytest.raises(SessionAlreadyCompletedError, match="replay"):
        resume_agent_with_report(
            report.session_id or "",
            tmp_path,
            model_client=_NoModelCallsClient(),
            session_store=store,
        )


def test_workspace_guard_rejects_a_different_canonical_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    store = SessionStore(workspace)
    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "pause",
            _config(workspace),
            model_client=_FinalClient(),
            session_store=store,
            fault_injector=_interrupt_at("after_model_response"),
        )

    events = store.load(_latest_session_id(store))
    raw_started = events[0].payload.get("session", events[0].payload)
    assert isinstance(raw_started, Mapping)
    started = session_started_from_dict(raw_started)

    with pytest.raises(WorkspaceMismatchError, match="does not match"):
        validate_workspace_guard(other, started, rebuild_state(events))


def test_resume_rejects_git_head_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    monkeypatch.setattr(agent_module, "discover_git_head", lambda _path: "head-a")
    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "pause",
            _config(tmp_path),
            model_client=_FinalClient(),
            session_store=store,
            fault_injector=_interrupt_at("after_model_response"),
        )

    monkeypatch.setattr(
        workspace_guard_module,
        "discover_git_head",
        lambda _path: "head-b",
    )
    with pytest.raises(GitHeadMismatchError, match="head-a"):
        resume_agent_with_report(
            _latest_session_id(store),
            tmp_path,
            model_client=_NoModelCallsClient(),
            session_store=store,
        )


def test_resume_rejects_external_drift_of_a_touched_file(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_text("one\n", encoding="utf-8")
    patch = _replace_line_patch("value.txt", "one", "two")
    store = SessionStore(tmp_path)
    client = _ToolThenFinalClient(
        _function_call("call-patch", "apply_patch", {"patch": patch})
    )

    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "change value",
            _config(tmp_path, auto_approve_edits=True),
            model_client=client,
            session_store=store,
            fault_injector=_interrupt_at("after_tool_finished"),
        )
    target.write_text("external\n", encoding="utf-8")

    with pytest.raises(TouchedFileDriftError) as raised:
        resume_agent_with_report(
            _latest_session_id(store),
            tmp_path,
            model_client=client,
            session_store=store,
        )

    assert raised.value.mismatches[0].path == "value.txt"


def test_interrupted_patch_transition_explains_touched_hash_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "value.txt"
    target.write_text("one\n", encoding="utf-8")
    store = SessionStore(tmp_path)
    client = _ToolThenFinalClient(
        _function_call(
            "call-patch-1",
            "apply_patch",
            {"patch": _replace_line_patch("value.txt", "one", "two")},
        ),
        _function_call(
            "call-patch-2",
            "apply_patch",
            {"patch": _replace_line_patch("value.txt", "two", "three")},
        ),
    )

    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "change twice",
            _config(tmp_path, auto_approve_edits=True),
            model_client=client,
            session_store=store,
            fault_injector=_interrupt_at("after_tool_side_effect", occurrence=2),
        )
    assert target.read_text(encoding="utf-8") == "three\n"

    monkeypatch.setattr(
        agent_module,
        "execute_tool",
        lambda *_args, **_kwargs: pytest.fail("recovered patch must not rerun"),
    )
    report = resume_agent_with_report(
        _latest_session_id(store),
        tmp_path,
        model_client=client,
        session_store=store,
    )

    assert report.answer == "done"
    assert target.read_text(encoding="utf-8") == "three\n"
    recovered = [
        event
        for event in store.load(report.session_id or "")
        if event.type == "tool.recovered"
    ]
    assert recovered[-1].payload["completed"] is True
    assert recovered[-1].payload["reason"] == "patch_after_hash_match"


def test_process_recovery_uses_explicit_resume_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executions = 0

    def fake_command(command: str, cwd: str, timeout_ms: int) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(
            ok=True,
            output="exit code: 0",
            data={
                "type": "command_result",
                "command": command,
                "cwd": str(Path(cwd).resolve()),
                "shell": True,
                "timeout_ms": timeout_ms,
                "exit_code": 0,
                "timed_out": False,
                "duration_ms": 1,
            },
        )

    monkeypatch.setattr(tools_module, "_run_shell_command", fake_command)
    store = SessionStore(tmp_path)
    client = _ToolThenFinalClient(
        _function_call(
            "call-command",
            "run_command",
            {"command": "echo recovery"},
        )
    )
    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "run a command",
            _config(tmp_path, auto_approve_commands=True),
            model_client=client,
            session_store=store,
            fault_injector=_interrupt_at("after_tool_side_effect"),
        )

    def forbid_normal_approval(_request: ApprovalRequest):
        raise AssertionError("normal auto-approval must not approve recovery")

    def approve_recovery(request: ApprovalRequest):
        return create_approval_decision(
            request,
            approved=True,
            source="resume_recovery",
        )

    report = resume_agent_with_report(
        _latest_session_id(store),
        tmp_path,
        model_client=client,
        session_store=store,
        approval_handler=forbid_normal_approval,
        recovery_approval_handler=approve_recovery,
    )

    assert report.answer == "done"
    assert executions == 2
    events = store.load(report.session_id or "")
    approval_sources = [
        event.payload["decision"]["source"]
        for event in events
        if event.type == "approval.decided"
    ]
    assert approval_sources == ["auto_policy", "resume_recovery"]
    finished = [event for event in events if event.type == "tool.finished"][-1]
    assert finished.payload["recovery_retry"] is True


def test_resume_reports_unavailable_previous_model_context(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    client = _ToolThenFinalClient(
        _function_call("call-list", "list_files", {"path": "."})
    )
    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "list files",
            _config(tmp_path),
            model_client=client,
            session_store=store,
            fault_injector=_interrupt_at("after_tool_finished"),
        )

    class MissingContextClient:
        def create_initial_response(self, **_kwargs: object) -> object:
            raise AssertionError

        def create_tool_response(self, **_kwargs: object) -> object:
            raise LookupError("previous response expired")

    session_id = _latest_session_id(store)
    with pytest.raises(
        ResumeModelContextUnavailable,
        match="resume_model_context_unavailable",
    ):
        resume_agent_with_report(
            session_id,
            tmp_path,
            model_client=MissingContextClient(),
            session_store=store,
        )

    events = store.load(session_id)
    assert events[-1].type == "session.failed"
    assert events[-1].payload["reason"] == "resume_model_context_unavailable"


def test_resume_restores_verification_limits_and_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    client = _ToolThenFinalClient(
        _function_call("call-setup", "list_files", {"path": "."}),
        _function_call("call-check", "list_files", {"path": "."}),
    )
    checked = False

    def stateful_execute_tool(
        _config_value: AgentConfig,
        _name: str,
        _arguments: str,
        *,
        state: Any,
        call_id: str,
        **_kwargs: object,
    ) -> ToolResult:
        nonlocal checked
        if call_id == "call-setup":
            state.record_verification(_failed_verification(tmp_path))
            state.record_patch_applied()
            state.record_patch_applied()
            state.passed_generations["python:prior"] = 1
            return ToolResult(ok=True, output="state prepared")

        assert call_id == "call-check"
        assert state.max_fix_attempts == 2
        assert len(state.verification_history) == 1
        assert state.unresolved_failure_command_id == "python:test"
        assert state.repair_attempts == 2
        assert state.repair_limit_reached is True
        assert state.edit_generation == 2
        assert state.after_edit is True
        assert state.passed_generations == {"python:prior": 1}
        checked = True
        return ToolResult(ok=True, output="state restored")

    monkeypatch.setattr(agent_module, "execute_tool", stateful_execute_tool)
    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "preserve verification state",
            _config(tmp_path, max_fix_attempts=2),
            model_client=client,
            session_store=store,
            fault_injector=_interrupt_at("after_tool_finished"),
        )

    resume_agent_with_report(
        _latest_session_id(store),
        tmp_path,
        model_client=client,
        session_store=store,
    )
    assert checked is True


def test_resume_rejects_tool_calls_beyond_the_persisted_turn_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    executed_call_ids: list[str] = []

    class TwoToolTurnClient:
        def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
            return _tool_response(
                "response-one",
                _function_call("call-one", "list_files", {"path": "."}),
            )

        def create_tool_response(self, **_kwargs: object) -> dict[str, object]:
            return _tool_response(
                "response-two",
                _function_call("call-two", "list_files", {"path": "."}),
            )

    def record_execution(
        _config_value: AgentConfig,
        _name: str,
        _arguments: str,
        *,
        call_id: str,
        **_kwargs: object,
    ) -> ToolResult:
        executed_call_ids.append(call_id)
        return ToolResult(ok=True, output="ok")

    monkeypatch.setattr(agent_module, "execute_tool", record_execution)
    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "respect turn limit",
            _config(tmp_path, max_turns=1),
            model_client=TwoToolTurnClient(),
            session_store=store,
            fault_injector=_interrupt_at("after_model_response", occurrence=2),
        )

    session_id = _latest_session_id(store)
    with pytest.raises(ResumeTurnLimitError, match="no remaining"):
        resume_agent_with_report(
            session_id,
            tmp_path,
            model_client=_NoModelCallsClient(),
            session_store=store,
        )

    assert executed_call_ids == ["call-one"]
    events = store.load(session_id)
    assert events[-1].type == "session.failed"
    assert events[-1].payload["reason"] == "turn_limit"


def test_active_writer_lease_blocks_a_second_resume(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "pause",
            _config(tmp_path),
            model_client=_FinalClient(),
            session_store=store,
            fault_injector=_interrupt_at("after_model_response"),
        )
    session_id = _latest_session_id(store)

    with store.exclusive_writer(session_id):
        with pytest.raises(ConcurrentSessionWriteError, match=session_id):
            resume_agent_with_report(
                session_id,
                tmp_path,
                model_client=_NoModelCallsClient(),
                session_store=store,
            )


def _replace_line_patch(path: str, before: str, after: str) -> str:
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


def _failed_verification(workspace: Path) -> VerificationResult:
    return VerificationResult(
        command_id="python:test",
        kind="test",
        status="failed",
        argv=("python", "-m", "pytest"),
        cwd=str(workspace.resolve()),
        exit_code=1,
        duration_ms=1,
        output="failed",
        truncated=False,
        omitted_lines=0,
        omitted_bytes=0,
        attempt=1,
    )

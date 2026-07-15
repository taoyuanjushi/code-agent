from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import coding_agent.tools as tools_module
from coding_agent.agent import FaultPoint, run_agent_with_report
from coding_agent.approvals import (
    ApprovalRequest,
    build_resume_recovery_approval_handler,
    create_approval_decision,
    validate_resume_recovery_decision,
)
from coding_agent.sessions.codec import (
    approval_decision_to_dict,
    create_session_event,
    approval_request_from_dict,
    approval_request_to_dict,
)
from coding_agent.sessions.recovery import (
    ToolRecoveryError,
    WorkspaceDriftError,
    build_recovery_event_payload,
    find_completed_tool_output,
    plan_interrupted_tools,
    plan_tool_recovery,
)
from coding_agent.sessions.reducer import (
    SessionReductionError,
    rebuild_state,
    reduce_event,
)
from coding_agent.sessions.store import SessionStore
from coding_agent.tool_policy import hash_tool_arguments
from coding_agent.types import AgentConfig, ToolResult


def _config(
    workspace: Path,
    *,
    auto_approve_edits: bool = False,
    auto_approve_commands: bool = False,
) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="workspace-write",
        auto_approve_commands=auto_approve_commands,
        auto_approve_edits=auto_approve_edits,
        context_max_files=6,
        context_max_bytes_per_file=4_000,
    )


class _SingleToolClient:
    def __init__(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        call_id: str = "call-recovery",
    ) -> None:
        self.name = name
        self.arguments = arguments
        self.call_id = call_id

    def create_initial_response(self, **_kwargs: object) -> dict[str, Any]:
        return {
            "id": "response-1",
            "output": [
                {
                    "type": "function_call",
                    "name": self.name,
                    "arguments": json.dumps(self.arguments),
                    "call_id": self.call_id,
                }
            ],
        }

    def create_tool_response(self, **_kwargs: object) -> object:
        raise AssertionError("fault injection must stop before model continuation")


def _fault_at(target: FaultPoint):
    def inject(point: FaultPoint) -> None:
        if point == target:
            raise KeyboardInterrupt(target)

    return inject


def _interrupted_session(
    workspace: Path,
    *,
    name: str,
    arguments: dict[str, object],
    config: AgentConfig,
    fault_point: FaultPoint = "after_tool_side_effect",
) -> tuple[SessionStore, str]:
    store = SessionStore(workspace)
    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "exercise interrupted tool recovery",
            config,
            model_client=_SingleToolClient(name, arguments),
            session_store=store,
            fault_injector=_fault_at(fault_point),
        )
    summary = store.list_sessions()[0]
    assert summary.status == "interrupted"
    return store, summary.session_id


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


def _two_file_patch() -> str:
    return "\n".join(
        [
            "--- a/one.txt",
            "+++ b/one.txt",
            "@@ -1 +1 @@",
            "-before-one",
            "+after-one",
            "--- a/two.txt",
            "+++ b/two.txt",
            "@@ -1 +1 @@",
            "-before-two",
            "+after-two",
            "",
        ]
    )


def test_completed_call_reuses_the_exact_persisted_output(
    tmp_path: Path,
) -> None:
    workspace = tmp_path
    store, session_id = _interrupted_session(
        workspace,
        name="list_files",
        arguments={"path": "."},
        config=_config(workspace),
        fault_point="after_tool_finished",
    )
    events = store.load(session_id)
    state = rebuild_state(events)
    finished = next(event for event in events if event.type == "tool.finished")

    output = find_completed_tool_output(events, "call-recovery")
    plan = plan_tool_recovery(
        workspace,
        events,
        state,
        "call-recovery",
    )

    assert output == finished.payload["tool_output"]
    assert plan.disposition == "reuse_completed"
    assert plan.tool_output == finished.payload["tool_output"]
    with pytest.raises(ToolRecoveryError, match="without appending"):
        build_recovery_event_payload(plan)


def test_read_only_interruption_is_reset_for_safe_retry(tmp_path: Path) -> None:
    (tmp_path / "visible.txt").write_text("value\n", encoding="utf-8")
    store, session_id = _interrupted_session(
        tmp_path,
        name="list_files",
        arguments={"path": "."},
        config=_config(tmp_path),
    )
    events = store.load(session_id)
    state = rebuild_state(events)

    plans = plan_interrupted_tools(tmp_path, events, state)

    assert len(plans) == 1
    assert plans[0].disposition == "safe_retry"
    payload = build_recovery_event_payload(plans[0])
    assert payload["completed"] is False
    assert payload["reason"] == "safe_retry"
    assert payload["requires_reapproval"] is False

    store.append(session_id, "session.resumed", {"reason": "test"})
    store.append(session_id, "tool.recovered", payload)
    recovered = rebuild_state(store.load(session_id))
    assert recovered.status == "running"
    assert recovered.pending_tool_calls[0].started is False
    assert recovered.completed_call_ids == frozenset()


def test_patch_after_hashes_recover_completion_without_reapplying(
    tmp_path: Path,
) -> None:
    target = tmp_path / "value.txt"
    target.write_text("before\n", encoding="utf-8")
    expected_before = hashlib.sha256(target.read_bytes()).hexdigest()
    patch = _modify_patch("value.txt", "before", "after")
    store, session_id = _interrupted_session(
        tmp_path,
        name="apply_patch",
        arguments={"patch": patch},
        config=_config(tmp_path, auto_approve_edits=True),
    )
    events = store.load(session_id)
    state = rebuild_state(events)

    assert target.read_text(encoding="utf-8") == "after\n"
    assert [event.type for event in events].count("tool.finished") == 0
    approval_event = next(
        event for event in events if event.type == "approval.decided"
    )
    request = approval_request_from_dict(approval_event.payload["request"])
    expected_after = hashlib.sha256(b"after\n").hexdigest()
    assert request.details["file_changes"] == (
        {
            "path": "value.txt",
            "change_type": "modify",
            "before_sha256": expected_before,
            "after_sha256": expected_after,
        },
    )

    plan = plan_tool_recovery(tmp_path, events, state, "call-recovery")

    assert plan.disposition == "recovered_completed"
    assert plan.reason == "patch_after_hash_match"
    assert plan.file_hashes[0].match == "after"
    payload = build_recovery_event_payload(
        plan,
        store=store,
        session_id=session_id,
    )
    store.append(session_id, "session.resumed", {"reason": "test"})
    store.append(session_id, "tool.recovered", payload)

    recovered = rebuild_state(store.load(session_id))
    assert target.read_text(encoding="utf-8") == "after\n"
    assert recovered.completed_call_ids == frozenset({"call-recovery"})
    assert recovered.pending_tool_calls == ()
    assert recovered.touched_file_hashes == {"value.txt": expected_after}
    persisted_payload = json.loads(recovered.pending_tool_outputs[0]["output"])
    assert persisted_payload["ok"] is True
    assert persisted_payload["data"]["recovery_reason"] == (
        "patch_after_hash_match"
    )


def test_patch_before_hashes_require_a_new_explicit_approval(
    tmp_path: Path,
) -> None:
    target = tmp_path / "value.txt"
    target.write_text("before\n", encoding="utf-8")
    patch = _modify_patch("value.txt", "before", "after")
    store, session_id = _interrupted_session(
        tmp_path,
        name="apply_patch",
        arguments={"patch": patch},
        config=_config(tmp_path, auto_approve_edits=True),
    )
    target.write_text("before\n", encoding="utf-8")
    events = store.load(session_id)
    state = rebuild_state(events)

    plan = plan_tool_recovery(tmp_path, events, state, "call-recovery")

    assert plan.disposition == "requires_reapproval"
    assert plan.reason == "patch_before_hash_match"
    assert plan.file_hashes[0].match == "before"
    assert plan.approval_request is not None
    assert plan.approval_request.details["patch"] == patch
    assert plan.approval_request.details["resume_recovery"] == {
        "reason": "patch_before_hash_match",
        "prior_result": "unknown",
        "auto_approval_allowed": False,
    }
    payload = build_recovery_event_payload(plan)
    assert payload["completed"] is False
    assert payload["requires_reapproval"] is True


def test_patch_mixed_or_unknown_hashes_refuse_recovery(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("before-one\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("before-two\n", encoding="utf-8")
    store, session_id = _interrupted_session(
        tmp_path,
        name="apply_patch",
        arguments={"patch": _two_file_patch()},
        config=_config(tmp_path, auto_approve_edits=True),
    )
    (tmp_path / "one.txt").write_text("before-one\n", encoding="utf-8")
    events = store.load(session_id)
    state = rebuild_state(events)

    mixed = plan_tool_recovery(tmp_path, events, state, "call-recovery")

    assert mixed.disposition == "workspace_drift"
    assert {item.match for item in mixed.file_hashes} == {"before", "after"}
    with pytest.raises(WorkspaceDriftError, match="Workspace drift"):
        build_recovery_event_payload(mixed)

    (tmp_path / "one.txt").write_text("unexpected\n", encoding="utf-8")
    unknown = plan_tool_recovery(tmp_path, events, state, "call-recovery")
    assert unknown.disposition == "workspace_drift"
    assert unknown.file_hashes[0].match == "drift"
    with pytest.raises(WorkspaceDriftError, match="current="):
        unknown.raise_for_workspace_drift()


def test_process_recovery_ignores_auto_approval_and_requires_resume_source(
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
    store, session_id = _interrupted_session(
        tmp_path,
        name="run_command",
        arguments={"command": "echo recovery"},
        config=_config(tmp_path, auto_approve_commands=True),
    )
    events = store.load(session_id)
    state = rebuild_state(events)
    original_decision = next(
        decision for decision in state.approvals if decision.call_id == "call-recovery"
    )
    assert original_decision.source == "auto_policy"

    plan = plan_tool_recovery(tmp_path, events, state, "call-recovery")

    assert executions == 1
    assert plan.disposition == "requires_reapproval"
    assert plan.reason == "unknown_process_result"
    assert plan.approval_request is not None
    assert plan.approval_request.details["resume_recovery"] == {
        "reason": "unknown_process_result",
        "prior_result": "unknown",
        "auto_approval_allowed": False,
    }

    auto_decision = create_approval_decision(
        plan.approval_request,
        approved=True,
        source="auto_policy",
    )
    with pytest.raises(ValueError, match="resume_recovery"):
        validate_resume_recovery_decision(plan.approval_request, auto_decision)

    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    recovery_handler = build_resume_recovery_approval_handler()
    recovery_decision = recovery_handler(plan.approval_request)
    validate_resume_recovery_decision(
        plan.approval_request,
        recovery_decision,
    )
    assert recovery_decision.source == "resume_recovery"
    assert executions == 1

    store.append(session_id, "session.resumed", {"reason": "test"})
    store.append(
        session_id,
        "tool.recovered",
        build_recovery_event_payload(plan),
    )
    raw_arguments = json.dumps({"command": "echo recovery"})
    store.append(
        session_id,
        "tool.started",
        {
            "call_id": "call-recovery",
            "name": "run_command",
            "arguments": raw_arguments,
            "effect": "process",
            "arguments_sha256": hash_tool_arguments(raw_arguments),
            "requires_approval": True,
        },
    )
    retry_state = rebuild_state(store.load(session_id))
    premature_finish = create_session_event(
        session_id=session_id,
        seq=retry_state.last_seq + 1,
        event_id="event-recovery-premature-finish",
        recorded_at="2026-07-15T02:00:00.000Z",
        event_type="tool.finished",
        prev_hash=retry_state.last_event_hash,
        payload={
            "call_id": "call-recovery",
            "name": "run_command",
            "arguments": raw_arguments,
            "effect": "process",
            "recovery_retry": True,
            "tool_output": {
                "type": "function_call_output",
                "call_id": "call-recovery",
                "output": "{}",
            },
        },
    )
    with pytest.raises(SessionReductionError, match="resume_recovery"):
        reduce_event(retry_state, premature_finish)

    store.append(
        session_id,
        "approval.decided",
        {
            "request": approval_request_to_dict(plan.approval_request),
            "decision": approval_decision_to_dict(recovery_decision),
        },
    )
    store.append(
        session_id,
        "tool.finished",
        {
            "call_id": "call-recovery",
            "name": "run_command",
            "arguments": raw_arguments,
            "effect": "process",
            "recovery_retry": True,
            "tool_output": {
                "type": "function_call_output",
                "call_id": "call-recovery",
                "output": json.dumps(
                    {"ok": True, "output": "exit code: 0", "data": None}
                ),
            },
        },
    )
    rebuilt = rebuild_state(store.load(session_id))
    assert [item.source for item in rebuilt.approvals] == [
        "auto_policy",
        "resume_recovery",
    ]
    assert rebuilt.completed_call_ids == frozenset({"call-recovery"})
    assert executions == 1

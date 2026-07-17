import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.process_fakes import patch_tools_runner, patch_verification_runner

import coding_agent.tools as tools_module
import coding_agent.verification as verification_module
from coding_agent.agent import run_agent_with_report
from coding_agent.approvals import (
    ApprovalDecision,
    ApprovalRequest,
    create_approval_decision,
    validate_approval_decision,
)
from coding_agent.sessions.codec import (
    approval_request_from_dict,
    approval_request_to_dict,
)
from coding_agent.sessions.store import SessionStore
from coding_agent.tool_policy import (
    TOOL_POLICIES,
    exposed_tool_names,
    hash_tool_arguments,
)
from coding_agent.tools import TOOL_DEFINITIONS, execute_tool
from coding_agent.types import AgentConfig, ToolResult


def _config(
    tmp_path: Path,
    *,
    auto_approve_edits: bool = False,
    auto_approve_commands: bool = False,
) -> AgentConfig:
    return AgentConfig(
        workspace=str(tmp_path),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="workspace-write",
        auto_approve_commands=auto_approve_commands,
        auto_approve_edits=auto_approve_edits,
        context_max_files=6,
        context_max_bytes_per_file=4_000,
    )


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


class _ToolClient:
    def __init__(self, calls: list[dict[str, str]]) -> None:
        self.calls = calls
        self.tool_outputs: list[dict[str, Any]] = []

    def create_initial_response(self, **_kwargs: object) -> dict[str, Any]:
        return {"id": "response-1", "output": self.calls}

    def create_tool_response(
        self,
        *,
        tool_outputs: list[dict[str, Any]],
        **_kwargs: object,
    ) -> dict[str, Any]:
        self.tool_outputs = tool_outputs
        return {"id": "response-2", "output_text": "done", "output": []}


def _call(name: str, call_id: str, arguments: dict[str, object]) -> dict[str, str]:
    return {
        "type": "function_call",
        "name": name,
        "arguments": json.dumps(arguments),
        "call_id": call_id,
    }


def test_policy_registry_matches_every_exposed_tool_schema() -> None:
    schema_names = {definition["name"] for definition in TOOL_DEFINITIONS}

    assert schema_names == exposed_tool_names()
    assert set(TOOL_POLICIES) == schema_names | {"write_file"}
    assert TOOL_POLICIES["apply_patch"].effect == "workspace_write"
    assert TOOL_POLICIES["apply_patch"].approval_group == "edits"
    assert TOOL_POLICIES["run_verification"].effect == "process"
    assert TOOL_POLICIES["run_command"].approval_group == "commands"
    assert TOOL_POLICIES["write_file"].exposed is False


def test_approval_request_codec_round_trip() -> None:
    request = ApprovalRequest(
        call_id="call-1",
        action="run_command",
        summary="Run tests",
        arguments_sha256=hash_tool_arguments('{"argv":["pytest"]}'),
        details={
            "argv": ("pytest",),
            "cwd": "D:/code/project",
            "timeout_ms": 30_000,
            "shell": False,
        },
    )

    restored = approval_request_from_dict(approval_request_to_dict(request))

    assert restored == request


def test_agent_persists_approval_before_patch_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    patch = _add_patch()
    raw_arguments = json.dumps({"patch": patch})
    client = _ToolClient([_call("apply_patch", "call-patch", {"patch": patch})])
    original_apply = tools_module.apply_patch_plan

    def approval_handler(request: ApprovalRequest) -> ApprovalDecision:
        events = store.load(store.list_sessions()[0].session_id)
        assert events[-1].type == "tool.started"
        assert not (tmp_path / "created.txt").exists()
        assert request.arguments_sha256 == hash_tool_arguments(raw_arguments)
        return create_approval_decision(
            request,
            approved=True,
            source="interactive",
        )

    def audited_apply(plan: object) -> None:
        events = store.load(store.list_sessions()[0].session_id)
        assert events[-1].type == "approval.decided"
        original_apply(plan)  # type: ignore[arg-type]

    monkeypatch.setattr(tools_module, "apply_patch_plan", audited_apply)

    report = run_agent_with_report(
        "create a file",
        _config(tmp_path),
        model_client=client,
        session_store=store,
        approval_handler=approval_handler,
    )

    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "created\n"
    events = store.load(report.session_id or "")
    event_types = [event.type for event in events]
    started_index = event_types.index("tool.started")
    assert event_types[started_index : started_index + 4] == [
        "tool.started",
        "approval.decided",
        "tool.finished",
        "checkpoint.saved",
    ]
    started = events[started_index]
    assert started.payload["effect"] == "workspace_write"
    assert started.payload["requires_approval"] is True
    assert started.payload["arguments_sha256"] == hash_tool_arguments(raw_arguments)
    finished = events[started_index + 2]
    touched = finished.payload["touched_file_hashes"]
    assert touched["created.txt"] == hashlib.sha256(b"created\n").hexdigest()


def test_auto_approved_edit_and_command_are_audited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch = _add_patch("auto.txt", "auto\n")
    client = _ToolClient(
        [
            _call("apply_patch", "call-edit", {"patch": patch}),
            _call(
                "run_command",
                "call-command",
                {"argv": ["echo", "audit"], "timeout_ms": 1_234},
            ),
        ]
    )
    store = SessionStore(tmp_path)

    def fake_argv(
        argv: tuple[str, ...],
        workspace: str | Path,
        cwd: str,
        timeout_ms: int,
        *,
        policy_decision: object,
        approval_granted: bool,
        command_spec: object,
    ) -> ToolResult:
        return ToolResult(
            ok=True,
            output="exit code: 0\naudit",
            data={
                "type": "secure_command_result",
                "argv": list(argv),
                "cwd": cwd,
                "shell": False,
                "timeout_ms": timeout_ms,
                "backend": "host",
                "sandboxed": False,
                "network_mode": "host",
                "image_digest": None,
                "exit_code": 0,
                "timed_out": False,
                "duration_ms": 1,
                "status": "passed",
                "policy_version": policy_decision.policy_version,
                "rule_id": policy_decision.rule_id,
                "disposition": policy_decision.disposition,
                "reasons": list(policy_decision.reasons),
                "normalized_executable": policy_decision.normalized_executable,
                "requires_approval": policy_decision.requires_approval,
                "requires_sandbox": policy_decision.requires_sandbox,
            },
        )

    monkeypatch.setattr(tools_module, "_run_argv_command", fake_argv)

    report = run_agent_with_report(
        "edit and inspect",
        _config(
            tmp_path,
            auto_approve_edits=True,
            auto_approve_commands=True,
        ),
        model_client=client,
        session_store=store,
    )

    events = store.load(report.session_id or "")
    approvals = [event for event in events if event.type == "approval.decided"]
    assert [event.payload["decision"]["source"] for event in approvals] == [
        "auto_policy",
        "auto_policy",
    ]
    assert [event.payload["decision"]["action"] for event in approvals] == [
        "apply_patch",
        "run_command",
    ]
    policy_event = next(
        event
        for event in events
        if event.type == "security.policy_evaluated"
    )
    command_started = next(
        event
        for event in events
        if event.type == "tool.started"
        and event.payload["name"] == "run_command"
    )
    command_approval = next(
        event
        for event in approvals
        if event.payload["decision"]["action"] == "run_command"
    )
    assert command_started.seq < policy_event.seq < command_approval.seq
    assert policy_event.payload["policy"]["rule_id"] == "allow.interactive_host"
    command_finished = next(
        event
        for event in events
        if event.type == "tool.finished" and event.payload["name"] == "run_command"
    )
    execution = command_finished.payload["execution"]
    assert execution == {
        "argv": ("echo", "audit"),
        "cwd": ".",
        "shell": False,
        "timeout_ms": 1_234,
        "backend": "host",
        "sandboxed": False,
        "network_mode": "host",
        "image_digest": None,
        "exit_code": 0,
        "timed_out": False,
        "duration_ms": 1,
        "status": "passed",
        "policy_version": 1,
        "rule_id": "allow.interactive_host",
        "disposition": "approval_required",
        "reasons": (
            "This narrowly scoped host command requires explicit approval.",
        ),
        "normalized_executable": "echo",
        "requires_approval": True,
        "requires_sandbox": False,
    }


def test_denial_is_persisted_and_prevents_command_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    client = _ToolClient(
        [_call("run_command", "call-denied", {"argv": ["echo", "denied"]})]
    )

    def denied(request: ApprovalRequest) -> ApprovalDecision:
        return create_approval_decision(
            request,
            approved=False,
            source="interactive",
        )

    def forbidden_argv(*_args: object, **_kwargs: object) -> ToolResult:
        raise AssertionError("denied command must not execute")

    monkeypatch.setattr(tools_module, "_run_argv_command", forbidden_argv)

    report = run_agent_with_report(
        "do not run",
        _config(tmp_path),
        model_client=client,
        session_store=store,
        approval_handler=denied,
    )

    events = store.load(report.session_id or "")
    approval = next(event for event in events if event.type == "approval.decided")
    assert approval.payload["decision"]["outcome"] == "denied"
    assert [event.type for event in events].count("tool.finished") == 1
    assert json.loads(client.tool_outputs[0]["output"])["ok"] is False


def test_approval_callback_exception_writes_denied_audit_event(
    tmp_path: Path,
) -> None:
    patch = _add_patch("blocked.txt", "blocked\n")
    store = SessionStore(tmp_path)
    client = _ToolClient(
        [_call("apply_patch", "call-error", {"patch": patch})]
    )

    def broken_handler(_request: ApprovalRequest) -> ApprovalDecision:
        raise RuntimeError("approval backend unavailable")

    report = run_agent_with_report(
        "blocked edit",
        _config(tmp_path),
        model_client=client,
        session_store=store,
        approval_handler=broken_handler,
    )

    assert not (tmp_path / "blocked.txt").exists()
    events = store.load(report.session_id or "")
    approval = next(event for event in events if event.type == "approval.decided")
    assert approval.payload["decision"]["outcome"] == "denied"
    assert approval.payload["handler_error"]["exception_type"] == "RuntimeError"
    assert json.loads(client.tool_outputs[0]["output"])["ok"] is False


@pytest.mark.parametrize("mismatch", ["call_id", "action", "arguments_sha256"])
def test_mismatched_approval_decisions_are_rejected(mismatch: str) -> None:
    request = ApprovalRequest(
        call_id="call-1",
        action="apply_patch",
        summary="Apply patch",
        arguments_sha256=hash_tool_arguments("{}"),
        details={"workspace": "D:/code", "change_summary": "one file", "patch": "diff"},
    )
    values = {
        "approval_id": "approval-1",
        "call_id": request.call_id,
        "action": request.action,
        "summary": request.summary,
        "outcome": "approved",
        "source": "interactive",
        "decided_at": "2026-07-14T03:15:00Z",
        "arguments_sha256": request.arguments_sha256,
    }
    values[mismatch] = (
        hash_tool_arguments("different")
        if mismatch == "arguments_sha256"
        else "different"
    )

    with pytest.raises(ValueError, match=mismatch):
        validate_approval_decision(request, ApprovalDecision(**values))  # type: ignore[arg-type]


def test_patch_result_contains_changed_paths_and_before_after_hashes(
    tmp_path: Path,
) -> None:
    target = tmp_path / "value.txt"
    target.write_bytes(b"before\n")
    patch = "\n".join(
        [
            "--- a/value.txt",
            "+++ b/value.txt",
            "@@ -1 +1 @@",
            "-before",
            "+after",
            "",
        ]
    )

    def approve(request: ApprovalRequest) -> ApprovalDecision:
        return create_approval_decision(
            request,
            approved=True,
            source="interactive",
        )

    result = execute_tool(
        _config(tmp_path),
        "apply_patch",
        json.dumps({"patch": patch}),
        approval_handler=approve,
        call_id="call-patch",
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["changed_paths"] == ["value.txt"]
    assert result.data["diff_sha256"] == hashlib.sha256(
        patch.encode("utf-8")
    ).hexdigest()
    assert result.data["file_changes"] == [
        {
            "path": "value.txt",
            "change_type": "modify",
            "before_sha256": hashlib.sha256(b"before\n").hexdigest(),
            "after_sha256": hashlib.sha256(b"after\n").hexdigest(),
        }
    ]
    assert result.data["touched_file_hashes"] == {
        "value.txt": hashlib.sha256(b"after\n").hexdigest()
    }


def test_verification_approval_and_result_preserve_exact_execution_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        verification_module,
        "_python_module_available",
        lambda _name: True,
    )
    patch_verification_runner(
        monkeypatch,
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="1 passed\n",
            stderr="",
        ),
    )
    captured: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest) -> ApprovalDecision:
        captured.append(request)
        return create_approval_decision(
            request,
            approved=True,
            source="interactive",
        )

    result = execute_tool(
        _config(tmp_path),
        "run_verification",
        json.dumps({"command_id": "python:pytest", "timeout_ms": 4_321}),
        approval_handler=approve,
        call_id="call-verify",
    )

    assert result.ok is True
    assert len(captured) == 1
    request = captured[0]
    assert request.action == "run_verification"
    assert request.details["argv"] == tuple(result.data["argv"])  # type: ignore[index]
    assert request.details["cwd"] == result.data["cwd"]  # type: ignore[index]
    assert request.details["timeout_ms"] == 4_321
    assert result.data is not None
    assert result.data["timeout_ms"] == 4_321
    assert result.data["exit_code"] == 0
    assert result.data["shell"] is False


def test_run_command_result_records_argv_cwd_timeout_and_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_kwargs: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured_kwargs.update(kwargs)
        assert argv == ["echo", "audit"]
        return SimpleNamespace(returncode=7, stdout="out\n", stderr="err\n")

    patch_tools_runner(monkeypatch, fake_run)

    def approve(request: ApprovalRequest) -> ApprovalDecision:
        assert request.details["argv"] == ("echo", "audit")
        assert request.details["cwd"] == "."
        assert request.details["backend"] == "host"
        assert request.details["sandboxed"] is False
        assert request.details["timeout_ms"] == 2_500
        return create_approval_decision(
            request,
            approved=True,
            source="interactive",
        )

    result = execute_tool(
        _config(tmp_path),
        "run_command",
        json.dumps({"argv": ["echo", "audit"], "timeout_ms": 2_500}),
        approval_handler=approve,
        call_id="call-command",
    )

    assert result.ok is False
    assert result.data is not None
    assert result.data["argv"] == ["echo", "audit"]
    assert result.data["cwd"] == "."
    assert result.data["shell"] is False
    assert result.data["timeout_ms"] == 2_500
    assert result.data["exit_code"] == 7
    assert result.data["timed_out"] is False
    assert captured_kwargs["cwd"] == str(tmp_path.resolve())
    assert captured_kwargs["timeout"] == 2.5

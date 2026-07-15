"""M4 integration tests for durable recovery across every safety boundary."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coding_agent.agent as agent_module
import coding_agent.tools as tools_module
import coding_agent.verification as verification_module
from coding_agent.agent import (
    FaultPoint,
    resume_agent_with_report,
    run_agent_with_report,
)
from coding_agent.sessions.reducer import rebuild_state
from coding_agent.sessions.replay import build_session_replay_payload
from coding_agent.sessions.store import SessionStore
from coding_agent.types import AgentConfig, ToolResult


def _config(workspace: Path, *, max_turns: int = 8) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=max_turns,
        permission_mode="workspace-write",
        auto_approve_commands=True,
        auto_approve_edits=True,
        context_max_files=6,
        context_max_bytes_per_file=8_000,
        max_fix_attempts=3,
    )


def _function_call(
    response_id: str,
    call_id: str,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    return {
        "id": response_id,
        "output": [
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            }
        ],
    }


def _final_response() -> dict[str, object]:
    return {
        "id": "response-final",
        "output": [],
        "output_text": "Fixed and verified.",
    }


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


class _ReadThenFinalClient:
    def __init__(
        self,
        store: SessionStore,
        captured_output: pytest.CaptureFixture[str],
    ) -> None:
        self.store = store
        self.captured_output = captured_output
        self.initial_count = 0
        self.continuation_count = 0
        self.session_printed_before_model = False

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        self.initial_count += 1
        session_id = _latest_session_id(self.store)
        output = self.captured_output.readouterr().out
        assert f"session: {session_id}" in output
        self.session_printed_before_model = True
        return _function_call(
            "response-list",
            "call-list",
            "list_files",
            {"path": "."},
        )

    def create_tool_response(
        self,
        *,
        tool_outputs: list[dict[str, Any]],
        **_kwargs: object,
    ) -> dict[str, object]:
        self.continuation_count += 1
        assert len(tool_outputs) == 1
        payload = json.loads(tool_outputs[0]["output"])
        assert payload["ok"] is True
        return _final_response()


@pytest.mark.parametrize(
    ("fault_point", "expected_tool_executions", "expects_safe_retry"),
    [
        ("after_model_response", 1, False),
        ("after_tool_side_effect", 2, True),
        ("after_tool_finished", 1, False),
        ("before_model_continuation", 1, False),
    ],
)
def test_resume_recovers_every_fault_injection_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault_point: FaultPoint,
    expected_tool_executions: int,
    expects_safe_retry: bool,
) -> None:
    """Every documented fault point must resume without losing or duplicating work."""

    store = SessionStore(tmp_path)
    client = _ReadThenFinalClient(store, capsys)
    executions = 0

    def counted_execute_tool(*_args: object, **_kwargs: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult(ok=True, output="file pyproject.toml")

    monkeypatch.setattr(agent_module, "execute_tool", counted_execute_tool)

    with pytest.raises(KeyboardInterrupt, match=fault_point):
        run_agent_with_report(
            "Inspect the repository.",
            _config(tmp_path),
            model_client=client,
            session_store=store,
            fault_injector=_interrupt_at(fault_point),
        )

    session_id = _latest_session_id(store)
    report = resume_agent_with_report(
        session_id,
        tmp_path,
        model_client=client,
        session_store=store,
    )

    assert report.answer == "Fixed and verified."
    assert report.session_id == session_id
    assert executions == expected_tool_executions
    assert client.initial_count == 1
    assert client.continuation_count == 1
    assert client.session_printed_before_model is True

    events = store.load(session_id)
    event_types = [event.type for event in events]
    state = rebuild_state(events)
    assert "session.interrupted" in event_types
    assert "session.resumed" in event_types
    assert event_types[-1] == "session.completed"
    assert state.status == "completed"
    assert state.phase == "completed"
    assert state.pending_tool_calls == ()
    assert state.completed_call_ids == frozenset({"call-list"})

    safe_retries = [
        event
        for event in events
        if event.type == "tool.recovered"
        and event.payload.get("reason") == "safe_retry"
    ]
    assert bool(safe_retries) is expects_safe_retry
    if safe_retries:
        assert safe_retries[0].payload["completed"] is False
        assert safe_retries[0].payload["requires_reapproval"] is False


class _InterruptedRepairClient:
    def __init__(self, patch: str) -> None:
        self.patch = patch
        self.continuation_count = 0
        self.requested_tools = ["run_verification"]

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        return _function_call(
            "response-verify-before",
            "call-verify-before",
            "run_verification",
            {"command_id": "python:pytest"},
        )

    def create_tool_response(
        self,
        *,
        tool_outputs: list[dict[str, Any]],
        **_kwargs: object,
    ) -> dict[str, object]:
        self.continuation_count += 1
        assert len(tool_outputs) == 1
        payload = json.loads(tool_outputs[0]["output"])

        if self.continuation_count == 1:
            assert payload["ok"] is False
            assert payload["data"]["status"] == "failed"
            self.requested_tools.append("apply_patch")
            return _function_call(
                "response-patch",
                "call-patch",
                "apply_patch",
                {"patch": self.patch},
            )

        if self.continuation_count == 2:
            assert payload["ok"] is True
            assert payload["data"]["recovered"] is True
            assert (
                payload["data"]["recovery_reason"]
                == "patch_after_hash_match"
            )
            self.requested_tools.append("run_verification")
            return _function_call(
                "response-verify-after",
                "call-verify-after",
                "run_verification",
                {"command_id": "python:pytest"},
            )

        if self.continuation_count == 3:
            assert payload["ok"] is True
            assert payload["data"]["status"] == "passed"
            return _final_response()

        raise AssertionError(
            f"Unexpected continuation {self.continuation_count}."
        )


def test_medium_repository_resumes_failed_verification_and_applies_patch_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the complete failed → patch → interrupt → resume → passed flow."""

    target_path = "src/calculator.py"
    unrelated_path = "src/unrelated.py"
    _create_medium_repository(tmp_path)
    original_unrelated = (tmp_path / unrelated_path).read_text(encoding="utf-8")
    patch = _replace_return_patch(target_path, "value + 1", "value + 2")
    client = _InterruptedRepairClient(patch)
    store = SessionStore(tmp_path)

    monkeypatch.setattr(
        verification_module,
        "_python_module_available",
        lambda _name: True,
    )
    verification_executions = 0

    def fake_verification_run(
        *_args: object,
        **_kwargs: object,
    ) -> SimpleNamespace:
        nonlocal verification_executions
        verification_executions += 1
        source = (tmp_path / target_path).read_text(encoding="utf-8")
        if "return value + 2" in source:
            return SimpleNamespace(
                returncode=0,
                stdout="1 passed\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=1,
            stdout="collected 1 item\n",
            stderr=(
                "tests/test_calculator.py:5: AssertionError: expected 4\n"
                f"{target_path}:2: calculate returned the wrong value\n"
            ),
        )

    monkeypatch.setattr(
        verification_module.subprocess,
        "run",
        fake_verification_run,
    )

    original_apply_patch_plan = tools_module.apply_patch_plan
    patch_applications = 0

    def counted_apply_patch_plan(plan: object) -> None:
        nonlocal patch_applications
        patch_applications += 1
        original_apply_patch_plan(plan)  # type: ignore[arg-type]

    monkeypatch.setattr(
        tools_module,
        "apply_patch_plan",
        counted_apply_patch_plan,
    )

    with pytest.raises(KeyboardInterrupt, match="after_tool_side_effect:2"):
        run_agent_with_report(
            "Fix the failing calculator test.",
            _config(tmp_path),
            model_client=client,
            session_store=store,
            fault_injector=_interrupt_at(
                "after_tool_side_effect",
                occurrence=2,
            ),
        )

    session_id = _latest_session_id(store)
    assert patch_applications == 1
    assert (tmp_path / target_path).read_text(encoding="utf-8").endswith(
        "return value + 2\n"
    )

    report = resume_agent_with_report(
        session_id,
        tmp_path,
        model_client=client,
        session_store=store,
    )

    assert report.answer == "Fixed and verified."
    assert report.final_status == "passed"
    assert [result.status for result in report.verifications] == [
        "failed",
        "passed",
    ]
    assert [result.attempt for result in report.verifications] == [1, 2]
    assert client.requested_tools == [
        "run_verification",
        "apply_patch",
        "run_verification",
    ]
    assert verification_executions == 2
    assert patch_applications == 1
    assert (tmp_path / unrelated_path).read_text(
        encoding="utf-8"
    ) == original_unrelated

    events = store.load(session_id)
    event_types = [event.type for event in events]
    assert "session.interrupted" in event_types
    assert "session.resumed" in event_types
    assert "tool.recovered" in event_types
    assert event_types[-1] == "session.completed"

    recovered_patch = next(
        event
        for event in events
        if event.type == "tool.recovered"
        and event.payload.get("call_id") == "call-patch"
    )
    assert recovered_patch.payload["completed"] is True
    assert recovered_patch.payload["reason"] == "patch_after_hash_match"

    verification_events = [
        event
        for event in events
        if event.type == "verification.recorded"
    ]
    assert [event.payload["result"]["status"] for event in verification_events] == [
        "failed",
        "passed",
    ]

    approvals = [
        event.payload["decision"]
        for event in events
        if event.type == "approval.decided"
    ]
    assert [decision["action"] for decision in approvals] == [
        "run_verification",
        "apply_patch",
        "run_verification",
    ]
    assert [decision["outcome"] for decision in approvals] == [
        "approved",
        "approved",
        "approved",
    ]
    assert [decision["source"] for decision in approvals] == [
        "auto_policy",
        "auto_policy",
        "auto_policy",
    ]

    state = rebuild_state(events)
    assert state.status == "completed"
    assert state.pending_tool_calls == ()
    assert state.completed_call_ids == frozenset(
        {"call-verify-before", "call-patch", "call-verify-after"}
    )

    replay = build_session_replay_payload(
        SessionStore(tmp_path, read_only=True),
        session_id,
    )
    assert replay["session"]["status"] == "completed"
    assert replay["session"]["final_status"] == "passed"
    assert [item["status"] for item in replay["verifications"]] == [
        "failed",
        "passed",
    ]
    assert [item["action"] for item in replay["approvals"]] == [
        "run_verification",
        "apply_patch",
        "run_verification",
    ]


def _create_medium_repository(workspace: Path) -> None:
    (workspace / "src").mkdir()
    (workspace / "tests").mkdir()
    (workspace / "AGENTS.md").write_text(
        "# Test instructions\n\nKeep arithmetic functions deterministic.\n",
        encoding="utf-8",
    )
    (workspace / ".gitignore").write_text(
        ".coding-agent/\nbuild/\n*.log\n",
        encoding="utf-8",
    )
    (workspace / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    (workspace / "src" / "calculator.py").write_text(
        "def calculate(value: int) -> int:\n    return value + 1\n",
        encoding="utf-8",
    )
    (workspace / "src" / "unrelated.py").write_text(
        "def sentinel() -> str:\n    return 'unchanged'\n",
        encoding="utf-8",
    )
    (workspace / "tests" / "test_calculator.py").write_text(
        "from src.calculator import calculate\n\n"
        "def test_calculate() -> None:\n"
        "    actual = calculate(2)\n"
        "    assert actual == 4\n",
        encoding="utf-8",
    )
    for index in range(24):
        (workspace / "src" / f"module_{index:02d}.py").write_text(
            f"VALUE_{index:02d} = {index}\n",
            encoding="utf-8",
        )


def _replace_return_patch(path: str, before: str, after: str) -> str:
    return "\n".join(
        [
            f"--- a/{path}",
            f"+++ b/{path}",
            "@@ -1,2 +1,2 @@",
            " def calculate(value: int) -> int:",
            f"-    return {before}",
            f"+    return {after}",
            "",
        ]
    )

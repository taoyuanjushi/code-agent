"""Tests for verification-aware agent repair loops."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.process_fakes import patch_verification_runner

import coding_agent.verification as verification
from coding_agent.agent import (
    _verification_failure_note,
    _verification_final_status,
    run_agent_with_report,
)
from coding_agent.tools import VerificationToolState
from coding_agent.types import AgentConfig
from coding_agent.verification import VerificationResult


class RepairLoopClient:
    def __init__(self) -> None:
        self.step = 0
        self.requested_tools: list[str] = []

    def create_initial_response(
        self,
        *,
        config: AgentConfig,
        instructions: str,
        input_text: str,
    ) -> dict[str, Any]:
        del config, input_text
        assert "discover_verification_commands" in instructions
        assert "run_verification" in instructions
        return self._next_tool(
            "discover_verification_commands",
            {},
        )

    def create_tool_response(
        self,
        *,
        config: AgentConfig,
        previous_response_id: str,
        tool_outputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        del config, previous_response_id
        payload = json.loads(tool_outputs[0]["output"])
        expected = self.requested_tools[-1]

        if expected == "discover_verification_commands":
            assert payload["ok"] is True
            assert payload["data"]["commands"][0]["id"] == "python:pytest"
            return self._next_tool("search_text", {"pattern": "calculate_refund"})
        if expected == "search_text" and self.step == 2:
            assert "service.py" in payload["output"]
            return self._next_tool(
                "read_many_files",
                {"paths": ["service.py", "tests/test_service.py"]},
            )
        if expected == "read_many_files" and self.step == 3:
            assert "return 0" in payload["output"]
            return self._next_tool("apply_patch", {"patch": _first_patch()})
        if expected == "apply_patch" and self.step == 4:
            assert payload["ok"] is True
            return self._next_tool(
                "run_verification",
                {"command_id": "python:pytest"},
            )
        if expected == "run_verification" and self.step == 5:
            assert payload["ok"] is False
            assert payload["data"]["status"] == "failed"
            assert "AssertionError" in payload["output"]
            return self._next_tool("search_text", {"pattern": "calculate_refund"})
        if expected == "search_text" and self.step == 6:
            return self._next_tool(
                "read_many_files",
                {"paths": ["service.py", "tests/test_service.py"]},
            )
        if expected == "read_many_files" and self.step == 7:
            assert "return 1" in payload["output"]
            return self._next_tool("apply_patch", {"patch": _second_patch()})
        if expected == "apply_patch" and self.step == 8:
            assert payload["ok"] is True
            return self._next_tool(
                "run_verification",
                {"command_id": "python:pytest"},
            )
        if expected == "run_verification" and self.step == 9:
            assert payload["ok"] is True
            assert payload["data"]["status"] == "passed"
            return {
                "id": "response-final",
                "output_text": "Fixed calculate_refund and verified the tests.",
                "output": [],
            }
        raise AssertionError(f"Unexpected step {self.step} after {expected}")

    def _next_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        self.step += 1
        self.requested_tools.append(name)
        return {
            "id": f"response-{self.step}",
            "output": [
                {
                    "type": "function_call",
                    "name": name,
                    "arguments": json.dumps(arguments),
                    "call_id": f"call-{self.step}",
                }
            ],
        }


def test_agent_records_failed_then_passed_verification_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n",
        encoding="utf-8",
    )
    (tmp_path / "service.py").write_text(
        "def calculate_refund() -> int:\n    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_service.py").write_text(
        "from service import calculate_refund\n\n"
        "def test_refund() -> None:\n"
        "    assert calculate_refund() == 2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(verification, "_python_module_available", lambda _name: True)

    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        source = (tmp_path / "service.py").read_text(encoding="utf-8")
        if "return 2" in source:
            return SimpleNamespace(returncode=0, stdout="1 passed\n", stderr="")
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=(
                "Traceback (most recent call last):\n"
                '  File "tests/test_service.py", line 4, in test_refund\n'
                "AssertionError: calculate_refund expected 2\n"
            ),
        )

    patch_verification_runner(monkeypatch, fake_run)
    config = AgentConfig(
        workspace=str(tmp_path),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=12,
        permission_mode="workspace-write",
        auto_approve_commands=True,
        auto_approve_edits=True,
        context_max_files=6,
        context_max_bytes_per_file=8_000,
        max_fix_attempts=3,
    )
    client = RepairLoopClient()

    report = run_agent_with_report(
        "Fix the failing refund test.",
        config,
        model_client=client,
    )

    assert report.answer == "Fixed calculate_refund and verified the tests."
    assert report.final_status == "passed"
    assert [result.status for result in report.verifications] == ["failed", "passed"]
    assert [result.attempt for result in report.verifications] == [1, 2]
    assert client.requested_tools == [
        "discover_verification_commands",
        "search_text",
        "read_many_files",
        "apply_patch",
        "run_verification",
        "search_text",
        "read_many_files",
        "apply_patch",
        "run_verification",
    ]
    assert (tmp_path / "service.py").read_text(encoding="utf-8").endswith(
        "return 2\n"
    )


def _first_patch() -> str:
    return "\n".join(
        [
            "--- a/service.py",
            "+++ b/service.py",
            "@@ -1,2 +1,2 @@",
            " def calculate_refund() -> int:",
            "-    return 0",
            "+    return 1",
            "",
        ]
    )


def _second_patch() -> str:
    return "\n".join(
        [
            "--- a/service.py",
            "+++ b/service.py",
            "@@ -1,2 +1,2 @@",
            " def calculate_refund() -> int:",
            "-    return 1",
            "+    return 2",
            "",
        ]
    )


def test_verification_state_invalidates_passes_after_a_later_edit(
    tmp_path: Path,
) -> None:
    state = VerificationToolState(task="Fix tests", max_fix_attempts=3)
    passed = VerificationResult(
        command_id="python:pytest",
        kind="test",
        status="passed",
        argv=("python", "-m", "pytest"),
        cwd=str(tmp_path.resolve()),
        exit_code=0,
        duration_ms=10,
        output="1 passed",
        truncated=False,
        omitted_lines=0,
        omitted_bytes=0,
        attempt=1,
    )
    state.verification_history.append(passed)
    state.passed_generations[passed.command_id] = state.edit_generation

    assert _verification_final_status(state) == "passed"

    state.edit_generation += 1
    state.after_edit = True

    assert _verification_final_status(state) == "failed"
    assert _verification_failure_note(state) == (
        "Verification results are stale after the latest edit and were not "
        "rerun: python:pytest."
    )

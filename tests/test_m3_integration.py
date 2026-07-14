"""Medium M3 integration test for diagnostic-driven iterative repair."""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coding_agent.verification as verification
from coding_agent.agent import run_agent_with_report
from coding_agent.types import AgentConfig


class DiagnosticRepairClient:
    def __init__(self) -> None:
        self.step = 0
        self.requested_tools: list[str] = []
        self.target_path: str | None = None
        self.unrelated_path: str | None = None
        self.diagnostic_path: str | None = None
        self.second_read_paths: list[str] = []

    def create_initial_response(
        self,
        *,
        config: AgentConfig,
        instructions: str,
        input_text: str,
    ) -> dict[str, Any]:
        del config
        assert "Always preserve integer refund values." in instructions
        assert "GENERATED_REFUND_MARKER" not in input_text
        assert "IGNORED_LOG_MARKER" not in input_text
        return self._next_tool("discover_verification_commands", {})

    def create_tool_response(
        self,
        *,
        config: AgentConfig,
        previous_response_id: str,
        tool_outputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        del config, previous_response_id
        assert len(tool_outputs) == 1
        payload = json.loads(tool_outputs[0]["output"])
        expected = self.requested_tools[-1]

        if expected == "discover_verification_commands" and self.step == 1:
            assert payload["ok"] is True
            assert payload["data"]["commands"][0]["id"] == "python:pytest"
            return self._next_tool("search_text", {"pattern": "calculate_refund"})

        if expected == "search_text" and self.step == 2:
            assert "GENERATED_REFUND_MARKER" not in payload["output"]
            assert "IGNORED_LOG_MARKER" not in payload["output"]
            paths = _paths_from_search_output(payload["output"])
            assert "tests/test_refund.py" in paths
            assert len([path for path in paths if path.startswith("src/")]) == 2
            return self._next_tool("read_many_files", {"paths": paths})

        if expected == "read_many_files" and self.step == 3:
            module_match = re.search(
                r"from (src\.[A-Za-z_][\w.]*) import calculate_refund",
                payload["output"],
            )
            assert module_match is not None
            self.target_path = module_match.group(1).replace(".", "/") + ".py"
            source_paths = [
                path
                for path in _section_paths(payload["output"])
                if path.startswith("src/") and path != self.target_path
            ]
            assert len(source_paths) == 1
            self.unrelated_path = source_paths[0]
            return self._next_tool(
                "apply_patch",
                {"patch": _value_patch(self.target_path, 0, 1)},
            )

        if expected == "apply_patch" and self.step == 4:
            assert payload["ok"] is True
            assert payload["data"]["repair_attempts"] == 0
            return self._next_tool(
                "run_verification",
                {"command_id": "python:pytest"},
            )

        if expected == "run_verification" and self.step == 5:
            assert payload["ok"] is False
            assert payload["data"]["status"] == "failed"
            diagnostic = re.search(
                r"(src/[A-Za-z0-9_./-]+\.py):\d+",
                payload["output"],
            )
            assert diagnostic is not None
            self.diagnostic_path = diagnostic.group(1)
            assert self.diagnostic_path == self.target_path
            return self._next_tool("search_text", {"pattern": "calculate_refund"})

        if expected == "search_text" and self.step == 6:
            assert self.diagnostic_path is not None
            assert self.diagnostic_path in payload["output"]
            assert "GENERATED_REFUND_MARKER" not in payload["output"]
            assert "IGNORED_LOG_MARKER" not in payload["output"]
            self.second_read_paths = [
                self.diagnostic_path,
                "tests/test_refund.py",
            ]
            return self._next_tool(
                "read_many_files",
                {"paths": self.second_read_paths},
            )

        if expected == "read_many_files" and self.step == 7:
            assert self.target_path is not None
            assert f"===== {self.target_path} =====" in payload["output"]
            assert "return 1" in payload["output"]
            assert self.unrelated_path is not None
            assert f"===== {self.unrelated_path} =====" not in payload["output"]
            return self._next_tool(
                "apply_patch",
                {"patch": _value_patch(self.target_path, 1, 2)},
            )

        if expected == "apply_patch" and self.step == 8:
            assert payload["ok"] is True
            assert payload["data"]["repair_attempts"] == 1
            return self._next_tool(
                "run_verification",
                {"command_id": "python:pytest"},
            )

        if expected == "run_verification" and self.step == 9:
            assert payload["ok"] is True
            assert payload["data"]["status"] == "passed"
            return {
                "id": "response-final",
                "output_text": "Fixed the refund implementation after following the diagnostic.",
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


def test_medium_repository_repairs_the_file_named_by_failure_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_path = "src/active_refund_engine.py"
    unrelated_path = "src/refund_engine_legacy.py"
    _create_medium_repository(
        tmp_path,
        target_path=target_path,
        unrelated_path=unrelated_path,
    )
    monkeypatch.setattr(verification, "_python_module_available", lambda _name: True)

    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        source = (tmp_path / target_path).read_text(encoding="utf-8")
        if "return 2" in source:
            return SimpleNamespace(returncode=0, stdout="1 passed\n", stderr="")
        return SimpleNamespace(
            returncode=1,
            stdout="collected 1 item\n",
            stderr=(
                "tests/test_refund.py:5: AssertionError: expected refund 2\n"
                f"{target_path}:2: calculate_refund returned the wrong value\n"
            ),
        )

    monkeypatch.setattr(verification.subprocess, "run", fake_run)
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
    client = DiagnosticRepairClient()

    report = run_agent_with_report(
        "Fix the failing refund test.",
        config,
        model_client=client,
    )

    assert report.final_status == "passed"
    assert [result.status for result in report.verifications] == ["failed", "passed"]
    assert [result.attempt for result in report.verifications] == [1, 2]
    assert client.target_path == target_path
    assert client.diagnostic_path == target_path
    assert client.unrelated_path == unrelated_path
    assert client.second_read_paths == [target_path, "tests/test_refund.py"]
    assert (tmp_path / target_path).read_text(encoding="utf-8").endswith(
        "return 2\n"
    )
    assert (tmp_path / unrelated_path).read_text(encoding="utf-8").endswith(
        "return 99\n"
    )


def _create_medium_repository(
    workspace: Path,
    *,
    target_path: str,
    unrelated_path: str,
) -> None:
    (workspace / "src").mkdir()
    (workspace / "tests").mkdir()
    (workspace / "build").mkdir()
    (workspace / "AGENTS.md").write_text(
        "# Test instructions\n\nAlways preserve integer refund values.\n",
        encoding="utf-8",
    )
    (workspace / ".gitignore").write_text(
        "build/\n*.log\n",
        encoding="utf-8",
    )
    (workspace / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n",
        encoding="utf-8",
    )
    (workspace / target_path).write_text(
        "def calculate_refund() -> int:\n    return 0\n",
        encoding="utf-8",
    )
    (workspace / unrelated_path).write_text(
        "def calculate_refund() -> int:\n    return 99\n",
        encoding="utf-8",
    )
    (workspace / "tests" / "test_refund.py").write_text(
        "from src.active_refund_engine import calculate_refund\n\n"
        "def test_refund() -> None:\n"
        "    actual = calculate_refund()\n"
        "    assert actual == 2\n",
        encoding="utf-8",
    )
    (workspace / "build" / "generated_refund.py").write_text(
        "GENERATED_REFUND_MARKER = 'calculate_refund'\n",
        encoding="utf-8",
    )
    (workspace / "debug.log").write_text(
        "IGNORED_LOG_MARKER calculate_refund\n",
        encoding="utf-8",
    )


def _paths_from_search_output(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines():
        match = re.match(r"([^:]+):\d+:\d+:", line)
        if match is not None and match.group(1) not in paths:
            paths.append(match.group(1))
    return paths


def _section_paths(output: str) -> list[str]:
    return re.findall(r"^===== ([^=]+) =====$", output, flags=re.MULTILINE)


def _value_patch(path: str, old: int, new: int) -> str:
    return "\n".join(
        [
            f"--- a/{path}",
            f"+++ b/{path}",
            "@@ -1,2 +1,2 @@",
            " def calculate_refund() -> int:",
            f"-    return {old}",
            f"+    return {new}",
            "",
        ]
    )

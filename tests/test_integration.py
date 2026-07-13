import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from coding_agent.agent import run_agent
from coding_agent.types import AgentConfig


class FixFailingTestClient:
    def __init__(self) -> None:
        self.step = 0
        self.tool_results: list[dict[str, Any]] = []

    def create_initial_response(
        self,
        *,
        config: AgentConfig,
        instructions: str,
        input_text: str,
    ) -> dict[str, Any]:
        assert "calculator.py" in input_text
        assert "All file edits must use apply_patch" in instructions
        return _tool_call_response(
            "response-1",
            "read_file",
            {"path": "calculator.py"},
            "call-read",
        )

    def create_tool_response(
        self,
        *,
        config: AgentConfig,
        previous_response_id: str,
        tool_outputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        del config, previous_response_id
        result = json.loads(tool_outputs[0]["output"])
        self.tool_results.append(result)

        if self.step == 0:
            assert result["ok"] is True
            assert "return left - right" in result["output"]
            response = _tool_call_response(
                "response-2",
                "apply_patch",
                {
                    "patch": "\n".join(
                        [
                            "--- a/calculator.py",
                            "+++ b/calculator.py",
                            "@@ -1,2 +1,2 @@",
                            " def add(left: int, right: int) -> int:",
                            "-    return left - right",
                            "+    return left + right",
                            "",
                        ]
                    )
                },
                "call-patch",
            )
        elif self.step == 1:
            assert result["ok"] is True
            assert "modify calculator.py" in result["output"]
            response = _tool_call_response(
                "response-3",
                "git_diff",
                {},
                "call-diff",
            )
        elif self.step == 2:
            assert result["ok"] is True
            assert "return left - right" in result["output"]
            assert "return left + right" in result["output"]
            response = _tool_call_response(
                "response-4",
                "run_command",
                {"command": f'"{sys.executable}" -m pytest -q'},
                "call-test",
            )
        elif self.step == 3:
            assert result["ok"] is True
            assert "1 passed" in result["output"]
            response = {
                "id": "response-5",
                "output_text": "Fixed calculator.py and verified the test passes.",
                "output": [],
            }
        else:
            raise AssertionError(f"Unexpected tool-response step: {self.step}")

        self.step += 1
        return response


def test_agent_fixes_failing_test_and_reports_git_diff(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "failing_project"
    workspace = tmp_path / "project"
    shutil.copytree(fixture, workspace)
    (workspace / "test_calculator.py.txt").rename(workspace / "test_calculator.py")

    failing = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=workspace,
        text=True,
        capture_output=True,
        check=False,
    )
    assert failing.returncode != 0
    assert "1 failed" in failing.stdout

    _initialize_git_repository(workspace)
    config = AgentConfig(
        workspace=str(workspace),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=6,
        permission_mode="workspace-write",
        auto_approve_commands=True,
        auto_approve_edits=True,
        context_max_files=20,
        context_max_bytes_per_file=4000,
    )
    client = FixFailingTestClient()

    answer = run_agent("Fix the failing test and verify it.", config, model_client=client)

    assert answer == "Fixed calculator.py and verified the test passes."
    assert client.step == 4
    assert (workspace / "calculator.py").read_text(encoding="utf-8").endswith(
        "return left + right\n"
    )


def _tool_call_response(
    response_id: str,
    name: str,
    arguments: dict[str, Any],
    call_id: str,
) -> dict[str, Any]:
    return {
        "id": response_id,
        "output": [
            {
                "type": "function_call",
                "name": name,
                "arguments": json.dumps(arguments),
                "call_id": call_id,
            }
        ],
    }


def _initialize_git_repository(workspace: Path) -> None:
    git = shutil.which("git")
    assert git is not None, "git is required for the M1 integration test"
    subprocess.run([git, "init", "-q"], cwd=workspace, check=True)
    subprocess.run([git, "add", "."], cwd=workspace, check=True)
    subprocess.run(
        [
            git,
            "-c",
            "user.name=coding-agent-tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "Add failing fixture",
        ],
        cwd=workspace,
        check=True,
    )

import json
from pathlib import Path
from typing import Any

from coding_agent.agent import run_agent
from coding_agent.prompts import build_system_prompt
from coding_agent.types import AgentConfig


class FakeModelClient:
    def __init__(self) -> None:
        self.initial_calls: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []

    def create_initial_response(
        self,
        *,
        config: AgentConfig,
        instructions: str,
        input_text: str,
    ) -> dict[str, Any]:
        self.initial_calls.append(
            {
                "config": config,
                "instructions": instructions,
                "input_text": input_text,
            }
        )
        return {
            "id": "response-1",
            "output": [
                {
                    "type": "function_call",
                    "name": "search_text",
                    "arguments": '{"pattern":"hello","path":"."}',
                    "call_id": "call-1",
                }
            ],
        }

    def create_tool_response(
        self,
        *,
        config: AgentConfig,
        previous_response_id: str,
        tool_outputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.tool_calls.append(
            {
                "config": config,
                "previous_response_id": previous_response_id,
                "tool_outputs": tool_outputs,
            }
        )
        return {
            "id": "response-2",
            "output_text": "Read hello.txt successfully.",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Read hello.txt successfully.",
                        }
                    ],
                }
            ],
        }


def test_run_agent_accepts_mock_model_client(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    config = AgentConfig(
        workspace=str(tmp_path),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="read-only",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=10,
        context_max_bytes_per_file=1000,
    )
    client = FakeModelClient()

    answer = run_agent("search hello", config, model_client=client)

    assert answer == "Read hello.txt successfully."
    assert len(client.initial_calls) == 1
    assert "hello.txt" in client.initial_calls[0]["input_text"]
    assert len(client.tool_calls) == 1
    assert client.tool_calls[0]["previous_response_id"] == "response-1"
    assert client.tool_calls[0]["tool_outputs"][0]["call_id"] == "call-1"
    assert '"ok": true' in client.tool_calls[0]["tool_outputs"][0]["output"]
    assert "hello.txt:1:1" in client.tool_calls[0]["tool_outputs"][0]["output"]


class _SearchThenReadClient:
    def __init__(self) -> None:
        self.initial_instructions = ""
        self.initial_input = ""
        self.requested_tools: list[str] = []
        self.tool_round = 0

    def create_initial_response(
        self,
        *,
        config: AgentConfig,
        instructions: str,
        input_text: str,
    ) -> dict[str, Any]:
        del config
        self.initial_instructions = instructions
        self.initial_input = input_text
        self.requested_tools.append("search_text")
        return {
            "id": "search-response",
            "output": [
                {
                    "type": "function_call",
                    "name": "search_text",
                    "arguments": json.dumps({"pattern": "TARGET_SYMBOL"}),
                    "call_id": "search-call",
                }
            ],
        }

    def create_tool_response(
        self,
        *,
        config: AgentConfig,
        previous_response_id: str,
        tool_outputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        del config
        result = json.loads(tool_outputs[0]["output"])

        if self.tool_round == 0:
            assert previous_response_id == "search-response"
            assert result["ok"] is True
            assert "src/service.py:1:1" in result["output"]
            self.requested_tools.append("read_many_files")
            response = {
                "id": "read-response",
                "output": [
                    {
                        "type": "function_call",
                        "name": "read_many_files",
                        "arguments": json.dumps({"paths": ["src/service.py"]}),
                        "call_id": "read-call",
                    }
                ],
            }
        else:
            assert previous_response_id == "read-response"
            assert result["ok"] is True
            assert "TARGET_IMPLEMENTATION_BODY" in result["output"]
            response = {
                "id": "final-response",
                "output_text": "Located src/service.py after searching and reading it.",
                "output": [],
            }

        self.tool_round += 1
        return response


def test_agent_injects_root_instructions_then_searches_before_reading(
    tmp_path: Path,
) -> None:
    root_rule = "ROOT_AGENT_RULE: Search before reading implementation files."
    (tmp_path / "AGENTS.md").write_text(root_rule + "\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text(
        "TARGET_SYMBOL = 'TARGET_IMPLEMENTATION_BODY'\n",
        encoding="utf-8",
    )
    config = AgentConfig(
        workspace=str(tmp_path),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="read-only",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=6,
        context_max_bytes_per_file=8_000,
    )
    client = _SearchThenReadClient()

    answer = run_agent(
        "Locate the implementation of TARGET_SYMBOL.",
        config,
        model_client=client,
    )

    assert root_rule in client.initial_instructions
    assert "TARGET_IMPLEMENTATION_BODY" not in client.initial_input
    assert client.requested_tools == ["search_text", "read_many_files"]
    assert client.tool_round == 2
    assert answer == "Located src/service.py after searching and reading it."


def test_system_prompt_guides_search_then_read_project_understanding(
    tmp_path: Path,
) -> None:
    config = AgentConfig(
        workspace=str(tmp_path),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="workspace-write",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=6,
        context_max_bytes_per_file=8_000,
    )

    prompt = build_system_prompt(config)
    workflow_steps = [
        "Inspect repository instructions and the ranked inventory.",
        "Search for task terms, symbols, errors, and likely tests.",
        "Read only the relevant files, preferably in one read_many_files call.",
        "Before editing, ensure applicable nested AGENTS.md files were considered.",
        "Do not infer implementation details from file names alone.",
    ]

    assert "Project understanding workflow:" in prompt
    assert [prompt.index(step) for step in workflow_steps] == sorted(
        prompt.index(step) for step in workflow_steps
    )
    assert (
        "ranked inventory -> search_text -> read_many_files -> apply_patch -> git_diff"
        in prompt
    )
    assert "guidance rather than a hardcoded requirement" in prompt
    assert "gather file-content evidence before editing" in prompt


def test_system_prompt_defines_evidence_driven_verification_workflow(
    tmp_path: Path,
) -> None:
    config = AgentConfig(
        workspace=str(tmp_path),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="workspace-write",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=6,
        context_max_bytes_per_file=8_000,
    )

    prompt = build_system_prompt(config)
    verification_steps = [
        "Call discover_verification_commands before running project checks",
        "Select the most task-relevant available command",
        "Before editing, search for and read the code, tests, and diagnostics",
        "After every successful apply_patch, run at least one relevant",
        "If verification fails, extract the reported path, symbol, line number",
        "Rerun the same failed command after the repair",
        "report the commands run and their final statuses",
        "Stop applying patches when the repair limit is reached",
    ]

    assert "Verification workflow:" in prompt
    assert [prompt.index(step) for step in verification_steps] == sorted(
        prompt.index(step) for step in verification_steps
    )
    assert "never guess a command or arbitrary argv" in prompt
    assert "any skipped checks and the reason they were skipped" in prompt
    assert "run a broader relevant check when one is available" in prompt


def test_system_prompt_forbids_command_policy_evasion(tmp_path: Path) -> None:
    config = AgentConfig(
        workspace=str(tmp_path),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="workspace-write",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=6,
        context_max_bytes_per_file=8_000,
        sandbox_mode="docker",
    )

    prompt = build_system_prompt(config)

    assert "Sandbox mode: docker" in prompt
    assert "dependency installation, network access" in prompt
    assert "secret/environment credential access" in prompt
    assert "Never use run_command to bypass" in prompt
    assert "Never rewrite, split, wrap, or otherwise disguise" in prompt
    assert "secure_command_result" in prompt

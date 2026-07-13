import json
from pathlib import Path
from typing import Any


from coding_agent.agent import run_agent
from coding_agent.types import AgentConfig

M2_MAX_DISCOVERY_TOOL_CALLS = 2
M2_MAX_INITIAL_SAMPLES = 6
M2_MAX_INITIAL_CONTEXT_BYTES = 64 * 1024


class _M2DiscoveryClient:
    def __init__(self) -> None:
        self.tool_rounds = 0
        self.initial_input_bytes = 0
        self.initial_content_bytes = 0
        self.initial_sample_count = 0

    def create_initial_response(
        self,
        *,
        config: AgentConfig,
        instructions: str,
        input_text: str,
    ) -> dict[str, Any]:
        del config
        self.initial_input_bytes = len(input_text.encode("utf-8"))
        contents = input_text.split("Initial file contents:\n", 1)[1]
        contents = contents.split("\n\nMost source code", 1)[0]
        self.initial_content_bytes = len(contents.encode("utf-8"))
        self.initial_sample_count = sum(
            line.startswith("### ") for line in contents.splitlines()
        )
        assert "M2_ROOT_RULE: Search before reading source." in instructions
        assert "Project understanding workflow:" in instructions
        assert (
            "ranked inventory -> search_text -> read_many_files"
            in instructions
        )
        assert "M2_REFUND_IMPLEMENTATION_MARKER" not in input_text
        assert "src/payments" in input_text
        assert "src/generated/" not in input_text
        assert "debug.log" not in input_text
        return _tool_call_response(
            "response-1",
            "search_text",
            {
                "pattern": "calculate_refund",
                "path": ".",
                "max_results": 20,
            },
            "call-search",
        )

    def create_tool_response(
        self,
        *,
        config: AgentConfig,
        previous_response_id: str,
        tool_outputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        del config, previous_response_id
        assert len(tool_outputs) == 1
        result = json.loads(tool_outputs[0]["output"])

        if self.tool_rounds == 0:
            assert result["ok"] is True
            assert "src/payments/refund_service.py" in result["output"]
            assert "src/generated/" not in result["output"]
            assert "debug.log" not in result["output"]
            response = _tool_call_response(
                "response-2",
                "read_many_files",
                {
                    "paths": [
                        "src/payments/refund_service.py",
                        "tests/payments/test_refund_service.py",
                        "src/payments/AGENTS.md",
                    ],
                    "max_files": 4,
                    "max_bytes_per_file": 8_000,
                    "max_total_bytes": 16_000,
                },
                "call-read-many",
            )
        elif self.tool_rounds == 1:
            assert result["ok"] is True
            assert "M2_REFUND_IMPLEMENTATION_MARKER" in result["output"]
            assert "test_calculate_refund" in result["output"]
            assert "===== src/payments/AGENTS.md =====" in result["output"]
            assert "src/payments/AGENTS.md" in result["output"]
            response = {
                "id": "response-3",
                "output_text": (
                    "The refund implementation is in "
                    "src/payments/refund_service.py and its tests are in "
                    "tests/payments/test_refund_service.py."
                ),
                "output": [],
            }
        else:
            raise AssertionError("M2 discovery exceeded the allowed tool rounds.")

        self.tool_rounds += 1
        return response


def test_m2_medium_repository_is_located_with_search_then_read(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "medium-project"
    workspace.mkdir()
    (workspace / ".gitignore").write_text("src/generated/\n*.log\n", encoding="utf-8")
    (workspace / "AGENTS.md").write_text(
        "# Repository rules\n\nM2_ROOT_RULE: Search before reading source.\n",
        encoding="utf-8",
    )
    (workspace / "README.md").write_text(
        "# Medium project\n\nPayments live under src/payments.\n",
        encoding="utf-8",
    )

    generated = workspace / "src" / "generated"
    generated.mkdir(parents=True)
    for index in range(120):
        (generated / f"module_{index:03d}.py").write_text(
            f"def calculate_refund_noise_{index}():\n    return {index}\n",
            encoding="utf-8",
        )

    payments = workspace / "src" / "payments"
    payments.mkdir()
    (payments / "AGENTS.md").write_text(
        "# Payments rules\n\nRead the matching payments test with each implementation.\n",
        encoding="utf-8",
    )
    (payments / "refund_service.py").write_text(
        "M2_REFUND_IMPLEMENTATION_MARKER = True\n\n"
        "def calculate_refund(amount: int) -> int:\n"
        "    return amount\n",
        encoding="utf-8",
    )

    payment_tests = workspace / "tests" / "payments"
    payment_tests.mkdir(parents=True)
    (payment_tests / "test_refund_service.py").write_text(
        "from src.payments.refund_service import calculate_refund\n\n"
        "def test_calculate_refund() -> None:\n"
        "    assert calculate_refund(10) == 10\n",
        encoding="utf-8",
    )
    (workspace / "debug.log").write_text(
        "calculate_refund should not be searchable\n",
        encoding="utf-8",
    )
    (payments / "refund_service_legacy.py").write_text(
        "def unrelated_legacy_helper() -> None:\n    pass\n",
        encoding="utf-8",
    )
    (payment_tests / "test_refund_service_legacy.py").write_text(
        "def test_unrelated_legacy_helper() -> None:\n    pass\n",
        encoding="utf-8",
    )

    config = AgentConfig(
        workspace=str(workspace),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="read-only",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=6,
        context_max_bytes_per_file=8_000,
    )
    client = _M2DiscoveryClient()

    answer = run_agent(
        "Locate the calculate_refund implementation and its tests.",
        config,
        model_client=client,
    )

    assert client.tool_rounds == M2_MAX_DISCOVERY_TOOL_CALLS
    assert client.initial_sample_count <= M2_MAX_INITIAL_SAMPLES
    assert client.initial_content_bytes <= M2_MAX_INITIAL_CONTEXT_BYTES
    assert client.initial_input_bytes <= M2_MAX_INITIAL_CONTEXT_BYTES
    assert "src/payments/refund_service.py" in answer
    assert "tests/payments/test_refund_service.py" in answer


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

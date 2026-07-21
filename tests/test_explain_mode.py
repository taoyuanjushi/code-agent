from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

import coding_agent.tools as tools_module
from coding_agent.agent import run_agent_with_report
from coding_agent.sessions.store import SessionStore
from coding_agent.types import AgentConfig, ToolResult


def _config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace.resolve()),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="read-only",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=8,
        context_max_bytes_per_file=8_000,
        task_mode="explain",
    )


class _ForbiddenToolsClient:
    def __init__(self) -> None:
        self.tool_outputs: list[dict[str, Any]] | None = None

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        return {
            "id": "response-forbidden",
            "output": [
                {
                    "type": "function_call",
                    "call_id": f"call-{index}",
                    "name": name,
                    "arguments": "{}",
                }
                for index, name in enumerate(
                    ("apply_patch", "run_command", "run_verification"),
                    start=1,
                )
            ],
        }

    def create_tool_response(
        self,
        *,
        tool_outputs: list[dict[str, Any]],
        **_kwargs: object,
    ) -> dict[str, object]:
        self.tool_outputs = tool_outputs
        return {
            "id": "response-final",
            "output": [],
            "output_text": (
                "Repository evidence is insufficient to answer safely."
            ),
        }


def test_explain_model_write_and_command_requests_have_zero_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    implementation_calls = {
        "apply_patch": 0,
        "run_command": 0,
        "run_verification": 0,
    }

    def implementation(name: str):
        def invoke(*_args: object, **_kwargs: object) -> ToolResult:
            implementation_calls[name] += 1
            return ToolResult(ok=True, output="unexpected")

        return invoke

    monkeypatch.setattr(
        tools_module,
        "_apply_patch_tool",
        implementation("apply_patch"),
    )
    monkeypatch.setattr(
        tools_module,
        "_run_command_tool",
        implementation("run_command"),
    )
    monkeypatch.setattr(
        tools_module,
        "_run_verification_tool",
        implementation("run_verification"),
    )
    approval_calls = 0

    def approval_handler(_request: object) -> object:
        nonlocal approval_calls
        approval_calls += 1
        raise AssertionError("explain mode must not request approval")

    client = _ForbiddenToolsClient()
    report = run_agent_with_report(
        "explain without side effects",
        _config(tmp_path),
        model_client=client,
        session_store=SessionStore(tmp_path),
        approval_handler=cast(Any, approval_handler),
        stream=False,
    )

    assert report.answer.startswith("Repository evidence is insufficient")
    assert implementation_calls == {
        "apply_patch": 0,
        "run_command": 0,
        "run_verification": 0,
    }
    assert approval_calls == 0
    assert client.tool_outputs is not None
    assert all(
        "not allowed in explain task mode" in output["output"]
        for output in client.tool_outputs
    )
    assert not (tmp_path / "unexpected").exists()

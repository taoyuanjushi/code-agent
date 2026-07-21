from __future__ import annotations

import json
from pathlib import Path

import pytest

import coding_agent.tools as tools_module
from coding_agent.task_modes import (
    READ_ONLY_TASK_TOOL_NAMES,
    TASK_MODE_PROFILES,
    filter_tool_definitions,
)
from coding_agent.tools import TOOL_DEFINITIONS, execute_tool
from coding_agent.types import AgentConfig


def _config(workspace: Path, task_mode: str) -> AgentConfig:
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
        task_mode=task_mode,  # type: ignore[arg-type]
    )


def _tool_names(definitions: list[dict[str, object]]) -> set[str]:
    return {str(definition["name"]) for definition in definitions}


@pytest.mark.parametrize(
    ("task_mode", "extra_tools"),
    [("review", {"submit_review"}), ("explain", set())],
)
def test_restricted_product_modes_expose_only_read_only_tools(
    task_mode: str,
    extra_tools: set[str],
) -> None:
    names = _tool_names(filter_tool_definitions(task_mode, TOOL_DEFINITIONS))

    assert names == READ_ONLY_TASK_TOOL_NAMES | extra_tools
    assert names.isdisjoint(
        {
            "apply_patch",
            "discover_verification_commands",
            "run_verification",
            "run_command",
        }
    )
    assert TASK_MODE_PROFILES[task_mode].workspace_write_allowed is False  # type: ignore[index]
    assert TASK_MODE_PROFILES[task_mode].general_processes_allowed is False  # type: ignore[index]


@pytest.mark.parametrize("task_mode", ["review", "explain"])
@pytest.mark.parametrize(
    "tool_name",
    ["apply_patch", "run_command", "run_verification"],
)
def test_restricted_product_modes_reject_before_approval_or_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    task_mode: str,
    tool_name: str,
) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("restricted dispatch reached a side-effect boundary")

    monkeypatch.setattr(tools_module, "build_default_approval_handler", unexpected)
    monkeypatch.setattr(tools_module, "_apply_patch_tool", unexpected)
    monkeypatch.setattr(tools_module, "_run_command_tool", unexpected)
    monkeypatch.setattr(tools_module, "_run_verification_tool", unexpected)

    result = execute_tool(
        _config(tmp_path, task_mode),
        tool_name,
        json.dumps({"untrusted": "argument"}),
    )

    assert result.ok is False
    assert result.data == {
        "type": "task_mode_policy_rejection",
        "task_mode": task_mode,
        "tool_name": tool_name,
        "status": "denied",
        "disposition": "deny",
        "requires_approval": False,
    }
    assert list(tmp_path.iterdir()) == []

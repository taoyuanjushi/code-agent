from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, cast

TaskMode = Literal["run", "review", "explain"]
TASK_MODES = frozenset({"run", "review", "explain"})

RUN_TOOL_NAMES = frozenset(
    {
        "read_file",
        "read_many_files",
        "apply_patch",
        "list_files",
        "search_text",
        "discover_verification_commands",
        "run_verification",
        "git_status",
        "git_diff",
        "update_plan",
        "run_command",
    }
)
READ_ONLY_TASK_TOOL_NAMES = frozenset(
    {
        "read_file",
        "read_many_files",
        "list_files",
        "search_text",
        "git_status",
        "git_diff",
        "update_plan",
    }
)


@dataclass(frozen=True)
class TaskModeProfile:
    mode: TaskMode
    allowed_tools: frozenset[str]
    workspace_write_allowed: bool
    general_processes_allowed: bool


TASK_MODE_PROFILES: Mapping[TaskMode, TaskModeProfile] = MappingProxyType(
    {
        "run": TaskModeProfile(
            mode="run",
            allowed_tools=RUN_TOOL_NAMES,
            workspace_write_allowed=True,
            general_processes_allowed=True,
        ),
        "review": TaskModeProfile(
            mode="review",
            allowed_tools=READ_ONLY_TASK_TOOL_NAMES | {"submit_review"},
            workspace_write_allowed=False,
            general_processes_allowed=False,
        ),
        "explain": TaskModeProfile(
            mode="explain",
            allowed_tools=READ_ONLY_TASK_TOOL_NAMES,
            workspace_write_allowed=False,
            general_processes_allowed=False,
        ),
    }
)

_TASK_MODE_PROMPT_FRAGMENTS: Mapping[TaskMode, str] = MappingProxyType(
    {
        "run": (
            "Task mode: run. Use the complete coding workflow. Workspace writes "
            "remain governed by the configured permission mode, and process "
            "execution remains governed by command policy and sandbox rules."
        ),
        "review": (
            "Task mode: review. Perform a read-only code review of the current Git "
            "diff unless user limits scope. Do not edit or run processes. Verify "
            "each finding path and line, then call submit_review exactly once. Use "
            "findings=[] and a non-empty summary if no issue exists. Without Git, "
            "review specified files and name that scope in summary. Findings come "
            "only from submit_review, never free-form text."
        ),
        "explain": (
            "Task mode: explain. Search and read evidence first. Cite file-backed "
            "claims as `path:line` only from workspace files returned by read_file "
            "or read_many_files. Explain files, modules, Git diff, or durable "
            "session facts. If evidence is insufficient, say so instead of "
            "guessing. Return only the user-visible explanation, not a reasoning "
            "summary. Do not edit, run processes, or request approvals."
        ),
    }
)


def get_task_mode_profile(task_mode: str) -> TaskModeProfile:
    if not isinstance(task_mode, str) or task_mode not in TASK_MODES:
        raise ValueError(f"Unsupported task mode: {task_mode!r}.")
    return TASK_MODE_PROFILES[cast(TaskMode, task_mode)]


def is_tool_allowed(task_mode: str, tool_name: str) -> bool:
    if not isinstance(tool_name, str) or not tool_name:
        return False
    return tool_name in get_task_mode_profile(task_mode).allowed_tools


def filter_tool_definitions(
    task_mode: str,
    definitions: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    allowed_tools = get_task_mode_profile(task_mode).allowed_tools
    filtered: list[dict[str, Any]] = []
    for definition in definitions:
        name = definition.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Tool definitions must contain a non-empty string name.")
        if name in allowed_tools:
            filtered.append(definition)
    return filtered


def tool_definitions_for_mode(
    task_mode: str,
    definitions: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    return filter_tool_definitions(task_mode, definitions)


def task_mode_prompt_fragment(task_mode: str) -> str:
    return _TASK_MODE_PROMPT_FRAGMENTS[get_task_mode_profile(task_mode).mode]


def validate_task_mode_configuration(
    task_mode: str,
    *,
    permission_mode: str,
    auto_approve_edits: bool,
    auto_approve_commands: bool,
    full_auto: bool,
) -> None:
    profile = get_task_mode_profile(task_mode)
    if profile.mode == "run":
        return

    conflicts: list[str] = []
    if permission_mode != "read-only":
        conflicts.append("workspace-write permission")
    if auto_approve_edits:
        conflicts.append("automatic edit approval")
    if auto_approve_commands:
        conflicts.append("automatic command approval")
    if full_auto:
        conflicts.append("full-auto")
    if conflicts:
        raise ValueError(
            f"Task mode {profile.mode!r} forbids " + ", ".join(conflicts) + "."
        )

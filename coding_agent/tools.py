import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .ignore import load_ignore_policy
from .instructions import (
    discover_agent_instructions,
    format_agent_instructions,
    instructions_for_path,
)
from .patch import apply_patch_plan, plan_patch, summarize_patch_plan
from .path_safety import resolve_inside_workspace
from .reader import (
    DEFAULT_MAX_BYTES_PER_FILE,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_TOTAL_BYTES,
    format_file_read_results,
    read_many_files,
)
from .search import format_search_matches, search_text
from .types import AgentConfig, ToolResult

DEFAULT_READ_FILE_MAX_BYTES = DEFAULT_MAX_BYTES_PER_FILE
DEFAULT_SEARCH_MAX_RESULTS = 100
DEFAULT_COMMAND_TIMEOUT_MS = 30_000

MAX_READ_FILES = 100
MAX_READ_BYTES_PER_FILE = 1_048_576
MAX_READ_TOTAL_BYTES = 4_194_304
MAX_SEARCH_RESULTS = 1_000
MAX_COMMAND_TIMEOUT_MS = 300_000
MAX_GLOB_PATTERNS = 100


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a UTF-8 text file from the workspace.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Workspace-relative file path.",
                },
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_READ_BYTES_PER_FILE,
                    "description": "Maximum bytes to read. Defaults to 30000.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "read_many_files",
        "description": "Read multiple UTF-8 workspace files in request order with file-count, per-file, and total-byte limits.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "paths": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_READ_FILES,
                    "items": {"type": "string", "minLength": 1},
                    "description": "Workspace-relative file paths in the order they should be read.",
                },
                "max_files": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_READ_FILES,
                    "description": "Maximum requested files to process. Defaults to 20.",
                },
                "max_bytes_per_file": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_READ_BYTES_PER_FILE,
                    "description": "Maximum bytes returned for one file. Defaults to 30000.",
                },
                "max_total_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_READ_TOTAL_BYTES,
                    "description": "Maximum bytes returned across successful files. Defaults to 120000.",
                },
            },
            "required": ["paths"],
        },
    },
    {
        "type": "function",
        "name": "apply_patch",
        "description": "Apply a unified diff patch inside the workspace. Requires workspace-write mode and validates file paths plus hunk context before writing.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "patch": {"type": "string", "description": "Unified diff patch, using ---/+++ file headers and @@ hunks."}
            },
            "required": ["patch"],
        },
    },
    {
        "type": "function",
        "name": "list_files",
        "description": "List direct children of a workspace directory.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative directory path. Defaults to '.'."}
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "search_text",
        "description": "Search workspace text files with ripgrep when available and a Python fallback.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Text or regular expression to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file or directory path. Defaults to '.'.",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Whether matching should be case-sensitive. Defaults to false.",
                },
                "regex": {
                    "type": "boolean",
                    "description": "Interpret pattern as a regular expression. Defaults to false.",
                },
                "glob": {
                    "type": "array",
                    "maxItems": MAX_GLOB_PATTERNS,
                    "items": {"type": "string", "minLength": 1},
                    "description": "Optional ordered include/exclude globs, for example ['*.py', '!test_*'].",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_SEARCH_RESULTS,
                    "description": "Maximum matches to return. Defaults to 100.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "type": "function",
        "name": "git_status",
        "description": "Run git status --short in the workspace.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "git_diff",
        "description": "Run git diff --stat and git diff in the workspace.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "run_command",
        "description": "Run a shell command in the workspace and return stdout/stderr.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "command": {"type": "string", "description": "Command to run."},
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_COMMAND_TIMEOUT_MS,
                    "description": "Timeout in milliseconds. Defaults to 30000.",
                },
            },
            "required": ["command"],
        },
    },
]


def execute_tool(config: AgentConfig, name: str, raw_arguments: str) -> ToolResult:
    try:
        args = _parse_tool_arguments(raw_arguments)

        if name == "read_file":
            return _read_file_tool(config, args)
        if name == "read_many_files":
            return _read_many_files_tool(config, args)
        if name == "write_file":
            return ToolResult(
                ok=False,
                output="write_file is disabled. Submit a unified diff through apply_patch so every edit is reviewable.",
            )
        if name == "apply_patch":
            return _apply_patch_tool(config, args)
        if name == "list_files":
            return _list_files_tool(config, args)
        if name == "search_text":
            return _search_text_tool(config, args)
        if name == "git_status":
            return _run_shell_command("git status --short", config.workspace, 30_000)
        if name == "git_diff":
            return _git_diff_tool(config)
        if name == "run_command":
            return _run_command_tool(config, args)

        return ToolResult(ok=False, output=f"Unknown tool: {name}")
    except Exception as exc:
        return ToolResult(ok=False, output=str(exc))


def _parse_tool_arguments(raw_arguments: str) -> dict[str, Any]:
    if not raw_arguments.strip():
        return {}

    parsed = json.loads(raw_arguments)
    if not isinstance(parsed, dict):
        raise ValueError("Tool arguments must be a JSON object.")

    return parsed


def _read_file_tool(config: AgentConfig, args: dict[str, Any]) -> ToolResult:
    relative_path = _require_string(args.get("path"), "path")
    max_bytes = _require_positive_int(
        args,
        "max_bytes",
        default=DEFAULT_READ_FILE_MAX_BYTES,
        maximum=MAX_READ_BYTES_PER_FILE,
    )

    root = Path(config.workspace).resolve()
    full_path = resolve_inside_workspace(root, relative_path)
    normalized_path = full_path.relative_to(root).as_posix()
    ignore_policy = load_ignore_policy(config.workspace)
    is_ignored = ignore_policy.is_ignored(full_path)
    is_visible_instruction = (
        full_path.name == "AGENTS.md"
        and not ignore_policy.is_ignored(full_path.parent)
    )
    if is_ignored and not is_visible_instruction:
        return ToolResult(ok=False, output=f"Path is ignored: {normalized_path}")
    if ignore_policy.is_binary(full_path):
        return ToolResult(ok=False, output=f"Binary file cannot be read as text: {normalized_path}")

    data = full_path.read_bytes()
    content = data[:max_bytes].decode("utf-8", errors="replace")
    suffix = f"\n\n[Truncated after {max_bytes} bytes]" if len(data) > max_bytes else ""
    instructions = [
        instruction
        for instruction in instructions_for_path(
            discover_agent_instructions(config.workspace),
            normalized_path,
        )
        if instruction.path != normalized_path
    ]
    instruction_text = format_agent_instructions(instructions)
    return ToolResult(
        ok=True,
        output=(
            f"[Applicable repository instructions for {normalized_path}]\n"
            f"{instruction_text}\n\n"
            f"[File contents: {normalized_path}]\n"
            f"{content}{suffix}"
        ),
    )


def _read_many_files_tool(config: AgentConfig, args: dict[str, Any]) -> ToolResult:
    paths = _require_string_list(
        args,
        "paths",
        required=True,
        allow_empty=False,
        maximum_items=MAX_READ_FILES,
    )
    assert paths is not None

    results = read_many_files(
        config.workspace,
        paths,
        max_files=_require_positive_int(
            args,
            "max_files",
            default=DEFAULT_MAX_FILES,
            maximum=MAX_READ_FILES,
        ),
        max_bytes_per_file=_require_positive_int(
            args,
            "max_bytes_per_file",
            default=DEFAULT_MAX_BYTES_PER_FILE,
            maximum=MAX_READ_BYTES_PER_FILE,
        ),
        max_total_bytes=_require_positive_int(
            args,
            "max_total_bytes",
            default=DEFAULT_MAX_TOTAL_BYTES,
            maximum=MAX_READ_TOTAL_BYTES,
        ),
    )
    return ToolResult(
        ok=True,
        output=format_file_read_results(config.workspace, results),
    )


def _apply_patch_tool(config: AgentConfig, args: dict[str, Any]) -> ToolResult:
    if config.permission_mode != "workspace-write":
        return ToolResult(
            ok=False,
            output="apply_patch is disabled in read-only mode. Re-run with --write to allow workspace edits.",
        )

    patch = _require_string(args.get("patch"), "patch")
    patch_plan = plan_patch(config.workspace, patch)
    summary = summarize_patch_plan(patch_plan)

    print(
        f"Apply patch in {config.workspace}?\n\n"
        f"Change summary:\n{summary}\n\n"
        f"Unified diff:\n{patch}"
    )

    if not config.auto_approve_edits:
        approved = input("Apply patch? [y/N] ").strip().lower() in {"y", "yes"}
        if not approved:
            return ToolResult(ok=False, output="User declined patch application.")

    apply_patch_plan(patch_plan)
    return ToolResult(ok=True, output=f"Applied patch:\n{summary}")


def _list_files_tool(config: AgentConfig, args: dict[str, Any]) -> ToolResult:
    relative_path = _optional_string_argument(args, "path", default=".")
    root = Path(config.workspace).resolve()
    full_path = resolve_inside_workspace(root, relative_path)
    ignore_policy = load_ignore_policy(config.workspace)
    if ignore_policy.is_ignored(full_path):
        return ToolResult(ok=False, output=f"Path is ignored: {relative_path}")

    entries = sorted(
        f"{'dir ' if entry.is_dir() else 'file'} {entry.name}"
        for entry in full_path.iterdir()
        if not ignore_policy.is_ignored(entry)
    )
    return ToolResult(ok=True, output="\n".join(entries) or "(empty directory)")


def _search_text_tool(config: AgentConfig, args: dict[str, Any]) -> ToolResult:
    pattern = _require_string(args.get("pattern"), "pattern")
    relative_path = _optional_string_argument(args, "path", default=".")
    matches = search_text(
        workspace=config.workspace,
        pattern=pattern,
        path=relative_path,
        case_sensitive=_require_bool(args, "case_sensitive", default=False),
        regex=_require_bool(args, "regex", default=False),
        glob=_require_string_list(
            args,
            "glob",
            maximum_items=MAX_GLOB_PATTERNS,
        ),
        max_results=_require_positive_int(
            args,
            "max_results",
            default=DEFAULT_SEARCH_MAX_RESULTS,
            maximum=MAX_SEARCH_RESULTS,
        ),
    )

    return ToolResult(ok=True, output=format_search_matches(matches))


def _run_command_tool(config: AgentConfig, args: dict[str, Any]) -> ToolResult:
    command = _require_string(args.get("command"), "command")
    timeout_ms = _require_positive_int(
        args,
        "timeout_ms",
        default=DEFAULT_COMMAND_TIMEOUT_MS,
        maximum=MAX_COMMAND_TIMEOUT_MS,
    )

    if config.permission_mode != "workspace-write" and _looks_mutating(command):
        return ToolResult(
            ok=False,
            output="Command appears to modify files and read-only mode is active. Re-run with --write if this is intended.",
        )

    if not config.auto_approve_commands:
        print(f"Run command in {config.workspace}?\n{command}")
        approved = input("Run command? [y/N] ").strip().lower() in {"y", "yes"}
        if not approved:
            return ToolResult(ok=False, output="User declined command execution.")

    return _run_shell_command(command, config.workspace, timeout_ms)


def _git_diff_tool(config: AgentConfig) -> ToolResult:
    stat = _run_shell_command("git diff --stat", config.workspace, 30_000)
    diff = _run_shell_command("git diff", config.workspace, 30_000)
    return ToolResult(
        ok=stat.ok and diff.ok,
        output="\n".join(["[git diff --stat]", stat.output, "", "[git diff]", diff.output]),
    )


def _looks_mutating(command: str) -> bool:
    mutating_patterns = [
        r"\bnpm\s+(install|i|update|uninstall|remove|audit\s+fix)\b",
        r"\bpnpm\s+(add|install|update|remove)\b",
        r"\byarn\s+(add|install|upgrade|remove)\b",
        r"\brm\s+-",
        r"\bdel\b",
        r"\bRemove-Item\b",
        r"\bgit\s+(checkout|reset|clean|apply|am|merge|rebase|commit)\b",
        r">",
    ]
    return any(re.search(pattern, command, flags=re.IGNORECASE) for pattern in mutating_patterns)


def _run_shell_command(command: str, cwd: str, timeout_ms: int) -> ToolResult:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout_ms / 1000,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        return ToolResult(ok=False, output=f"Command timed out after {timeout_ms}ms.\n{stdout}\n{stderr}".strip())

    return ToolResult(
        ok=completed.returncode == 0,
        output="\n".join(
            part
            for part in [
                f"exit code: {completed.returncode}",
                completed.stdout.strip(),
                completed.stderr.strip(),
            ]
            if part
        ),
    )


def _require_positive_int(
    args: dict[str, Any],
    name: str,
    *,
    default: int,
    maximum: int,
) -> int:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    if value > maximum:
        raise ValueError(f"{name} must be at most {maximum}.")
    return value


def _require_bool(
    args: dict[str, Any],
    name: str,
    *,
    default: bool,
) -> bool:
    value = args.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean.")
    return value


def _require_string_list(
    args: dict[str, Any],
    name: str,
    *,
    required: bool = False,
    allow_empty: bool = True,
    maximum_items: int | None = None,
) -> list[str] | None:
    if name not in args:
        if required:
            raise ValueError(f"{name} must be a non-empty list of non-empty strings.")
        return None

    value = args[name]
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        qualifier = "non-empty " if not allow_empty else ""
        raise ValueError(
            f"{name} must be a {qualifier}list of non-empty strings."
        )
    if not allow_empty and not value:
        raise ValueError(f"{name} must be a non-empty list of non-empty strings.")
    if maximum_items is not None and len(value) > maximum_items:
        raise ValueError(f"{name} must contain at most {maximum_items} items.")
    return value


def _optional_string_argument(
    args: dict[str, Any],
    name: str,
    *,
    default: str,
) -> str:
    if name not in args:
        return default
    return _require_string(args[name], name)


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing or invalid non-empty string argument: {label}")
    return value


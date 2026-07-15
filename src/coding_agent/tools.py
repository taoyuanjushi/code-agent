import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .approvals import (
    ApprovalHandler,
    ApprovalRequest,
    build_default_approval_handler,
    validate_approval_decision,
)
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
from .tool_policy import hash_tool_arguments
from .types import MAX_FIX_ATTEMPTS, AgentConfig, ToolResult
from .verification import (
    DEFAULT_VERIFICATION_MAX_OUTPUT_BYTES,
    DEFAULT_VERIFICATION_MAX_OUTPUT_LINES,
    DEFAULT_VERIFICATION_TIMEOUT_MS,
    MAX_VERIFICATION_OUTPUT_BYTES,
    MAX_VERIFICATION_OUTPUT_LINES,
    MAX_VERIFICATION_TIMEOUT_MS,
    VerificationCommand,
    VerificationDiscoveryResult,
    VerificationResult,
    discover_verification_commands,
    run_verification_command,
)

DEFAULT_READ_FILE_MAX_BYTES = DEFAULT_MAX_BYTES_PER_FILE
DEFAULT_SEARCH_MAX_RESULTS = 100
DEFAULT_COMMAND_TIMEOUT_MS = 30_000

MAX_READ_FILES = 100
MAX_READ_BYTES_PER_FILE = 1_048_576
MAX_READ_TOTAL_BYTES = 4_194_304
MAX_SEARCH_RESULTS = 1_000
MAX_COMMAND_TIMEOUT_MS = 300_000
MAX_GLOB_PATTERNS = 100


@dataclass
class VerificationToolState:
    task: str
    max_fix_attempts: int
    discovery: VerificationDiscoveryResult | None = None
    verification_history: list[VerificationResult] = field(default_factory=list)
    unresolved_failure_command_id: str | None = None
    repair_attempts: int = 0
    after_edit: bool = False
    edit_generation: int = 0
    passed_generations: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.task, str):
            raise TypeError("task must be a string.")
        if (
            isinstance(self.max_fix_attempts, bool)
            or not isinstance(self.max_fix_attempts, int)
            or self.max_fix_attempts < 0
            or self.max_fix_attempts > MAX_FIX_ATTEMPTS
        ):
            raise ValueError(
                f"max_fix_attempts must be between 0 and {MAX_FIX_ATTEMPTS}."
            )

    @property
    def repair_limit_reached(self) -> bool:
        return (
            self.unresolved_failure_command_id is not None
            and self.repair_attempts >= self.max_fix_attempts
        )

    def record_patch_applied(self) -> None:
        if self.unresolved_failure_command_id is not None:
            self.repair_attempts += 1
        self.edit_generation += 1
        self.after_edit = True
        self.discovery = None

    def record_verification(self, result: VerificationResult) -> None:
        self.verification_history.append(result)
        self.after_edit = False

        if result.status == "failed":
            if self.unresolved_failure_command_id is None:
                self.repair_attempts = 0
            self.unresolved_failure_command_id = result.command_id
            return

        if result.status == "passed":
            self.passed_generations[result.command_id] = self.edit_generation

        if self.unresolved_failure_command_id == result.command_id:
            self.unresolved_failure_command_id = None
            self.repair_attempts = 0


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
        "name": "discover_verification_commands",
        "description": "Discover trusted project test, lint, type-check, and build commands, ranked for the current task.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Optional task text used to rerank discovered commands.",
                }
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "run_verification",
        "description": "Run one trusted command selected by its discovered command ID. Arbitrary argv is not accepted.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "command_id": {"type": "string", "minLength": 1},
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_VERIFICATION_TIMEOUT_MS,
                },
                "max_output_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_VERIFICATION_OUTPUT_BYTES,
                },
                "max_output_lines": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_VERIFICATION_OUTPUT_LINES,
                },
            },
            "required": ["command_id"],
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


def execute_tool(
    config: AgentConfig,
    name: str,
    raw_arguments: str,
    *,
    state: VerificationToolState | None = None,
    approval_handler: ApprovalHandler | None = None,
    call_id: str | None = None,
) -> ToolResult:
    try:
        args = _parse_tool_arguments(raw_arguments)
        arguments_sha256 = hash_tool_arguments(raw_arguments)
        effective_call_id = call_id or f"direct-{name}-{arguments_sha256[:16]}"
        handler = approval_handler or build_default_approval_handler(config)

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
            return _apply_patch_tool(
                config,
                args,
                state=state,
                approval_handler=handler,
                call_id=effective_call_id,
                arguments_sha256=arguments_sha256,
            )
        if name == "list_files":
            return _list_files_tool(config, args)
        if name == "search_text":
            return _search_text_tool(config, args)
        if name == "discover_verification_commands":
            return _discover_verification_tool(config, args, state=state)
        if name == "run_verification":
            return _run_verification_tool(
                config,
                args,
                state=state,
                approval_handler=handler,
                call_id=effective_call_id,
                arguments_sha256=arguments_sha256,
            )
        if name == "git_status":
            return _run_shell_command("git status --short", config.workspace, 30_000)
        if name == "git_diff":
            return _git_diff_tool(config)
        if name == "run_command":
            return _run_command_tool(
                config,
                args,
                approval_handler=handler,
                call_id=effective_call_id,
                arguments_sha256=arguments_sha256,
            )

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


def _apply_patch_tool(
    config: AgentConfig,
    args: dict[str, Any],
    *,
    state: VerificationToolState | None = None,
    approval_handler: ApprovalHandler,
    call_id: str,
    arguments_sha256: str,
) -> ToolResult:
    if config.permission_mode != "workspace-write":
        return ToolResult(
            ok=False,
            output="apply_patch is disabled in read-only mode. Re-run with --write to allow workspace edits.",
        )

    if state is not None and state.repair_limit_reached:
        return ToolResult(
            ok=False,
            output=(
                "Repair attempt limit reached; no additional patch was applied "
                f"while {state.unresolved_failure_command_id} is failing."
            ),
            data={
                "type": "repair_limit_reached",
                "failed_command_id": state.unresolved_failure_command_id,
                "repair_attempts": state.repair_attempts,
                "max_fix_attempts": state.max_fix_attempts,
            },
        )

    patch = _require_string(args.get("patch"), "patch")
    patch_plan = plan_patch(config.workspace, patch)
    summary = summarize_patch_plan(patch_plan)
    before_hashes = {
        file.path: _hash_file_or_none(file.absolute_path)
        for file in patch_plan.files
    }
    expected_after_hashes = {
        file.path: (
            None
            if file.after_content is None
            else hashlib.sha256(file.after_content.encode("utf-8")).hexdigest()
        )
        for file in patch_plan.files
    }
    diff_sha256 = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    recovery_file_changes = [
        {
            "path": file.path,
            "change_type": file.change_type,
            "before_sha256": before_hashes[file.path],
            "after_sha256": expected_after_hashes[file.path],
        }
        for file in patch_plan.files
    ]
    request = ApprovalRequest(
        call_id=call_id,
        action="apply_patch",
        summary=summary,
        arguments_sha256=arguments_sha256,
        details={
            "workspace": str(Path(config.workspace).resolve()),
            "change_summary": summary,
            "patch": patch,
            "changed_paths": [file.path for file in patch_plan.files],
            "diff_sha256": diff_sha256,
            "file_changes": recovery_file_changes,
        },
    )
    if not _approval_granted(approval_handler, request):
        return ToolResult(ok=False, output="User declined patch application.")

    current_before_hashes = {
        file.path: _hash_file_or_none(file.absolute_path)
        for file in patch_plan.files
    }
    if current_before_hashes != before_hashes:
        raise RuntimeError(
            "Workspace changed after patch review; refusing to apply the approved diff."
        )

    apply_patch_plan(patch_plan)
    after_hashes = {
        file.path: _hash_file_or_none(file.absolute_path)
        for file in patch_plan.files
    }
    if after_hashes != expected_after_hashes:
        raise RuntimeError(
            "Applied patch content does not match the audited after hashes."
        )
    file_changes = [dict(item) for item in recovery_file_changes]
    data: dict[str, object] = {
        "type": "patch_applied",
        "changed_paths": [file.path for file in patch_plan.files],
        "diff_sha256": diff_sha256,
        "file_changes": file_changes,
        "touched_file_hashes": after_hashes,
    }
    if state is not None:
        state.record_patch_applied()
        data.update(
            {
                "edit_generation": state.edit_generation,
                "failed_command_id": state.unresolved_failure_command_id,
                "repair_attempts": state.repair_attempts,
                "max_fix_attempts": state.max_fix_attempts,
                "repair_limit_reached": state.repair_limit_reached,
            }
        )
    return ToolResult(
        ok=True,
        output=f"Applied patch:\n{summary}",
        data=data,
    )


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


def _discover_verification_tool(
    config: AgentConfig,
    args: dict[str, Any],
    *,
    state: VerificationToolState | None,
) -> ToolResult:
    _reject_unknown_arguments(args, {"task"})
    current_state = state or VerificationToolState(
        task="",
        max_fix_attempts=config.max_fix_attempts,
    )
    if "task" in args:
        current_state.task = _require_string(args["task"], "task")

    discovery = discover_verification_commands(
        config.workspace,
        task=current_state.task,
        failed_command_id=current_state.unresolved_failure_command_id,
        after_edit=current_state.after_edit,
    )
    current_state.discovery = discovery
    data = {
        "type": "verification_discovery",
        "workspace": discovery.workspace,
        "commands": [_verification_command_data(command) for command in discovery.commands],
        "warnings": list(discovery.warnings),
        "errors": list(discovery.errors),
    }
    lines = [
        f"{command.id} [{command.kind}] {'available' if command.available else 'unavailable'}"
        f" - {command.reason or 'unranked'}"
        for command in discovery.commands
    ]
    if discovery.warnings:
        lines.extend(f"warning: {warning}" for warning in discovery.warnings)
    if discovery.errors:
        lines.extend(f"error: {error}" for error in discovery.errors)
    return ToolResult(
        ok=not discovery.errors,
        output="\n".join(lines) or "No verification commands discovered.",
        data=data,
    )


def _run_verification_tool(
    config: AgentConfig,
    args: dict[str, Any],
    *,
    state: VerificationToolState | None,
    approval_handler: ApprovalHandler,
    call_id: str,
    arguments_sha256: str,
) -> ToolResult:
    _reject_unknown_arguments(
        args,
        {"command_id", "timeout_ms", "max_output_bytes", "max_output_lines"},
    )
    command_id = _require_string(args.get("command_id"), "command_id")
    current_state = state or VerificationToolState(
        task="",
        max_fix_attempts=config.max_fix_attempts,
    )

    if current_state.passed_generations.get(command_id) == current_state.edit_generation:
        data = {
            "type": "verification_skipped",
            "command_id": command_id,
            "reason": "already passed after the latest edit",
        }
        return ToolResult(ok=False, output=data["reason"], data=data)

    if current_state.discovery is None:
        current_state.discovery = discover_verification_commands(
            config.workspace,
            task=current_state.task,
            failed_command_id=current_state.unresolved_failure_command_id,
            after_edit=current_state.after_edit,
        )

    attempt = 1 + sum(
        result.command_id == command_id
        for result in current_state.verification_history
    )
    timeout_ms = _require_positive_int(
        args,
        "timeout_ms",
        default=DEFAULT_VERIFICATION_TIMEOUT_MS,
        maximum=MAX_VERIFICATION_TIMEOUT_MS,
    )

    def approve(command: VerificationCommand) -> bool:
        request = ApprovalRequest(
            call_id=call_id,
            action="run_verification",
            summary=f"Run verification command {command.id}",
            arguments_sha256=arguments_sha256,
            details={
                "command_id": command.id,
                "kind": command.kind,
                "argv": list(command.argv),
                "cwd": command.cwd,
                "timeout_ms": timeout_ms,
                "shell": False,
            },
        )
        return _approval_granted(approval_handler, request)

    result = run_verification_command(
        config.workspace,
        command_id=command_id,
        discovery=current_state.discovery,
        timeout_ms=timeout_ms,
        max_output_bytes=_require_positive_int(
            args,
            "max_output_bytes",
            default=DEFAULT_VERIFICATION_MAX_OUTPUT_BYTES,
            maximum=MAX_VERIFICATION_OUTPUT_BYTES,
        ),
        max_output_lines=_require_positive_int(
            args,
            "max_output_lines",
            default=DEFAULT_VERIFICATION_MAX_OUTPUT_LINES,
            maximum=MAX_VERIFICATION_OUTPUT_LINES,
        ),
        attempt=attempt,
        approval_callback=approve,
    )
    current_state.record_verification(result)
    data = _verification_result_data(
        result,
        state=current_state,
        timeout_ms=timeout_ms,
    )
    return ToolResult(
        ok=result.status == "passed",
        output=result.output or f"Verification {result.status}: {command_id}",
        data=data,
    )


def _verification_command_data(command: VerificationCommand) -> dict[str, object]:
    return {
        "id": command.id,
        "kind": command.kind,
        "argv": list(command.argv),
        "cwd": command.cwd,
        "source": command.source,
        "available": command.available,
        "unavailable_reason": command.unavailable_reason,
        "reason": command.reason,
    }


def _verification_result_data(
    result: VerificationResult,
    *,
    state: VerificationToolState,
    timeout_ms: int,
) -> dict[str, object]:
    return {
        "type": "verification_result",
        "command_id": result.command_id,
        "kind": result.kind,
        "status": result.status,
        "argv": list(result.argv),
        "cwd": result.cwd,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "timeout_ms": timeout_ms,
        "timed_out": result.status == "timed_out",
        "shell": False,
        "truncated": result.truncated,
        "omitted_lines": result.omitted_lines,
        "omitted_bytes": result.omitted_bytes,
        "attempt": result.attempt,
        "active_failure_command_id": state.unresolved_failure_command_id,
        "repair_attempts": state.repair_attempts,
        "max_fix_attempts": state.max_fix_attempts,
        "repair_limit_reached": state.repair_limit_reached,
    }


def _reject_unknown_arguments(args: dict[str, Any], allowed: set[str]) -> None:
    unexpected = sorted(set(args) - allowed)
    if unexpected:
        raise ValueError(f"Unexpected argument(s): {', '.join(unexpected)}")


def _run_command_tool(
    config: AgentConfig,
    args: dict[str, Any],
    *,
    approval_handler: ApprovalHandler,
    call_id: str,
    arguments_sha256: str,
) -> ToolResult:
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

    request = ApprovalRequest(
        call_id=call_id,
        action="run_command",
        summary=f"Run shell command: {command}",
        arguments_sha256=arguments_sha256,
        details={
            "command": command,
            "cwd": str(Path(config.workspace).resolve()),
            "timeout_ms": timeout_ms,
            "shell": True,
        },
    )
    if not _approval_granted(approval_handler, request):
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
    resolved_cwd = str(Path(cwd).resolve())
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=resolved_cwd,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout_ms / 1000,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        stdout = _coerce_process_output(exc.stdout)
        stderr = _coerce_process_output(exc.stderr)
        return ToolResult(
            ok=False,
            output=(
                f"Command timed out after {timeout_ms}ms.\n{stdout}\n{stderr}"
            ).strip(),
            data={
                "type": "command_result",
                "command": command,
                "cwd": resolved_cwd,
                "shell": True,
                "timeout_ms": timeout_ms,
                "exit_code": None,
                "timed_out": True,
                "duration_ms": duration_ms,
            },
        )

    duration_ms = max(0, round((time.monotonic() - started) * 1000))
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
        data={
            "type": "command_result",
            "command": command,
            "cwd": resolved_cwd,
            "shell": True,
            "timeout_ms": timeout_ms,
            "exit_code": completed.returncode,
            "timed_out": False,
            "duration_ms": duration_ms,
        },
    )


def _approval_granted(
    handler: ApprovalHandler,
    request: ApprovalRequest,
) -> bool:
    decision = handler(request)
    validate_approval_decision(request, decision)
    return decision.outcome == "approved"


def _hash_file_or_none(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _coerce_process_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


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


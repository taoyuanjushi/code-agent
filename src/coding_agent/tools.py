import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .explanations import (
    ExplanationReadEvidence,
    explanation_read_evidence_list_to_dict,
)
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
from .plans import (
    EMPTY_PLAN,
    PLAN_MAX_EXPLANATION_CHARS,
    PLAN_MAX_ITEMS,
    PLAN_MAX_STEP_CHARS,
    PLAN_MIN_ITEMS,
    PLAN_STATUSES,
    PlanState,
    parse_plan_update,
    plan_state_to_dict,
    validate_plan_transition,
)
from .patch import (
    FilePatchPlan,
    PatchPlan,
    apply_patch_plan,
    plan_patch,
    summarize_patch_plan,
)
from .reviews import (
    REVIEW_MAX_DETAIL_CHARS,
    REVIEW_MAX_FINDINGS,
    REVIEW_MAX_SUMMARY_CHARS,
    REVIEW_MAX_TITLE_CHARS,
    ReviewFinding,
    ReviewResult,
    parse_review_submission,
    review_result_to_dict,
)
from .path_safety import resolve_workspace_path
from .reader import (
    DEFAULT_MAX_BYTES_PER_FILE,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_TOTAL_BYTES,
    format_file_read_results,
    read_many_files,
)
from .search import format_search_matches, search_text
from .task_modes import is_tool_allowed
from .security.command_policy import (
    evaluate_command_policy,
    format_command_policy_block,
)
from .security.docker_backend import DockerSandboxBackend
from .security.models import (
    MAX_COMMAND_ARGUMENTS,
    CommandPolicyDecision,
    CommandSpec,
    ExecutionLimits,
    SandboxCapability,
    SandboxExecutionPlan,
)
from .security.path_policy import (
    SENSITIVE_PATH_DENIAL_REASON,
    load_sensitive_path_policy,
)
from .security.process_runner import HostProcessResult, run_host_process
from .security.sandbox import SandboxExecutionOutcome
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
    build_verification_command_spec,
    discover_verification_commands,
    evaluate_verification_command_policy,
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

SecurityEventHandler = Callable[[str, Mapping[str, object]], None]


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
        "name": "update_plan",
        "description": (
            "Replace the complete session plan. This updates resumable session "
            "state only and never writes workspace files."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "explanation": {
                    "type": "string",
                    "maxLength": PLAN_MAX_EXPLANATION_CHARS,
                    "description": "Optional short reason for the plan update.",
                },
                "items": {
                    "type": "array",
                    "minItems": PLAN_MIN_ITEMS,
                    "maxItems": PLAN_MAX_ITEMS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "step": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": PLAN_MAX_STEP_CHARS,
                            },
                            "status": {
                                "type": "string",
                                "enum": sorted(PLAN_STATUSES),
                            },
                        },
                        "required": ["step", "status"],
                    },
                },
            },
            "required": ["items"],
        },
    },
    {
        "type": "function",
        "name": "submit_review",
        "description": (
            "Submit the one final structured result for a review-mode session. "
            "Every finding path and line is revalidated without returning file text."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": REVIEW_MAX_SUMMARY_CHARS,
                },
                "findings": {
                    "type": "array",
                    "maxItems": REVIEW_MAX_FINDINGS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "severity": {
                                "type": "string",
                                "enum": ["critical", "high", "medium", "low"],
                            },
                            "path": {"type": "string", "minLength": 1},
                            "line": {"type": "integer", "minimum": 1},
                            "title": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": REVIEW_MAX_TITLE_CHARS,
                            },
                            "detail": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": REVIEW_MAX_DETAIL_CHARS,
                            },
                        },
                        "required": [
                            "severity",
                            "path",
                            "line",
                            "title",
                            "detail",
                        ],
                    },
                },
            },
            "required": ["summary", "findings"],
        },
    },
    {
        "type": "function",
        "name": "run_command",
        "description": "Run a structured command in the workspace and return stdout/stderr. Arguments are passed directly without a shell.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "argv": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_COMMAND_ARGUMENTS,
                    "items": {"type": "string", "minLength": 1},
                    "description": "Command executable and arguments. Each item is passed directly without a shell.",
                },
                "cwd": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional workspace-relative working directory. Defaults to .",
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_COMMAND_TIMEOUT_MS,
                    "description": "Timeout in milliseconds. Defaults to 30000.",
                },
            },
            "required": ["argv"],
        },
    },
]

_EXPOSED_TOOL_NAMES = frozenset(
    definition["name"] for definition in TOOL_DEFINITIONS
)


def execute_tool(
    config: AgentConfig,
    name: str,
    raw_arguments: str,
    *,
    state: VerificationToolState | None = None,
    plan_state: PlanState | None = None,
    review_result: ReviewResult | None = None,
    approval_handler: ApprovalHandler | None = None,
    call_id: str | None = None,
    session_id: str | None = None,
    security_event_handler: SecurityEventHandler | None = None,
) -> ToolResult:
    try:
        if name == "write_file":
            return ToolResult(
                ok=False,
                output="write_file is disabled. Submit a unified diff through apply_patch so every edit is reviewable.",
            )
        if name not in _EXPOSED_TOOL_NAMES:
            return ToolResult(ok=False, output=f"Unknown tool: {name}")
        if not is_tool_allowed(config.task_mode, name):
            return ToolResult(
                ok=False,
                output=(
                    f"Tool {name!r} is not allowed in "
                    f"{config.task_mode} task mode."
                ),
                data={
                    "type": "task_mode_policy_rejection",
                    "task_mode": config.task_mode,
                    "tool_name": name,
                    "status": "denied",
                    "disposition": "deny",
                    "requires_approval": False,
                },
            )

        args = _parse_tool_arguments(raw_arguments)
        arguments_sha256 = hash_tool_arguments(raw_arguments)
        effective_call_id = call_id or f"direct-{name}-{arguments_sha256[:16]}"
        handler = approval_handler or build_default_approval_handler(config)

        if name == "update_plan":
            return _update_plan_tool(args, previous_plan=plan_state or EMPTY_PLAN)
        if name == "submit_review":
            return _submit_review_tool(
                config,
                args,
                previous_review=review_result,
            )
        if name == "read_file":
            return _read_file_tool(config, args)
        if name == "read_many_files":
            return _read_many_files_tool(config, args)
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
                session_id=session_id,
                security_event_handler=security_event_handler,
            )
        if name == "git_status":
            return _run_internal_argv_command(
                ("git", "status", "--short"),
                config,
                purpose="Inspect concise Git status",
            )
        if name == "git_diff":
            return _git_diff_tool(config)
        if name == "run_command":
            return _run_command_tool(
                config,
                args,
                approval_handler=handler,
                call_id=effective_call_id,
                arguments_sha256=arguments_sha256,
                session_id=session_id,
                security_event_handler=security_event_handler,
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


def _update_plan_tool(
    args: dict[str, Any],
    *,
    previous_plan: PlanState,
) -> ToolResult:
    updated = parse_plan_update(args)
    validate_plan_transition(previous_plan, updated)
    counts = {
        status: sum(item.status == status for item in updated.items)
        for status in PLAN_STATUSES
    }
    return ToolResult(
        ok=True,
        output=(
            f"Plan updated with {len(updated.items)} items: "
            f"{counts['completed']} completed, "
            f"{counts['in_progress']} in progress, "
            f"{counts['pending']} pending."
        ),
        data={
            "type": "plan_update",
            "plan": plan_state_to_dict(updated),
        },
    )


def _submit_review_tool(
    config: AgentConfig,
    args: dict[str, Any],
    *,
    previous_review: ReviewResult | None,
) -> ToolResult:
    if previous_review is not None:
        return ToolResult(
            ok=False,
            output="A final review has already been submitted for this session.",
        )

    submitted = parse_review_submission(args)
    root = Path(config.workspace).resolve(strict=True)
    sensitive_policy = load_sensitive_path_policy(root)
    ignore_policy = load_ignore_policy(root)
    line_counts: dict[str, int] = {}
    normalized_findings: list[ReviewFinding] = []

    for finding in submitted.findings:
        full_path = resolve_workspace_path(
            root,
            finding.path,
            operation="read",
            allow_missing=False,
        )
        normalized_path = full_path.relative_to(root).as_posix()
        sensitive_decision = sensitive_policy.evaluate(
            full_path,
            operation="read",
        )
        if not sensitive_decision.allowed:
            raise ValueError(
                f"Review finding path is sensitive and cannot be submitted: "
                f"{normalized_path}"
            )
        if not full_path.is_file():
            raise ValueError(
                f"Review finding path is not an existing file: {normalized_path}"
            )

        line_count = line_counts.get(normalized_path)
        if line_count is None:
            if ignore_policy.is_binary(full_path):
                raise ValueError(
                    "Review finding path is not a text file: "
                    f"{normalized_path}"
                )
            raw = full_path.read_bytes()
            if b"\x00" in raw:
                raise ValueError(
                    "Review finding path is not a text file: "
                    f"{normalized_path}"
                )
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    "Review finding path is not valid UTF-8 text: "
                    f"{normalized_path}"
                ) from exc
            line_count = len(text.splitlines())
            line_counts[normalized_path] = line_count

        if finding.line > line_count:
            raise ValueError(
                f"Review finding line {finding.line} exceeds the "
                f"{line_count} lines in {normalized_path}."
            )
        normalized_findings.append(
            ReviewFinding(
                severity=finding.severity,
                path=normalized_path,
                line=finding.line,
                title=finding.title,
                detail=finding.detail,
            )
        )

    review = ReviewResult(
        summary=submitted.summary,
        findings=tuple(normalized_findings),
    )
    return ToolResult(
        ok=True,
        output=(
            f"Review submitted with {len(review.findings)} structured "
            "finding(s)."
        ),
        data={
            "type": "review_submission",
            "review": review_result_to_dict(review),
        },
    )


def _read_file_tool(config: AgentConfig, args: dict[str, Any]) -> ToolResult:
    relative_path = _require_string(args.get("path"), "path")
    max_bytes = _require_positive_int(
        args,
        "max_bytes",
        default=DEFAULT_READ_FILE_MAX_BYTES,
        maximum=MAX_READ_BYTES_PER_FILE,
    )

    root = Path(config.workspace).resolve()
    full_path = resolve_workspace_path(
        root,
        relative_path,
        operation="read",
        allow_missing=True,
    )
    normalized_path = full_path.relative_to(root).as_posix()
    sensitive_policy = load_sensitive_path_policy(root)
    sensitive_decision = sensitive_policy.evaluate(full_path, operation="read")
    if not sensitive_decision.allowed:
        return _sensitive_path_denied_result(normalized_path, operation="read")

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

    full_path = resolve_workspace_path(
        root,
        relative_path,
        operation="read",
        allow_missing=False,
    )
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
        data={
            "type": "read_evidence",
            "files": explanation_read_evidence_list_to_dict(
                (
                    ExplanationReadEvidence(
                        path=normalized_path,
                        max_line=len(content.splitlines()),
                        truncated=len(data) > max_bytes,
                    ),
                )
            ),
        },
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
        data={
            "type": "read_evidence",
            "files": explanation_read_evidence_list_to_dict(
                ExplanationReadEvidence(
                    path=result.path,
                    max_line=len(result.content.splitlines()),
                    truncated=result.truncated,
                )
                for result in results
                if result.ok
            ),
        },
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
    before_hashes = _hash_patch_plan_files(patch_plan)
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

    current_before_hashes = _hash_patch_plan_files(patch_plan)
    if current_before_hashes != before_hashes:
        raise RuntimeError(
            "Workspace changed after patch review; refusing to apply the approved diff."
        )

    apply_patch_plan(patch_plan)
    after_hashes = _hash_patch_plan_files(patch_plan)
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
    full_path = resolve_workspace_path(
        root,
        relative_path,
        operation="list",
        allow_missing=False,
    )
    normalized_path = full_path.relative_to(root).as_posix()
    sensitive_policy = load_sensitive_path_policy(root)
    if not sensitive_policy.evaluate(full_path, operation="list").allowed:
        return _sensitive_path_denied_result(normalized_path, operation="list")

    ignore_policy = load_ignore_policy(config.workspace)
    if ignore_policy.is_ignored(full_path):
        return ToolResult(ok=False, output=f"Path is ignored: {relative_path}")

    entries = sorted(
        f"{'dir ' if entry.is_dir() else 'file'} {entry.name}"
        for entry in full_path.iterdir()
        if not ignore_policy.is_ignored(entry)
        and sensitive_policy.evaluate(entry, operation="list").allowed
    )
    return ToolResult(ok=True, output="\n".join(entries) or "(empty directory)")


def _search_text_tool(config: AgentConfig, args: dict[str, Any]) -> ToolResult:
    pattern = _require_string(args.get("pattern"), "pattern")
    relative_path = _optional_string_argument(args, "path", default=".")
    root = Path(config.workspace).resolve()
    full_path = resolve_workspace_path(
        root,
        relative_path,
        operation="search",
        allow_missing=True,
    )
    normalized_path = full_path.relative_to(root).as_posix()
    sensitive_policy = load_sensitive_path_policy(root)
    if not sensitive_policy.evaluate(full_path, operation="search").allowed:
        return _sensitive_path_denied_result(normalized_path, operation="search")

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


def _sensitive_path_denied_result(path: str, *, operation: str) -> ToolResult:
    return ToolResult(
        ok=False,
        output=(
            f"{SENSITIVE_PATH_DENIAL_REASON}: "
            f"{operation} access to sensitive workspace path {path!r} is denied."
        ),
        data={
            "reason": SENSITIVE_PATH_DENIAL_REASON,
            "operation": operation,
            "path": path,
        },
    )


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
    session_id: str | None,
    security_event_handler: SecurityEventHandler | None,
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
    max_output_bytes = _require_positive_int(
        args,
        "max_output_bytes",
        default=DEFAULT_VERIFICATION_MAX_OUTPUT_BYTES,
        maximum=MAX_VERIFICATION_OUTPUT_BYTES,
    )
    max_output_lines = _require_positive_int(
        args,
        "max_output_lines",
        default=DEFAULT_VERIFICATION_MAX_OUTPUT_LINES,
        maximum=MAX_VERIFICATION_OUTPUT_LINES,
    )
    selected_command = next(
        (
            command
            for command in current_state.discovery.commands
            if command.id == command_id
        ),
        None,
    )
    policy_decision = _verification_policy_decision_or_none(
        config,
        command_id=command_id,
        command=selected_command,
        discovery=current_state.discovery,
        timeout_ms=timeout_ms,
        max_output_bytes=max_output_bytes,
        max_output_lines=max_output_lines,
    )
    if policy_decision is not None:
        _emit_security_event(
            security_event_handler,
            "security.policy_evaluated",
            {"policy": policy_decision.to_dict()},
        )
    use_docker = (
        policy_decision is not None
        and _should_use_docker(config, policy_decision)
    )

    def approve(command: VerificationCommand) -> bool:
        nonlocal policy_decision
        if policy_decision is None:
            policy_decision = evaluate_verification_command_policy(
                config.workspace,
                command_id=command_id,
                command=command,
                discovery=current_state.discovery,
                timeout_ms=timeout_ms,
                max_output_bytes=max_output_bytes,
                max_output_lines=max_output_lines,
            )
        command_spec = build_verification_command_spec(
            config.workspace,
            command_id=command_id,
            command=command,
            discovery=current_state.discovery,
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
            max_output_lines=max_output_lines,
        )
        request = ApprovalRequest(
            call_id=call_id,
            action="run_verification",
            summary=f"Run verification command {command.id}",
            arguments_sha256=arguments_sha256,
            details={
                "command_id": command.id,
                "kind": command.kind,
                "argv": list(command.argv),
                "cwd": command_spec.cwd,
                "timeout_ms": timeout_ms,
                "shell": False,
                "backend": "docker" if use_docker else "host",
                "sandboxed": use_docker,
                "network_mode": "none" if use_docker else "host",
                "image_reference": config.sandbox_image if use_docker else None,
                "image_digest": (
                    config.sandbox_image_digest if use_docker else None
                ),
                **_command_policy_fields(policy_decision),
            },
        )
        return _approval_granted(approval_handler, request)

    if (
        selected_command is not None
        and selected_command.available
        and policy_decision is not None
        and use_docker
    ):
        approval_granted = False
        if policy_decision.requires_approval:
            if not approve(selected_command):
                return _verification_not_executed_result(
                    selected_command,
                    current_state,
                    attempt=attempt,
                    timeout_ms=timeout_ms,
                    policy_decision=policy_decision,
                    status="approval_declined",
                    output="User declined verification command execution.",
                    backend="docker",
                    workspace=config.workspace,
                )
            approval_granted = True

        command_spec = build_verification_command_spec(
            config.workspace,
            command_id=command_id,
            command=selected_command,
            discovery=current_state.discovery,
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
            max_output_lines=max_output_lines,
        )
        execution = _execute_docker_command(
            config,
            command_spec,
            policy_decision,
            approval_granted=approval_granted,
            session_id=session_id,
            call_id=call_id,
            security_event_handler=security_event_handler,
        )
        if isinstance(execution, ToolResult):
            return _verification_not_executed_result(
                selected_command,
                current_state,
                attempt=attempt,
                timeout_ms=timeout_ms,
                policy_decision=policy_decision,
                status=str((execution.data or {}).get("status", "internal_error")),
                output=execution.output,
                backend="docker",
                workspace=config.workspace,
                execution_data=execution.data,
            )
        return _docker_verification_result(
            execution,
            selected_command,
            current_state,
            attempt=attempt,
            timeout_ms=timeout_ms,
            workspace=config.workspace,
        )

    result = run_verification_command(
        config.workspace,
        command_id=command_id,
        discovery=current_state.discovery,
        timeout_ms=timeout_ms,
        max_output_bytes=max_output_bytes,
        max_output_lines=max_output_lines,
        attempt=attempt,
        approval_callback=approve,
    )
    current_state.record_verification(result)
    host_backend = (
        "host"
        if policy_decision is not None
        and policy_decision.disposition in {"allow_host", "approval_required"}
        else None
    )
    data = _verification_result_data(
        result,
        state=current_state,
        timeout_ms=timeout_ms,
        policy_decision=policy_decision,
        workspace=config.workspace,
        backend=host_backend,
    )
    if policy_decision is not None and policy_decision.disposition in {
        "deny",
        "sandbox_required",
    }:
        data["verification_status"] = result.status
        data["status"] = (
            "denied"
            if policy_decision.disposition == "deny"
            else "sandbox_unavailable"
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
    policy_decision: CommandPolicyDecision | None,
    workspace: str | Path,
    backend: str | None,
) -> dict[str, object]:
    workspace_root = Path(workspace).resolve()
    result_cwd = Path(result.cwd).resolve()
    try:
        relative_cwd = result_cwd.relative_to(workspace_root).as_posix()
    except ValueError:
        relative_cwd = result.cwd
    data: dict[str, object] = {
        "type": "secure_command_result",
        "command_id": result.command_id,
        "kind": result.kind,
        "status": result.status,
        "argv": list(result.argv),
        "cwd": relative_cwd,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "timeout_ms": timeout_ms,
        "timed_out": result.status == "timed_out",
        "shell": False,
        "backend": backend,
        "sandboxed": backend == "docker",
        "image_digest": None,
        "network_mode": (
            "none" if backend == "docker" else "host" if backend == "host" else None
        ),
        "output_truncated": result.truncated,
        "omitted_lines": result.omitted_lines,
        "omitted_bytes": result.omitted_bytes,
        "attempt": result.attempt,
        "active_failure_command_id": state.unresolved_failure_command_id,
        "repair_attempts": state.repair_attempts,
        "max_fix_attempts": state.max_fix_attempts,
        "repair_limit_reached": state.repair_limit_reached,
    }
    if policy_decision is not None:
        data.update(_command_policy_fields(policy_decision))
    return data


def _verification_not_executed_result(
    command: VerificationCommand,
    state: VerificationToolState,
    *,
    attempt: int,
    timeout_ms: int,
    policy_decision: CommandPolicyDecision,
    status: str,
    output: str,
    backend: str,
    workspace: str | Path,
    execution_data: dict[str, object] | None = None,
) -> ToolResult:
    result = VerificationResult(
        command_id=command.id,
        kind=command.kind,
        status="error",
        argv=command.argv,
        cwd=command.cwd,
        exit_code=None,
        duration_ms=0,
        output=output,
        truncated=False,
        omitted_lines=0,
        omitted_bytes=0,
        attempt=attempt,
    )
    state.record_verification(result)
    data = _verification_result_data(
        result,
        state=state,
        timeout_ms=timeout_ms,
        policy_decision=policy_decision,
        workspace=workspace,
        backend=backend,
    )
    data["status"] = status
    data["verification_status"] = result.status
    if execution_data is not None:
        data.update(execution_data)
        data["verification_status"] = result.status
    return ToolResult(ok=False, output=output, data=data)


def _docker_verification_result(
    outcome: SandboxExecutionOutcome,
    command: VerificationCommand,
    state: VerificationToolState,
    *,
    attempt: int,
    timeout_ms: int,
    workspace: str | Path,
) -> ToolResult:
    secure_result = outcome.result
    verification_status = (
        secure_result.status
        if secure_result.status in {"passed", "failed", "timed_out"}
        else "error"
    )
    result = VerificationResult(
        command_id=command.id,
        kind=command.kind,
        status=verification_status,  # type: ignore[arg-type]
        argv=command.argv,
        cwd=command.cwd,
        exit_code=secure_result.exit_code,
        duration_ms=secure_result.duration_ms,
        output=secure_result.output or secure_result.error_reason or "",
        truncated=secure_result.output_truncated,
        omitted_lines=secure_result.omitted_lines,
        omitted_bytes=secure_result.omitted_bytes,
        attempt=attempt,
    )
    state.record_verification(result)
    tool_result = _docker_tool_result(outcome)
    data = dict(tool_result.data or {})
    data.update(
        {
            "command_id": command.id,
            "kind": command.kind,
            "attempt": attempt,
            "verification_status": result.status,
            "active_failure_command_id": state.unresolved_failure_command_id,
            "repair_attempts": state.repair_attempts,
            "max_fix_attempts": state.max_fix_attempts,
            "repair_limit_reached": state.repair_limit_reached,
        }
    )
    return ToolResult(
        ok=result.status == "passed",
        output=result.output or f"Verification {result.status}: {command.id}",
        data=data,
    )


def _verification_policy_decision_or_none(
    config: AgentConfig,
    *,
    command_id: str,
    command: VerificationCommand | None,
    discovery: VerificationDiscoveryResult,
    timeout_ms: int,
    max_output_bytes: int,
    max_output_lines: int,
) -> CommandPolicyDecision | None:
    if command is None or not command.available:
        return None
    try:
        return evaluate_verification_command_policy(
            config.workspace,
            command_id=command_id,
            command=command,
            discovery=discovery,
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
            max_output_lines=max_output_lines,
        )
    except ValueError:
        return None


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
    session_id: str | None,
    security_event_handler: SecurityEventHandler | None,
) -> ToolResult:
    if "command" in args:
        return ToolResult(
            ok=False,
            output=(
                'run_command no longer accepts "command"; provide a non-empty "argv" array.'
            ),
        )

    _reject_unknown_arguments(args, {"argv", "cwd", "timeout_ms"})
    argv = _require_argv(args)
    cwd = _optional_string_argument(args, "cwd", default=".")
    timeout_ms = _require_positive_int(
        args,
        "timeout_ms",
        default=DEFAULT_COMMAND_TIMEOUT_MS,
        maximum=MAX_COMMAND_TIMEOUT_MS,
    )
    resolved_cwd, command_spec, policy_decision = _evaluate_execution_policy(
        config,
        argv=argv,
        cwd=cwd,
        timeout_ms=timeout_ms,
        source="tool",
        purpose="Run a model-requested workspace command",
    )
    _emit_security_event(
        security_event_handler,
        "security.policy_evaluated",
        {"policy": policy_decision.to_dict()},
    )
    if policy_decision.disposition == "deny":
        return _blocked_command_result(
            argv,
            cwd=command_spec.cwd,
            timeout_ms=timeout_ms,
            policy_decision=policy_decision,
        )
    use_docker = _should_use_docker(config, policy_decision)
    if policy_decision.disposition == "sandbox_required" and not use_docker:
        return _blocked_command_result(
            argv,
            cwd=command_spec.cwd,
            timeout_ms=timeout_ms,
            policy_decision=policy_decision,
        )

    if config.permission_mode != "workspace-write" and _looks_mutating(argv):
        return ToolResult(
            ok=False,
            output="Command appears to modify files and read-only mode is active. Re-run with --write if this is intended.",
            data={
                "type": "secure_command_result",
                "argv": list(argv),
                "cwd": command_spec.cwd,
                "shell": False,
                "timeout_ms": timeout_ms,
                "exit_code": None,
                "timed_out": False,
                "duration_ms": 0,
                "status": "permission_denied",
                "backend": None,
                "sandboxed": False,
                "image_digest": None,
                **_command_policy_fields(policy_decision),
            },
        )

    approval_granted = False
    if policy_decision.requires_approval:
        rendered_argv = subprocess.list2cmdline(list(argv))
        request = ApprovalRequest(
            call_id=call_id,
            action="run_command",
            summary=f"Run command: {rendered_argv}",
            arguments_sha256=arguments_sha256,
            details={
                "argv": argv,
                "cwd": command_spec.cwd,
                "timeout_ms": timeout_ms,
                "shell": False,
                "backend": "docker" if use_docker else "host",
                "sandboxed": use_docker,
                "network_mode": "none" if use_docker else "host",
                "image_reference": config.sandbox_image if use_docker else None,
                "image_digest": config.sandbox_image_digest if use_docker else None,
                **_command_policy_fields(policy_decision),
            },
        )
        if not _approval_granted(approval_handler, request):
            return ToolResult(
                ok=False,
                output="User declined command execution.",
                data={
                    "type": "secure_command_result",
                    "argv": list(argv),
                    "cwd": command_spec.cwd,
                    "shell": False,
                    "timeout_ms": timeout_ms,
                    "exit_code": None,
                    "timed_out": False,
                    "duration_ms": 0,
                    "status": "approval_declined",
                    "backend": "docker" if use_docker else "host",
                    "sandboxed": use_docker,
                    "image_digest": config.sandbox_image_digest if use_docker else None,
                    **_command_policy_fields(policy_decision),
                },
            )
        approval_granted = True

    if use_docker:
        return _run_docker_command(
            config,
            command_spec,
            policy_decision,
            approval_granted=approval_granted,
            session_id=session_id,
            call_id=call_id,
            security_event_handler=security_event_handler,
        )

    return _run_argv_command(
        argv,
        config.workspace,
        cwd,
        timeout_ms,
        policy_decision=policy_decision,
        approval_granted=approval_granted,
        command_spec=command_spec,
    )


def _run_internal_argv_command(
    argv: tuple[str, ...],
    config: AgentConfig,
    *,
    purpose: str,
    cwd: str = ".",
    timeout_ms: int = DEFAULT_COMMAND_TIMEOUT_MS,
) -> ToolResult:
    _resolved_cwd, command_spec, policy_decision = _evaluate_execution_policy(
        config,
        argv=argv,
        cwd=cwd,
        timeout_ms=timeout_ms,
        source="internal",
        purpose=purpose,
    )
    return _run_argv_command(
        argv,
        config.workspace,
        cwd,
        timeout_ms,
        policy_decision=policy_decision,
        approval_granted=False,
        command_spec=command_spec,
    )


def _git_diff_tool(config: AgentConfig) -> ToolResult:
    stat = _run_internal_argv_command(
        ("git", "diff", "--stat"),
        config,
        purpose="Inspect Git diff statistics",
    )
    diff = _run_internal_argv_command(
        ("git", "diff"),
        config,
        purpose="Inspect the current Git diff",
    )
    command_results = [
        result.data
        for result in (stat, diff)
        if result.data is not None
    ]
    return ToolResult(
        ok=stat.ok and diff.ok,
        output="\n".join(["[git diff --stat]", stat.output, "", "[git diff]", diff.output]),
        data={
            "type": "command_batch_result",
            "commands": command_results,
        },
    )


def _evaluate_execution_policy(
    config: AgentConfig,
    *,
    argv: tuple[str, ...],
    cwd: str,
    timeout_ms: int,
    source: str,
    purpose: str,
) -> tuple[Path, CommandSpec, CommandPolicyDecision]:
    resolved_cwd = _resolve_command_cwd(config.workspace, cwd)
    root = Path(config.workspace).resolve(strict=True)
    relative_cwd = resolved_cwd.relative_to(root).as_posix()
    command = CommandSpec(
        argv=argv,
        cwd=relative_cwd,
        source=source,  # type: ignore[arg-type]
        purpose=purpose,
        limits=ExecutionLimits(timeout_ms=timeout_ms),
    )
    return resolved_cwd, command, evaluate_command_policy(command)


def _command_policy_fields(
    decision: CommandPolicyDecision,
) -> dict[str, object]:
    return {
        "policy_version": decision.policy_version,
        "rule_id": decision.rule_id,
        "disposition": decision.disposition,
        "reasons": list(decision.reasons),
        "normalized_executable": decision.normalized_executable,
        "requires_approval": decision.requires_approval,
        "requires_sandbox": decision.requires_sandbox,
        "policy": decision.to_dict(),
    }


def _blocked_command_result(
    argv: tuple[str, ...],
    *,
    cwd: str,
    timeout_ms: int,
    policy_decision: CommandPolicyDecision,
) -> ToolResult:
    status = (
        "denied"
        if policy_decision.disposition == "deny"
        else "sandbox_unavailable"
    )
    output = (
        "Docker sandbox routing is required and host fallback is disabled."
        if policy_decision.disposition == "approval_required"
        else format_command_policy_block(policy_decision)
    )
    return ToolResult(
        ok=False,
        output=output,
        data={
            "type": "secure_command_result",
            "argv": list(argv),
            "cwd": cwd,
            "shell": False,
            "timeout_ms": timeout_ms,
            "exit_code": None,
            "timed_out": False,
            "duration_ms": 0,
            "status": status,
            "backend": None,
            "sandboxed": False,
            "image_digest": None,
            **_command_policy_fields(policy_decision),
        },
    )


def _should_use_docker(
    config: AgentConfig,
    decision: CommandPolicyDecision,
) -> bool:
    if decision.disposition == "deny":
        return False
    if config.sandbox_mode == "docker":
        return True
    return config.sandbox_mode == "auto" and decision.requires_sandbox


def _run_docker_command(
    config: AgentConfig,
    command: CommandSpec,
    decision: CommandPolicyDecision,
    *,
    approval_granted: bool,
    session_id: str | None,
    call_id: str,
    security_event_handler: SecurityEventHandler | None,
) -> ToolResult:
    execution = _execute_docker_command(
        config,
        command,
        decision,
        approval_granted=approval_granted,
        session_id=session_id,
        call_id=call_id,
        security_event_handler=security_event_handler,
    )
    if isinstance(execution, ToolResult):
        return execution
    return _docker_tool_result(execution)


def _execute_docker_command(
    config: AgentConfig,
    command: CommandSpec,
    decision: CommandPolicyDecision,
    *,
    approval_granted: bool,
    session_id: str | None,
    call_id: str,
    security_event_handler: SecurityEventHandler | None = None,
) -> SandboxExecutionOutcome | ToolResult:
    backend = DockerSandboxBackend(image_reference=config.sandbox_image)
    if config.sandbox_image_digest is None:
        capability = backend.probe_capability(config.workspace)
    else:
        capability = SandboxCapability(
            backend="docker",
            available=True,
            reason=None,
            image_reference=config.sandbox_image,
            image_digest=config.sandbox_image_digest,
        )
    if not capability.available or capability.image_digest is None:
        _emit_security_event(
            security_event_handler,
            "sandbox.capability_checked",
            {"capability": capability.to_dict()},
        )
        return _docker_unavailable_result(
            command,
            decision,
            capability.reason or "Docker sandbox capability is unavailable.",
        )

    plan = SandboxExecutionPlan(
        command=command,
        decision=decision,
        capability=capability,
        backend="docker",
        sandboxed=True,
        network_mode="none",
        image_digest=capability.image_digest,
    )
    outcome = backend.execute(
        config.workspace,
        plan,
        session_id=_sandbox_identifier("session", session_id or config.workspace),
        call_id=_sandbox_identifier("call", call_id),
        approval_granted=approval_granted,
        event_handler=security_event_handler,
    )
    if outcome.backend_argv:
        persisted_result = outcome.result.to_dict()
        persisted_result["output"] = ""
        _emit_security_event(
            security_event_handler,
            "sandbox.finished",
            {"result": persisted_result},
        )
    for cleanup_kind, succeeded, error in (
        (
            "container",
            outcome.container_cleanup_succeeded,
            outcome.container_cleanup_error,
        ),
        (
            "snapshot",
            outcome.snapshot_cleanup_succeeded,
            outcome.snapshot_cleanup_error,
        ),
    ):
        if succeeded is False:
            _emit_security_event(
                security_event_handler,
                "sandbox.cleanup_failed",
                {
                    "cleanup_kind": cleanup_kind,
                    "reason": error or f"{cleanup_kind} cleanup failed",
                },
            )
    return outcome


def _docker_unavailable_result(
    command: CommandSpec,
    decision: CommandPolicyDecision,
    reason: str,
) -> ToolResult:
    return ToolResult(
        ok=False,
        output=reason,
        data={
            "type": "secure_command_result",
            "argv": list(command.argv),
            "cwd": command.cwd,
            "shell": False,
            "timeout_ms": command.limits.timeout_ms,
            "backend": "docker",
            "sandboxed": False,
            "image_digest": None,
            "exit_code": None,
            "timed_out": False,
            "duration_ms": 0,
            "output_truncated": False,
            "status": "sandbox_unavailable",
            "error_reason": reason,
            **_command_policy_fields(decision),
        },
    )


def _docker_tool_result(outcome: SandboxExecutionOutcome) -> ToolResult:
    result = outcome.result
    output = result.output or result.error_reason or f"Command {result.status}."
    return ToolResult(
        ok=result.status == "passed",
        output=output,
        data={
            "type": "secure_command_result",
            "argv": list(result.command.argv),
            "cwd": result.command.cwd,
            "shell": False,
            "timeout_ms": result.command.limits.timeout_ms,
            "backend": result.backend,
            "sandboxed": result.sandboxed,
            "image_digest": result.image_digest,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "duration_ms": result.duration_ms,
            "output_truncated": result.output_truncated,
            "omitted_lines": result.omitted_lines,
            "omitted_bytes": result.omitted_bytes,
            "status": result.status,
            "error_reason": result.error_reason,
            "network_mode": "none",
            "snapshot": (
                dict(outcome.snapshot_summary)
                if outcome.snapshot_summary is not None
                else None
            ),
            "snapshot_cleanup_succeeded": outcome.snapshot_cleanup_succeeded,
            "snapshot_cleanup_error": outcome.snapshot_cleanup_error,
            "container_cleanup_attempted": outcome.container_cleanup_attempted,
            "container_cleanup_succeeded": outcome.container_cleanup_succeeded,
            "container_cleanup_error": outcome.container_cleanup_error,
            **_command_policy_fields(result.decision),
        },
    )


def _sandbox_identifier(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def _emit_security_event(
    handler: SecurityEventHandler | None,
    event_type: str,
    payload: Mapping[str, object],
) -> None:
    if handler is not None:
        handler(event_type, payload)


def _looks_mutating(argv: tuple[str, ...]) -> bool:
    command = " ".join(argv)
    mutating_patterns = [
        r"\bnpm\s+(install|i|update|uninstall|remove|audit\s+fix)\b",
        r"\bpnpm\s+(add|install|update|remove)\b",
        r"\byarn\s+(add|install|upgrade|remove)\b",
        r"\brm\s+-",
        r"\bdel\b",
        r"\bRemove-Item\b",
        r"\bgit\s+(checkout|reset|clean|apply|am|merge|rebase|commit)\b",
    ]
    return any(re.search(pattern, command, flags=re.IGNORECASE) for pattern in mutating_patterns)


def _resolve_command_cwd(workspace: str | Path, cwd: str) -> Path:
    root = Path(workspace).resolve(strict=True)
    return resolve_workspace_path(
        root,
        cwd,
        operation="execute",
        allow_missing=False,
    )


def _run_argv_command(
    argv: tuple[str, ...],
    workspace: str | Path,
    cwd: str,
    timeout_ms: int,
    *,
    policy_decision: CommandPolicyDecision,
    approval_granted: bool,
    command_spec: CommandSpec,
) -> ToolResult:
    resolved_cwd = _resolve_command_cwd(workspace, cwd)
    if policy_decision.disposition in {"deny", "sandbox_required"}:
        return _blocked_command_result(
            argv,
            cwd=command_spec.cwd,
            timeout_ms=timeout_ms,
            policy_decision=policy_decision,
        )
    if policy_decision.disposition not in {"allow_host", "approval_required"}:
        raise ValueError(
            f"Unsupported host execution disposition: {policy_decision.disposition}"
        )

    workspace_root = Path(workspace).resolve(strict=True)
    relative_cwd = resolved_cwd.relative_to(workspace_root).as_posix()
    if (
        command_spec.argv != argv
        or command_spec.cwd != relative_cwd
        or command_spec.limits.timeout_ms != timeout_ms
    ):
        raise ValueError(
            "Authorized command specification changed before process execution."
        )
    process_result = run_host_process(
        workspace_root,
        command_spec,
        policy_decision,
        approval_granted=approval_granted,
    )
    metadata = _host_process_fields(process_result)

    if process_result.status == "timed_out":
        output = "\n".join(
            part
            for part in [
                f"Command timed out after {timeout_ms}ms.",
                process_result.stdout.strip(),
                process_result.stderr.strip(),
            ]
            if part
        )
    elif process_result.status in {"not_found", "error"}:
        output = process_result.error_reason or "Failed to execute command."
    else:
        output = "\n".join(
            part
            for part in [
                f"exit code: {process_result.exit_code}",
                process_result.stdout.strip(),
                process_result.stderr.strip(),
            ]
            if part
        )

    return ToolResult(
        ok=process_result.status == "passed",
        output=output,
        data={
            "type": "secure_command_result",
            "argv": list(argv),
            "cwd": command_spec.cwd,
            "shell": False,
            "timeout_ms": timeout_ms,
            "exit_code": process_result.exit_code,
            "timed_out": process_result.timed_out,
            "duration_ms": process_result.duration_ms,
            "status": process_result.status,
            "backend": "host",
            "sandboxed": False,
            "image_digest": None,
            "network_mode": "host",
            **metadata,
            **_command_policy_fields(policy_decision),
        },
    )


def _host_process_fields(process_result: HostProcessResult) -> dict[str, object]:
    return {
        "actual_executable": process_result.actual_executable,
        "allowed_environment_keys": list(process_result.allowed_environment_keys),
        "output_truncated": process_result.output_truncated,
        "omitted_lines": process_result.omitted_lines,
        "omitted_bytes": process_result.omitted_bytes,
        "process_tree_terminated": process_result.process_tree_terminated,
        "cleanup_error": process_result.cleanup_error,
        "error_reason": process_result.error_reason,
    }


def _approval_granted(
    handler: ApprovalHandler,
    request: ApprovalRequest,
) -> bool:
    decision = handler(request)
    validate_approval_decision(request, decision)
    return decision.outcome == "approved"


def _hash_patch_plan_files(plan: PatchPlan) -> dict[str, str | None]:
    return {
        file.path: _hash_patch_file(plan, file)
        for file in plan.files
    }


def _hash_patch_file(plan: PatchPlan, file: FilePatchPlan) -> str | None:
    path = resolve_workspace_path(
        plan.workspace,
        file.path,
        operation="write",
        allow_missing=True,
    )
    return _hash_file_or_none(path)


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


def _require_argv(args: dict[str, Any]) -> tuple[str, ...]:
    values = _require_string_list(
        args,
        "argv",
        required=True,
        allow_empty=False,
        maximum_items=MAX_COMMAND_ARGUMENTS,
    )
    assert values is not None
    for index, value in enumerate(values):
        if "\x00" in value:
            raise ValueError(f"argv[{index}] must not contain NUL characters.")
    return tuple(values)



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


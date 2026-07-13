from dataclasses import dataclass
from typing import Literal

ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh"]
PermissionMode = Literal["read-only", "workspace-write"]


@dataclass(frozen=True)
class AgentConfig:
    workspace: str
    model: str
    reasoning_effort: ReasoningEffort
    max_turns: int
    permission_mode: PermissionMode
    auto_approve_commands: bool
    auto_approve_edits: bool
    context_max_files: int
    context_max_bytes_per_file: int


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str


@dataclass(frozen=True)
class FileReadResult:
    path: str
    ok: bool
    content: str
    truncated: bool
    error: str | None = None
    instruction_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceFile:
    path: str
    size: int


@dataclass(frozen=True)
class WorkspaceSample:
    path: str
    content: str


@dataclass(frozen=True)
class WorkspaceSnapshot:
    root: str
    files: list[WorkspaceFile]
    samples: list[WorkspaceSample]
    total_file_count: int
    omitted_file_count: int

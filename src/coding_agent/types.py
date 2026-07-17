from dataclasses import dataclass
from typing import Literal

ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh"]
PermissionMode = Literal["read-only", "workspace-write"]
SandboxMode = Literal["none", "auto", "docker"]
MAX_FIX_ATTEMPTS = 10


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
    max_fix_attempts: int = 3
    sandbox_mode: SandboxMode = "none"
    sandbox_image: str = "python:3.12-slim"
    sandbox_image_digest: str | None = None
    full_auto: bool = False


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str
    data: dict[str, object] | None = None


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

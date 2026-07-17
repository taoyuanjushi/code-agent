import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .ignore import load_ignore_policy
from .path_safety import is_link_or_reparse_point, resolve_workspace_path
from .security.path_policy import load_sensitive_path_policy

MAX_AGENT_INSTRUCTION_BYTES = 16 * 1024


@dataclass(frozen=True)
class AgentInstruction:
    path: str
    directory: str
    content: str
    truncated: bool = False


def discover_agent_instructions(
    workspace: str | Path,
    max_bytes_per_file: int = MAX_AGENT_INSTRUCTION_BYTES,
) -> list[AgentInstruction]:
    if max_bytes_per_file <= 0:
        raise ValueError("max_bytes_per_file must be positive.")

    root = Path(workspace).resolve()
    ignore_policy = load_ignore_policy(root)
    sensitive_policy = load_sensitive_path_policy(root)
    instructions: list[AgentInstruction] = []

    for current_directory, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        directory = Path(current_directory)
        directory_names.sort()
        file_names.sort()
        directory_names[:] = [
            name
            for name in directory_names
            if not is_link_or_reparse_point(directory / name)
            and not ignore_policy.is_ignored(directory / name)
            and sensitive_policy.evaluate(
                directory / name,
                operation="read",
            ).allowed
        ]

        if "AGENTS.md" not in file_names:
            continue

        requested_instruction_path = directory / "AGENTS.md"
        try:
            instruction_path = resolve_workspace_path(
                root,
                requested_instruction_path,
                operation="read",
                allow_missing=False,
            )
        except (OSError, ValueError):
            continue
        if not sensitive_policy.evaluate(
            instruction_path,
            operation="read",
        ).allowed:
            continue

        content, truncated = _read_instruction(
            root,
            requested_instruction_path,
            max_bytes_per_file,
        )
        relative_path = requested_instruction_path.relative_to(root).as_posix()
        relative_directory = directory.relative_to(root).as_posix()
        instructions.append(
            AgentInstruction(
                path=relative_path,
                directory=relative_directory,
                content=content,
                truncated=truncated,
            )
        )

    return sorted(
        instructions,
        key=lambda instruction: (
            _scope_depth(instruction.directory),
            instruction.path,
        ),
    )


def instructions_for_path(
    instructions: list[AgentInstruction],
    target_path: str,
) -> list[AgentInstruction]:
    target_parts = _normalize_relative_path(target_path).parts
    applicable = [
        instruction
        for instruction in instructions
        if _scope_applies(instruction.directory, target_parts)
    ]
    return sorted(
        applicable,
        key=lambda instruction: (
            _scope_depth(instruction.directory),
            instruction.path,
        ),
    )


def format_agent_instructions(instructions: list[AgentInstruction]) -> str:
    if not instructions:
        return "(no applicable AGENTS.md instructions)"

    sections: list[str] = []
    for instruction in instructions:
        scope = "workspace root" if instruction.directory == "." else instruction.directory
        sections.append(
            f"### {instruction.path}\n"
            f"Scope: {scope}\n\n"
            f"{instruction.content}"
        )
    return "\n\n".join(sections)


def _read_instruction(
    root: Path,
    requested_path: Path,
    max_bytes: int,
) -> tuple[str, bool]:
    path = resolve_workspace_path(
        root,
        requested_path,
        operation="read",
        allow_missing=False,
    )
    data = path.read_bytes()
    truncated = len(data) > max_bytes
    content = data[:max_bytes].decode("utf-8", errors="replace")
    if truncated:
        content = f"{content}\n\n[Truncated after {max_bytes} bytes]"
    return content, truncated


def _normalize_relative_path(path: str) -> PurePosixPath:
    windows_candidate = PureWindowsPath(path)
    normalized = path.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if (
        candidate.is_absolute()
        or bool(windows_candidate.drive)
        or bool(windows_candidate.root)
        or ".." in candidate.parts
    ):
        raise ValueError(f"Target path must be workspace-relative: {path}")
    return candidate


def _scope_applies(directory: str, target_parts: tuple[str, ...]) -> bool:
    if directory == ".":
        return True

    scope_parts = PurePosixPath(directory).parts
    return target_parts[: len(scope_parts)] == scope_parts


def _scope_depth(directory: str) -> int:
    if directory == ".":
        return 0
    return len(PurePosixPath(directory).parts)

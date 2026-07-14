from pathlib import Path

from .ignore import IgnorePolicy, load_ignore_policy
from .instructions import (
    AgentInstruction,
    discover_agent_instructions,
    format_agent_instructions,
    instructions_for_path,
)
from .path_safety import resolve_inside_workspace
from .types import FileReadResult

DEFAULT_MAX_FILES = 20
DEFAULT_MAX_BYTES_PER_FILE = 30_000
DEFAULT_MAX_TOTAL_BYTES = 120_000


def read_many_files(
    workspace: str,
    paths: list[str],
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes_per_file: int = DEFAULT_MAX_BYTES_PER_FILE,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> list[FileReadResult]:
    _validate_limits(max_files, max_bytes_per_file, max_total_bytes)
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ValueError("paths must be a list of strings.")

    root = Path(workspace).resolve()
    ignore_policy = load_ignore_policy(root)
    repository_instructions = discover_agent_instructions(root)
    remaining_bytes = max_total_bytes
    results: list[FileReadResult] = []

    for index, requested_path in enumerate(paths):
        try:
            full_path = resolve_inside_workspace(root, requested_path)
            normalized_path = full_path.relative_to(root).as_posix()
        except (OSError, ValueError) as exc:
            results.append(_failed_result(requested_path, str(exc)))
            continue

        instruction_paths = _instruction_paths_for_target(
            repository_instructions,
            normalized_path,
        )

        if index >= max_files:
            results.append(
                _failed_result(
                    normalized_path,
                    f"File count limit exceeded (max_files={max_files}).",
                    instruction_paths,
                )
            )
            continue

        if remaining_bytes <= 0:
            results.append(
                _failed_result(
                    normalized_path,
                    f"Total byte limit exhausted (max_total_bytes={max_total_bytes}).",
                    instruction_paths,
                )
            )
            continue

        result = _read_one_file(
            full_path=full_path,
            normalized_path=normalized_path,
            instruction_paths=instruction_paths,
            ignore_policy=ignore_policy,
            max_bytes=min(max_bytes_per_file, remaining_bytes),
        )
        results.append(result)
        if result.ok:
            remaining_bytes -= len(result.content.encode("utf-8"))

    return results


def format_file_read_results(
    workspace: str,
    results: list[FileReadResult],
) -> str:
    repository_instructions = discover_agent_instructions(workspace)
    required_instruction_paths = {
        path
        for result in results
        for path in result.instruction_paths
    }
    shared_instructions = [
        instruction
        for instruction in repository_instructions
        if instruction.path in required_instruction_paths
    ]

    sections: list[str] = []
    if shared_instructions:
        sections.append(
            "===== Repository instructions =====\n"
            f"{format_agent_instructions(shared_instructions)}"
        )

    for result in results:
        applicable = (
            ", ".join(result.instruction_paths)
            if result.instruction_paths
            else "(none)"
        )
        lines = [
            f"===== {result.path} =====",
            f"status: {'ok' if result.ok else 'error'}",
            f"applicable AGENTS.md: {applicable}",
        ]
        if result.ok:
            lines.extend(
                [
                    f"truncated: {'yes' if result.truncated else 'no'}",
                    "",
                    result.content,
                ]
            )
        else:
            lines.append(f"error: {result.error or 'Unknown file read error.'}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections) or "(no files requested)"


def _read_one_file(
    *,
    full_path: Path,
    normalized_path: str,
    instruction_paths: tuple[str, ...],
    ignore_policy: IgnorePolicy,
    max_bytes: int,
) -> FileReadResult:
    try:
        if not full_path.exists():
            return _failed_result(
                normalized_path,
                "File does not exist.",
                instruction_paths,
            )
        if not full_path.is_file():
            return _failed_result(
                normalized_path,
                "Path is not a file.",
                instruction_paths,
            )
        if _is_ignored_file(ignore_policy, full_path):
            return _failed_result(
                normalized_path,
                "File is ignored by workspace policy.",
                instruction_paths,
            )
        if ignore_policy.is_binary(full_path):
            return _failed_result(
                normalized_path,
                "Binary file cannot be read as text.",
                instruction_paths,
            )

        data = full_path.read_bytes()
        if b"\0" in data[:8_000]:
            return _failed_result(
                normalized_path,
                "Binary file cannot be read as text.",
                instruction_paths,
            )

        content, consumed_bytes = _decode_utf8_prefix(data, max_bytes)
        return FileReadResult(
            path=normalized_path,
            ok=True,
            content=content,
            truncated=consumed_bytes < len(data),
            error=None,
            instruction_paths=instruction_paths,
        )
    except (OSError, UnicodeError) as exc:
        return _failed_result(normalized_path, str(exc), instruction_paths)


def _decode_utf8_prefix(data: bytes, max_bytes: int) -> tuple[str, int]:
    data.decode("utf-8")
    prefix = data[:max_bytes]
    while prefix:
        try:
            return prefix.decode("utf-8"), len(prefix)
        except UnicodeDecodeError as exc:
            prefix = prefix[: exc.start]
    return "", 0


def _instruction_paths_for_target(
    instructions: list[AgentInstruction],
    target_path: str,
) -> tuple[str, ...]:
    return tuple(
        instruction.path
        for instruction in instructions_for_path(instructions, target_path)
        if instruction.path != target_path
    )


def _is_ignored_file(ignore_policy: IgnorePolicy, path: Path) -> bool:
    if not ignore_policy.is_ignored(path):
        return False
    return not (
        path.name == "AGENTS.md"
        and not ignore_policy.is_ignored(path.parent)
    )


def _failed_result(
    path: str,
    error: str,
    instruction_paths: tuple[str, ...] = (),
) -> FileReadResult:
    return FileReadResult(
        path=path,
        ok=False,
        content="",
        truncated=False,
        error=error,
        instruction_paths=instruction_paths,
    )


def _validate_limits(
    max_files: int,
    max_bytes_per_file: int,
    max_total_bytes: int,
) -> None:
    for label, value in [
        ("max_files", max_files),
        ("max_bytes_per_file", max_bytes_per_file),
        ("max_total_bytes", max_total_bytes),
    ]:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} must be a positive integer.")

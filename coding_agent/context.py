from pathlib import Path

from .ignore import load_ignore_policy
from .ranking import rank_files, task_mentions_file
from .types import WorkspaceFile, WorkspaceSample, WorkspaceSnapshot

DEFAULT_MAX_INVENTORY_FILES = 400
DEFAULT_MAX_TOTAL_SAMPLE_BYTES = 64 * 1024

SAMPLE_PRIORITY_EXACT = (
    "README.md",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "settings.gradle",
    "Makefile",
    "Dockerfile",
    "setup.py",
    "setup.cfg",
    "tox.ini",
)


def collect_workspace_snapshot(
    workspace: str,
    task: str,
    *,
    max_inventory_files: int,
    max_sample_files: int,
    max_bytes_per_file: int,
    max_total_sample_bytes: int,
) -> WorkspaceSnapshot:
    _require_string(task, "task")
    _require_positive(max_inventory_files, "max_inventory_files")
    _require_positive(max_sample_files, "max_sample_files")
    _require_positive(max_bytes_per_file, "max_bytes_per_file")
    _require_positive(max_total_sample_bytes, "max_total_sample_bytes")

    root = Path(workspace).resolve()
    ignore_policy = load_ignore_policy(root)
    discovered_files: list[WorkspaceFile] = []

    for path in root.rglob("*"):
        is_instruction_file = path.name == "AGENTS.md"
        is_ignored = ignore_policy.is_ignored(path)
        is_visible_instruction = (
            is_instruction_file
            and not ignore_policy.is_ignored(path.parent)
        )
        if (
            not path.is_file()
            or (is_ignored and not is_visible_instruction)
            or ignore_policy.is_binary(path)
        ):
            continue

        relative = path.relative_to(root).as_posix()
        discovered_files.append(
            WorkspaceFile(path=relative, size=path.stat().st_size)
        )

    files_by_path = {file.path: file for file in discovered_files}
    ranked_files = [
        files_by_path[ranked.path]
        for ranked in rank_files(discovered_files, task)
    ]
    inventory = ranked_files[:max_inventory_files]
    total_file_count = len(ranked_files)
    omitted_file_count = total_file_count - len(inventory)

    selected_paths = _select_sample_files(
        ranked_files,
        task,
        max_sample_files=max_sample_files,
    )
    samples: list[WorkspaceSample] = []
    remaining_bytes = max_total_sample_bytes

    for relative_path in selected_paths:
        if remaining_bytes <= 0:
            break

        sample_budget = min(max_bytes_per_file, remaining_bytes)
        content = _read_text_slice(root / relative_path, sample_budget)
        if content is None:
            continue

        content_bytes = len(content.encode("utf-8"))
        samples.append(WorkspaceSample(path=relative_path, content=content))
        remaining_bytes -= content_bytes

    return WorkspaceSnapshot(
        root=str(root),
        files=inventory,
        samples=samples,
        total_file_count=total_file_count,
        omitted_file_count=omitted_file_count,
    )


def format_snapshot(snapshot: WorkspaceSnapshot) -> str:
    shown_file_count = len(snapshot.files)
    inventory_summary = (
        f"File inventory (showing {shown_file_count} of "
        f"{snapshot.total_file_count} files; "
        f"{snapshot.omitted_file_count} omitted):"
    )
    file_list = "\n".join(
        f"- {file.path} ({file.size} bytes)" for file in snapshot.files
    ) or "(empty workspace)"
    samples = "\n\n".join(
        f"### {sample.path}\n```\n{sample.content}\n```"
        for sample in snapshot.samples
    ) or "(no initial file contents selected)"

    return "\n".join(
        [
            f"Workspace root: {snapshot.root}",
            "",
            inventory_summary,
            file_list,
            "",
            "Initial file contents:",
            samples,
            "",
            (
                "Most source code is not loaded in the initial context. "
                "Use search_text to locate relevant symbols or text, then "
                "use read_file or read_many_files to inspect the target files."
            ),
        ]
    )


def _select_sample_files(
    ranked_files: list[WorkspaceFile],
    task: str,
    *,
    max_sample_files: int,
) -> list[str]:
    selected: list[str] = []
    selected_set: set[str] = set()

    def add(path: str) -> None:
        if path not in selected_set and len(selected) < max_sample_files:
            selected.append(path)
            selected_set.add(path)

    for file in ranked_files:
        if Path(file.path).name == "AGENTS.md":
            continue
        if task_mentions_file(task, file.path):
            add(file.path)

    root_paths_by_name = {
        file.path.casefold(): file.path
        for file in ranked_files
        if "/" not in file.path and Path(file.path).name != "AGENTS.md"
    }
    for metadata_path in SAMPLE_PRIORITY_EXACT:
        actual_path = root_paths_by_name.get(metadata_path.casefold())
        if actual_path is not None:
            add(actual_path)

    return selected


def _read_text_slice(path: Path, max_bytes: int) -> str | None:
    probe_size = max(max_bytes, 8_000)
    with path.open("rb") as stream:
        data = stream.read(probe_size)

    if b"\0" in data[:8_000]:
        return None

    content = data[:max_bytes].decode("utf-8", errors="replace")
    return _fit_utf8_budget(content, max_bytes)


def _fit_utf8_budget(content: str, max_bytes: int) -> str:
    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _require_positive(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")


def _require_string(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string.")
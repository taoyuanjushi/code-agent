from pathlib import Path


def resolve_inside_workspace(workspace: str | Path, requested_path: str) -> Path:
    workspace_path = Path(workspace).resolve()
    resolved = (workspace_path / requested_path).resolve()

    if resolved != workspace_path and workspace_path not in resolved.parents:
        raise ValueError(f"Path escapes workspace: {requested_path}")

    return resolved


def ensure_parent_directory(file_path: str | Path) -> None:
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)

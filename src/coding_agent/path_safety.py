from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath

from .security.models import PATH_OPERATIONS, PathOperation

_READ_LINK_OPERATIONS = frozenset({"read", "artifact_expand"})
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


def resolve_workspace_path(
    workspace: str | Path,
    requested_path: str | Path,
    *,
    operation: PathOperation,
    allow_missing: bool = False,
) -> Path:
    """Resolve one path while enforcing workspace, link, and Windows boundaries.

    Read-like operations may follow links only when the final real path remains
    inside the workspace. Mutating, execution, listing, search, and snapshot
    operations reject every symlink or Windows reparse point in an existing
    path component.
    """

    if operation not in PATH_OPERATIONS:
        raise ValueError(f"Unsupported path operation: {operation}")
    if not isinstance(allow_missing, bool):
        raise TypeError("allow_missing must be a boolean.")

    root = _resolve_workspace_root(workspace)
    raw_path = _coerce_requested_path(requested_path)
    _validate_windows_path_syntax(raw_path)

    normalized = raw_path.replace("\\", "/")
    if ".." in PurePosixPath(normalized).parts:
        raise ValueError(
            f"Path escapes workspace: parent traversal is not allowed: {raw_path}"
        )

    native = Path(normalized)
    candidate = native if native.is_absolute() else root / native
    candidate = Path(os.path.abspath(candidate))
    _require_contained(root, candidate, raw_path, boundary="lexical")

    relative = candidate.relative_to(root)
    _validate_existing_components(
        root,
        relative,
        raw_path=raw_path,
        operation=operation,
        allow_missing=allow_missing,
    )

    resolved = candidate.resolve(strict=False)
    _require_contained(root, resolved, raw_path, boundary="realpath")
    if not allow_missing and not resolved.exists():
        raise FileNotFoundError(f"Path does not exist: {raw_path}")

    return resolved


def resolve_inside_workspace(workspace: str | Path, requested_path: str | Path) -> Path:
    """Compatibility wrapper for callers that only need containment checks."""

    return resolve_workspace_path(
        workspace,
        requested_path,
        operation="read",
        allow_missing=True,
    )


def ensure_workspace_parent_directory(
    workspace: str | Path,
    requested_path: str | Path,
) -> Path:
    """Create a target's parents without accepting links or reparse points."""

    root = _resolve_workspace_root(workspace)
    target = resolve_workspace_path(
        root,
        requested_path,
        operation="write",
        allow_missing=True,
    )
    relative_parent = target.parent.relative_to(root)
    current = root

    for part in relative_parent.parts:
        current = current / part
        try:
            current.mkdir()
        except FileExistsError:
            pass
        validated = resolve_workspace_path(
            root,
            current,
            operation="write",
            allow_missing=False,
        )
        if not validated.is_dir():
            raise NotADirectoryError(f"Path component is not a directory: {current}")

    return resolve_workspace_path(
        root,
        requested_path,
        operation="write",
        allow_missing=True,
    )


def is_link_or_reparse_point(path: str | Path) -> bool:
    """Return whether an existing path is a symlink or Windows reparse point."""

    try:
        status = Path(path).lstat()
    except FileNotFoundError:
        return False
    return _stat_is_link_or_reparse(status)


def ensure_parent_directory(file_path: str | Path) -> None:
    """Legacy parent creation helper; prefer ensure_workspace_parent_directory."""

    Path(file_path).parent.mkdir(parents=True, exist_ok=True)


def _resolve_workspace_root(workspace: str | Path) -> Path:
    if not isinstance(workspace, (str, Path)):
        raise TypeError("workspace must be a string or Path.")
    raw_workspace = os.fspath(workspace)
    if not raw_workspace or "\x00" in raw_workspace:
        raise ValueError("workspace must be a non-empty path without NUL characters.")

    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Workspace is not an existing directory: {root}")
    return root


def _coerce_requested_path(requested_path: str | Path) -> str:
    if not isinstance(requested_path, (str, Path)):
        raise TypeError("requested_path must be a string or Path.")
    raw_path = os.fspath(requested_path)
    if not raw_path:
        raise ValueError("requested_path must not be empty.")
    if "\x00" in raw_path:
        raise ValueError("requested_path must not contain NUL characters.")
    return raw_path


def _validate_windows_path_syntax(raw_path: str) -> None:
    windows_text = raw_path.replace("/", "\\")
    lowered = windows_text.casefold()
    if lowered.startswith(("\\\\?\\", "\\\\.\\", "\\??\\")):
        raise ValueError(f"Windows device paths are not allowed: {raw_path}")
    if windows_text.startswith("\\\\"):
        raise ValueError(f"UNC paths are not allowed: {raw_path}")

    windows_path = PureWindowsPath(raw_path)
    if windows_path.drive:
        if windows_path.drive.startswith("\\\\"):
            raise ValueError(f"UNC paths are not allowed: {raw_path}")
        if not windows_path.root:
            raise ValueError(f"Drive-relative paths are not allowed: {raw_path}")
        if os.name != "nt":
            raise ValueError(
                f"Windows absolute paths are not valid on this platform: {raw_path}"
            )
    elif windows_path.root and (os.name == "nt" or raw_path.startswith("\\")):
        raise ValueError(f"Drive-rooted paths are not allowed: {raw_path}")

    ads_candidate = windows_text
    if len(ads_candidate) >= 3 and ads_candidate[1] == ":" and ads_candidate[2] == "\\":
        ads_candidate = ads_candidate[2:]
    if ":" in ads_candidate:
        raise ValueError(
            f"Windows alternate data streams are not allowed: {raw_path}"
        )


def _validate_existing_components(
    root: Path,
    relative: Path,
    *,
    raw_path: str,
    operation: PathOperation,
    allow_missing: bool,
) -> None:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            status = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise FileNotFoundError(f"Path does not exist: {raw_path}") from None

        if _stat_is_link_or_reparse(status) and operation not in _READ_LINK_OPERATIONS:
            raise ValueError(
                "Path contains a symlink or reparse point that is not allowed "
                f"for {operation}: {raw_path}"
            )


def _stat_is_link_or_reparse(status: os.stat_result) -> bool:
    if stat.S_ISLNK(status.st_mode):
        return True
    attributes = getattr(status, "st_file_attributes", 0)
    return bool(attributes & _WINDOWS_REPARSE_POINT)


def _require_contained(
    root: Path,
    candidate: Path,
    raw_path: str,
    *,
    boundary: str,
) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"Path escapes workspace ({boundary} boundary): {raw_path}"
        ) from exc

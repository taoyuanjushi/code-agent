from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..path_safety import resolve_inside_workspace
from .models import AgentSessionState, SessionStarted
from .recovery import FileHashObservation, ToolRecoveryPlan


class WorkspaceGuardError(RuntimeError):
    """Base error raised when a persisted session no longer matches its workspace."""


class WorkspaceMismatchError(WorkspaceGuardError):
    """Raised when a session is resumed from a different canonical workspace."""


class GitHeadMismatchError(WorkspaceGuardError):
    """Raised when the repository HEAD changed since the session started."""


@dataclass(frozen=True)
class TouchedFileHashMismatch:
    path: str
    expected_sha256: str | None
    current_sha256: str | None


class TouchedFileDriftError(WorkspaceGuardError):
    """Raised when a session-owned file changed outside an audited recovery."""

    def __init__(self, mismatches: tuple[TouchedFileHashMismatch, ...]) -> None:
        self.mismatches = mismatches
        details = "; ".join(
            f"{item.path}: expected={item.expected_sha256}, "
            f"current={item.current_sha256}"
            for item in mismatches
        )
        super().__init__(
            "Touched-file drift prevents session resume"
            + (f": {details}" if details else ".")
        )


@dataclass(frozen=True)
class WorkspaceGuardResult:
    workspace: Path
    git_head: str | None
    checked_file_count: int


def validate_workspace_guard(
    workspace: str | Path,
    started: SessionStarted,
    state: AgentSessionState,
    *,
    recovery_plans: Sequence[ToolRecoveryPlan] = (),
) -> WorkspaceGuardResult:
    """Validate workspace identity, Git HEAD, and durable touched-file hashes."""

    actual = Path(workspace).resolve()
    if not actual.is_dir():
        raise WorkspaceMismatchError(
            f"Resume workspace is not an existing directory: {actual}"
        )

    expected = Path(started.workspace).resolve()
    if _canonical_path(actual) != _canonical_path(expected):
        raise WorkspaceMismatchError(
            "Session workspace does not match the requested workspace: "
            f"expected={expected}, actual={actual}."
        )

    for plan in recovery_plans:
        plan.raise_for_workspace_drift()

    current_head = discover_git_head(actual)
    if started.git_head is not None and current_head != started.git_head:
        raise GitHeadMismatchError(
            "Git HEAD changed since the session started: "
            f"expected={started.git_head}, current={current_head}."
        )

    recovery_observations = _recovery_observations(recovery_plans)
    mismatches: list[TouchedFileHashMismatch] = []
    for relative_path, expected_hash in state.touched_file_hashes.items():
        if expected_hash is not None and not isinstance(expected_hash, str):
            raise WorkspaceGuardError(
                f"Persisted hash for {relative_path!r} is not a string or null."
            )
        absolute = resolve_inside_workspace(actual, relative_path)
        current_hash = hash_file_or_none(absolute)
        if current_hash == expected_hash:
            continue
        if _is_explained_patch_transition(
            relative_path,
            expected_hash,
            current_hash,
            recovery_observations,
        ):
            continue
        mismatches.append(
            TouchedFileHashMismatch(
                path=relative_path,
                expected_sha256=expected_hash,
                current_sha256=current_hash,
            )
        )

    if mismatches:
        raise TouchedFileDriftError(tuple(mismatches))

    return WorkspaceGuardResult(
        workspace=actual,
        git_head=current_head,
        checked_file_count=len(state.touched_file_hashes),
    )


def discover_git_head(workspace: str | Path) -> str | None:
    """Return the verified repository HEAD, or ``None`` outside a Git repository."""

    git = shutil.which("git")
    if git is None:
        return None
    try:
        completed = subprocess.run(
            [git, "rev-parse", "--verify", "HEAD"],
            cwd=Path(workspace).resolve(),
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def hash_file_or_none(path: Path) -> str | None:
    """Hash one regular file, returning ``None`` for an absent path."""

    if not path.exists():
        return None
    if not path.is_file():
        raise WorkspaceGuardError(
            f"Expected a regular file while checking workspace guard: {path}"
        )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_path(path: Path) -> str:
    return os.path.normcase(str(path))


def _recovery_observations(
    plans: Sequence[ToolRecoveryPlan],
) -> dict[str, tuple[FileHashObservation, ...]]:
    observations: dict[str, list[FileHashObservation]] = {}
    for plan in plans:
        for item in plan.file_hashes:
            observations.setdefault(item.path, []).append(item)
    return {path: tuple(items) for path, items in observations.items()}


def _is_explained_patch_transition(
    path: str,
    expected: object,
    current: str | None,
    observations: dict[str, tuple[FileHashObservation, ...]],
) -> bool:
    return any(
        item.match == "after"
        and item.before_sha256 == expected
        and item.after_sha256 == current
        for item in observations.get(path, ())
    )
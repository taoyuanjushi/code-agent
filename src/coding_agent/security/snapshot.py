from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import BinaryIO

from ..ignore import IgnorePolicy, load_ignore_policy
from ..path_safety import (
    ensure_workspace_parent_directory,
    is_link_or_reparse_point,
    resolve_workspace_path,
)
from .models import SECURITY_SCHEMA_VERSION
from .path_policy import SensitivePathPolicy, load_sensitive_path_policy

DEFAULT_SNAPSHOT_MAX_FILES = 10_000
DEFAULT_SNAPSHOT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_SNAPSHOT_MAX_BINARY_FILE_BYTES = 16 * 1024 * 1024
SNAPSHOT_MANIFEST_VERSION = 1
SNAPSHOT_EXCLUSION_REASONS = frozenset(
    {
        "ignored",
        "large_binary",
        "non_regular",
        "sensitive",
        "symlink",
    }
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_COPY_CHUNK_BYTES = 1024 * 1024


class SandboxSnapshotError(RuntimeError):
    """Base error for sandbox workspace snapshot operations."""


class SnapshotBudgetExceededError(SandboxSnapshotError):
    """Raised before copying when the eligible workspace exceeds its budget."""


class SnapshotSourceChangedError(SandboxSnapshotError):
    """Raised when the workspace changes while a snapshot is being built."""


class SnapshotAlreadyExistsError(SandboxSnapshotError):
    """Raised when a session/call snapshot destination already exists."""


@dataclass(frozen=True)
class SnapshotFileEntry:
    """One deterministic file record in a sandbox snapshot manifest."""

    path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        _validate_relative_posix_path(self.path, "snapshot file path")
        _validate_non_negative_int(self.size, "snapshot file size")
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise ValueError(
                "snapshot file sha256 must be a lowercase sha256:<64 hex> digest."
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class SnapshotManifest:
    """Canonical, content-addressed description of a filtered workspace."""

    files: tuple[SnapshotFileEntry, ...]
    total_bytes: int
    manifest_version: int = SNAPSHOT_MANIFEST_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.files, tuple):
            raise TypeError("manifest files must be a tuple.")
        if any(not isinstance(entry, SnapshotFileEntry) for entry in self.files):
            raise TypeError("manifest files must contain SnapshotFileEntry values.")
        paths = tuple(entry.path for entry in self.files)
        if paths != tuple(sorted(paths)):
            raise ValueError("manifest files must be sorted by path.")
        if len(paths) != len(set(paths)):
            raise ValueError("manifest file paths must be unique.")
        _validate_non_negative_int(self.total_bytes, "manifest total_bytes")
        if self.total_bytes != sum(entry.size for entry in self.files):
            raise ValueError("manifest total_bytes must equal the sum of file sizes.")
        if self.manifest_version != SNAPSHOT_MANIFEST_VERSION:
            raise ValueError(
                f"Unsupported snapshot manifest version: {self.manifest_version}"
            )

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def sha256(self) -> str:
        return _sha256_digest(self.canonical_bytes())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SECURITY_SCHEMA_VERSION,
            "manifest_version": self.manifest_version,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "files": [entry.to_dict() for entry in self.files],
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")


@dataclass(frozen=True)
class SandboxWorkspaceSnapshot:
    """Published sandbox snapshot and its non-sensitive audit summary."""

    source_workspace: Path
    session_id: str
    call_id: str
    call_directory: Path
    workspace_directory: Path
    manifest_path: Path
    manifest: SnapshotManifest
    excluded_counts: Mapping[str, int]

    def __post_init__(self) -> None:
        source_workspace = Path(self.source_workspace).resolve(strict=True)
        if not source_workspace.is_dir():
            raise ValueError("source_workspace must be an existing directory.")
        _validate_identifier(self.session_id, "session_id")
        _validate_identifier(self.call_id, "call_id")
        if not isinstance(self.manifest, SnapshotManifest):
            raise TypeError("manifest must be a SnapshotManifest instance.")

        expected_call = source_workspace.joinpath(
            ".coding-agent",
            "sandboxes",
            self.session_id,
            self.call_id,
        )
        call_directory = Path(self.call_directory)
        workspace_directory = Path(self.workspace_directory)
        manifest_path = Path(self.manifest_path)
        if call_directory != expected_call:
            raise ValueError("call_directory does not match the snapshot identifiers.")
        if workspace_directory != call_directory / "workspace":
            raise ValueError("workspace_directory must be inside call_directory.")
        if manifest_path != call_directory / "manifest.json":
            raise ValueError("manifest_path must be inside call_directory.")

        counts = _freeze_excluded_counts(self.excluded_counts)
        object.__setattr__(self, "source_workspace", source_workspace)
        object.__setattr__(self, "call_directory", call_directory)
        object.__setattr__(self, "workspace_directory", workspace_directory)
        object.__setattr__(self, "manifest_path", manifest_path)
        object.__setattr__(self, "excluded_counts", counts)

    @property
    def manifest_sha256(self) -> str:
        return self.manifest.sha256

    def audit_summary(self) -> dict[str, object]:
        """Return the snapshot facts safe to persist in the session journal."""

        return {
            "schema_version": SECURITY_SCHEMA_VERSION,
            "manifest_sha256": self.manifest_sha256,
            "file_count": self.manifest.file_count,
            "total_bytes": self.manifest.total_bytes,
            "excluded_counts": dict(self.excluded_counts),
        }


@dataclass(frozen=True)
class SnapshotCleanupResult:
    """Non-throwing cleanup result for sandbox execution audit."""

    removed: bool
    cleanup_error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.removed, bool):
            raise TypeError("removed must be a boolean.")
        if self.cleanup_error is not None and not isinstance(
            self.cleanup_error,
            str,
        ):
            raise TypeError("cleanup_error must be a string or null.")
        if self.removed and self.cleanup_error is not None:
            raise ValueError("successful cleanup cannot include cleanup_error.")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SECURITY_SCHEMA_VERSION,
            "removed": self.removed,
            "cleanup_error": self.cleanup_error,
        }


@dataclass(frozen=True)
class _FileIdentity:
    size: int
    mode: int
    mtime_ns: int
    device: int
    inode: int


@dataclass(frozen=True)
class _SourceFile:
    path: Path
    relative_path: str
    identity: _FileIdentity


def create_sandbox_workspace_snapshot(
    workspace: str | Path,
    *,
    session_id: str,
    call_id: str,
    max_files: int = DEFAULT_SNAPSHOT_MAX_FILES,
    max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
    max_binary_file_bytes: int = DEFAULT_SNAPSHOT_MAX_BINARY_FILE_BYTES,
    ignore_policy: IgnorePolicy | None = None,
    sensitive_policy: SensitivePathPolicy | None = None,
) -> SandboxWorkspaceSnapshot:
    """Create and atomically publish a filtered, immutable-input snapshot.

    Eligible files are fully enumerated, budgeted, and hashed before a staging
    directory is created. The final session/call directory appears only after
    every file has been copied and revalidated.
    """

    _validate_identifier(session_id, "session_id")
    _validate_identifier(call_id, "call_id")
    _validate_positive_int(max_files, "max_files")
    _validate_positive_int(max_bytes, "max_bytes")
    _validate_positive_int(max_binary_file_bytes, "max_binary_file_bytes")

    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Workspace is not an existing directory: {root}")

    effective_ignore_policy = ignore_policy or load_ignore_policy(root)
    effective_sensitive_policy = sensitive_policy or load_sensitive_path_policy(root)
    _validate_policy_roots(
        root,
        effective_ignore_policy,
        effective_sensitive_policy,
    )

    source_files, excluded_counts = _collect_source_files(
        root,
        ignore_policy=effective_ignore_policy,
        sensitive_policy=effective_sensitive_policy,
        max_files=max_files,
        max_bytes=max_bytes,
        max_binary_file_bytes=max_binary_file_bytes,
    )
    manifest = _build_manifest(root, source_files)

    relative_call_directory = (
        f".coding-agent/sandboxes/{session_id}/{call_id}"
    )
    call_directory = resolve_workspace_path(
        root,
        relative_call_directory,
        operation="write",
        allow_missing=True,
    )
    if _path_lexists(call_directory):
        raise SnapshotAlreadyExistsError(
            f"Sandbox snapshot already exists for {session_id}/{call_id}."
        )

    ensure_workspace_parent_directory(root, relative_call_directory)
    session_directory = resolve_workspace_path(
        root,
        f".coding-agent/sandboxes/{session_id}",
        operation="write",
        allow_missing=False,
    )
    staging_directory = Path(
        tempfile.mkdtemp(
            prefix=f".{call_id}.",
            suffix=".tmp",
            dir=session_directory,
        )
    )
    staging_directory = resolve_workspace_path(
        root,
        staging_directory,
        operation="write",
        allow_missing=False,
    )

    try:
        staging_workspace = staging_directory / "workspace"
        staging_workspace.mkdir()
        for source, entry in zip(source_files, manifest.files, strict=True):
            destination = staging_workspace.joinpath(*PurePosixPath(entry.path).parts)
            _copy_source_file(root, source, entry, destination)

        current_files, _current_excluded = _collect_source_files(
            root,
            ignore_policy=effective_ignore_policy,
            sensitive_policy=effective_sensitive_policy,
            max_files=max_files,
            max_bytes=max_bytes,
            max_binary_file_bytes=max_binary_file_bytes,
        )
        _require_unchanged_inventory(source_files, current_files)

        manifest_bytes = manifest.canonical_bytes()
        manifest_path = staging_directory / "manifest.json"
        _write_bytes_durably(manifest_path, manifest_bytes)

        if _path_lexists(call_directory):
            raise SnapshotAlreadyExistsError(
                f"Sandbox snapshot already exists for {session_id}/{call_id}."
            )
        os.rename(staging_directory, call_directory)
    except BaseException:
        if _path_lexists(staging_directory):
            try:
                _remove_tree(staging_directory)
            except OSError:
                pass
        raise

    published_call = resolve_workspace_path(
        root,
        relative_call_directory,
        operation="write",
        allow_missing=False,
    )
    published_workspace = resolve_workspace_path(
        root,
        f"{relative_call_directory}/workspace",
        operation="write",
        allow_missing=False,
    )
    published_manifest = resolve_workspace_path(
        root,
        f"{relative_call_directory}/manifest.json",
        operation="write",
        allow_missing=False,
    )
    stored_manifest = published_manifest.read_bytes()
    if stored_manifest != manifest.canonical_bytes():
        cleanup = _cleanup_call_directory(published_call)
        detail = cleanup.cleanup_error or "published manifest content changed"
        raise SandboxSnapshotError(
            f"Published snapshot manifest verification failed: {detail}"
        )

    return SandboxWorkspaceSnapshot(
        source_workspace=root,
        session_id=session_id,
        call_id=call_id,
        call_directory=published_call,
        workspace_directory=published_workspace,
        manifest_path=published_manifest,
        manifest=manifest,
        excluded_counts=excluded_counts,
    )


def cleanup_sandbox_workspace_snapshot(
    snapshot: SandboxWorkspaceSnapshot,
) -> SnapshotCleanupResult:
    """Remove one published snapshot without following replaced links."""

    if not isinstance(snapshot, SandboxWorkspaceSnapshot):
        raise TypeError("snapshot must be a SandboxWorkspaceSnapshot instance.")

    relative_call_directory = (
        f".coding-agent/sandboxes/{snapshot.session_id}/{snapshot.call_id}"
    )
    try:
        expected = resolve_workspace_path(
            snapshot.source_workspace,
            relative_call_directory,
            operation="write",
            allow_missing=True,
        )
    except (OSError, ValueError) as exc:
        return SnapshotCleanupResult(removed=False, cleanup_error=str(exc))
    if expected != snapshot.call_directory:
        return SnapshotCleanupResult(
            removed=False,
            cleanup_error="Snapshot cleanup path no longer matches its identifiers.",
        )
    return _cleanup_call_directory(expected)


def _collect_source_files(
    root: Path,
    *,
    ignore_policy: IgnorePolicy,
    sensitive_policy: SensitivePathPolicy,
    max_files: int,
    max_bytes: int,
    max_binary_file_bytes: int,
) -> tuple[tuple[_SourceFile, ...], Mapping[str, int]]:
    files: list[_SourceFile] = []
    excluded: Counter[str] = Counter()
    total_bytes = 0

    def walk_error(error: OSError) -> None:
        raise SandboxSnapshotError(f"Could not enumerate workspace: {error}")

    for current_directory, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=walk_error,
    ):
        directory = Path(current_directory)
        try:
            directory = resolve_workspace_path(
                root,
                directory,
                operation="snapshot",
                allow_missing=False,
            )
        except (OSError, ValueError) as exc:
            raise SnapshotSourceChangedError(
                f"Workspace directory changed during snapshot: {directory}"
            ) from exc

        retained_directories: list[str] = []
        for name in sorted(directory_names):
            candidate = directory / name
            reason = _directory_exclusion_reason(
                candidate,
                ignore_policy=ignore_policy,
                sensitive_policy=sensitive_policy,
            )
            if reason is not None:
                excluded[reason] += 1
                continue
            try:
                resolved = resolve_workspace_path(
                    root,
                    candidate,
                    operation="snapshot",
                    allow_missing=False,
                )
            except (OSError, ValueError) as exc:
                raise SnapshotSourceChangedError(
                    "Workspace directory changed during snapshot: "
                    f"{candidate.relative_to(root).as_posix()}"
                ) from exc
            if not resolved.is_dir():
                excluded["non_regular"] += 1
                continue
            retained_directories.append(name)
        directory_names[:] = retained_directories

        for name in sorted(file_names):
            candidate = directory / name
            reason = _file_exclusion_reason(
                candidate,
                ignore_policy=ignore_policy,
                sensitive_policy=sensitive_policy,
            )
            if reason is not None:
                excluded[reason] += 1
                continue

            try:
                resolved = resolve_workspace_path(
                    root,
                    candidate,
                    operation="snapshot",
                    allow_missing=False,
                )
                status = resolved.stat(follow_symlinks=False)
            except (OSError, ValueError) as exc:
                raise SnapshotSourceChangedError(
                    "Workspace file changed during snapshot: "
                    f"{candidate.relative_to(root).as_posix()}"
                ) from exc
            if not stat.S_ISREG(status.st_mode):
                excluded["non_regular"] += 1
                continue
            if (
                ignore_policy.is_binary(resolved)
                and status.st_size > max_binary_file_bytes
            ):
                excluded["large_binary"] += 1
                continue

            relative_path = candidate.relative_to(root).as_posix()
            files.append(
                _SourceFile(
                    path=resolved,
                    relative_path=relative_path,
                    identity=_identity_from_stat(status),
                )
            )
            total_bytes += status.st_size
            if len(files) > max_files:
                raise SnapshotBudgetExceededError(
                    "Sandbox snapshot exceeds the file-count budget: "
                    f"{len(files)} > {max_files}."
                )
            if total_bytes > max_bytes:
                raise SnapshotBudgetExceededError(
                    "Sandbox snapshot exceeds the byte budget: "
                    f"{total_bytes} > {max_bytes}."
                )

    files.sort(key=lambda item: item.relative_path)
    return tuple(files), _freeze_excluded_counts(excluded)


def _directory_exclusion_reason(
    path: Path,
    *,
    ignore_policy: IgnorePolicy,
    sensitive_policy: SensitivePathPolicy,
) -> str | None:
    if is_link_or_reparse_point(path):
        return "symlink"
    if not sensitive_policy.evaluate(path, operation="snapshot").allowed:
        return "sensitive"
    if ignore_policy.is_ignored(path):
        return "ignored"
    return None


def _file_exclusion_reason(
    path: Path,
    *,
    ignore_policy: IgnorePolicy,
    sensitive_policy: SensitivePathPolicy,
) -> str | None:
    if is_link_or_reparse_point(path):
        return "symlink"
    if not sensitive_policy.evaluate(path, operation="snapshot").allowed:
        return "sensitive"
    if ignore_policy.is_ignored(path):
        return "ignored"
    return None


def _build_manifest(
    root: Path,
    source_files: tuple[_SourceFile, ...],
) -> SnapshotManifest:
    entries = tuple(
        SnapshotFileEntry(
            path=source.relative_path,
            size=source.identity.size,
            sha256=_hash_source_file(root, source),
        )
        for source in source_files
    )
    return SnapshotManifest(
        files=entries,
        total_bytes=sum(entry.size for entry in entries),
    )


def _hash_source_file(root: Path, source: _SourceFile) -> str:
    stream = _open_verified_source(root, source)
    digest = hashlib.sha256()
    try:
        while True:
            chunk = stream.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        _require_matching_identity(source, os.fstat(stream.fileno()))
    finally:
        stream.close()
    return f"sha256:{digest.hexdigest()}"


def _copy_source_file(
    root: Path,
    source: _SourceFile,
    entry: SnapshotFileEntry,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    stream = _open_verified_source(root, source)
    digest = hashlib.sha256()
    copied_bytes = 0
    try:
        with destination.open("xb") as output:
            while True:
                chunk = stream.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                copied_bytes += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        _require_matching_identity(source, os.fstat(stream.fileno()))
    finally:
        stream.close()

    copied_digest = f"sha256:{digest.hexdigest()}"
    if copied_bytes != entry.size or copied_digest != entry.sha256:
        raise SnapshotSourceChangedError(
            f"Workspace file changed while copying: {entry.path}"
        )

    source_mode = stat.S_IMODE(source.identity.mode)
    safe_mode = (source_mode & 0o777) | stat.S_IRUSR | stat.S_IWUSR
    os.chmod(destination, safe_mode)


def _open_verified_source(root: Path, source: _SourceFile) -> BinaryIO:
    try:
        resolved = resolve_workspace_path(
            root,
            source.path,
            operation="snapshot",
            allow_missing=False,
        )
    except (OSError, ValueError) as exc:
        raise SnapshotSourceChangedError(
            f"Workspace file changed before opening: {source.relative_path}"
        ) from exc
    if resolved != source.path:
        raise SnapshotSourceChangedError(
            f"Workspace file target changed: {source.relative_path}"
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise SnapshotSourceChangedError(
            f"Workspace file could not be opened safely: {source.relative_path}"
        ) from exc

    try:
        status = os.fstat(descriptor)
        _require_matching_identity(source, status)
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _require_unchanged_inventory(
    expected: tuple[_SourceFile, ...],
    current: tuple[_SourceFile, ...],
) -> None:
    expected_by_path = {item.relative_path: item.identity for item in expected}
    current_by_path = {item.relative_path: item.identity for item in current}
    if expected_by_path.keys() != current_by_path.keys():
        added = sorted(current_by_path.keys() - expected_by_path.keys())
        removed = sorted(expected_by_path.keys() - current_by_path.keys())
        details: list[str] = []
        if added:
            details.append("added=" + ", ".join(added[:3]))
        if removed:
            details.append("removed=" + ", ".join(removed[:3]))
        raise SnapshotSourceChangedError(
            "Workspace inventory changed while copying snapshot"
            + (": " + "; ".join(details) if details else ".")
        )

    for path, expected_identity in expected_by_path.items():
        if not _identities_match(expected_identity, current_by_path[path]):
            raise SnapshotSourceChangedError(
                f"Workspace file changed after copying: {path}"
            )


def _require_matching_identity(source: _SourceFile, status: os.stat_result) -> None:
    current = _identity_from_stat(status)
    if not stat.S_ISREG(status.st_mode) or not _identities_match(
        source.identity,
        current,
    ):
        raise SnapshotSourceChangedError(
            f"Workspace file changed during snapshot: {source.relative_path}"
        )


def _identity_from_stat(status: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        size=status.st_size,
        mode=status.st_mode,
        mtime_ns=status.st_mtime_ns,
        device=status.st_dev,
        inode=status.st_ino,
    )


def _identities_match(first: _FileIdentity, second: _FileIdentity) -> bool:
    if (
        first.size != second.size
        or stat.S_IFMT(first.mode) != stat.S_IFMT(second.mode)
        or first.mtime_ns != second.mtime_ns
    ):
        return False
    if first.device and second.device and first.device != second.device:
        return False
    if first.inode and second.inode and first.inode != second.inode:
        return False
    return True


def _write_bytes_durably(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _cleanup_call_directory(path: Path) -> SnapshotCleanupResult:
    if not _path_lexists(path):
        return SnapshotCleanupResult(removed=True)
    if is_link_or_reparse_point(path):
        return SnapshotCleanupResult(
            removed=False,
            cleanup_error="Refusing to clean a replaced snapshot link or reparse point.",
        )
    try:
        _remove_tree(path)
    except OSError as exc:
        return SnapshotCleanupResult(removed=False, cleanup_error=str(exc))
    return SnapshotCleanupResult(removed=not _path_lexists(path))


def _remove_tree(path: Path) -> None:
    def make_writable_and_retry(function, name, error) -> None:
        del error
        os.chmod(name, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        function(name)

    shutil.rmtree(path, onexc=make_writable_and_retry)


def _validate_policy_roots(
    root: Path,
    ignore_policy: IgnorePolicy,
    sensitive_policy: SensitivePathPolicy,
) -> None:
    if not isinstance(ignore_policy, IgnorePolicy):
        raise TypeError("ignore_policy must be an IgnorePolicy instance.")
    if not isinstance(sensitive_policy, SensitivePathPolicy):
        raise TypeError("sensitive_policy must be a SensitivePathPolicy instance.")
    if ignore_policy.root.resolve() != root:
        raise ValueError("ignore_policy root must match the snapshot workspace.")
    if sensitive_policy.root.resolve() != root:
        raise ValueError("sensitive_policy root must match the snapshot workspace.")


def _freeze_excluded_counts(values: Mapping[str, int]) -> Mapping[str, int]:
    if not isinstance(values, Mapping):
        raise TypeError("excluded_counts must be a mapping.")
    normalized: dict[str, int] = {}
    for reason, count in values.items():
        if reason not in SNAPSHOT_EXCLUSION_REASONS:
            raise ValueError(f"Unsupported snapshot exclusion reason: {reason}")
        _validate_non_negative_int(count, f"excluded count for {reason}")
        if count:
            normalized[reason] = count
    return MappingProxyType(dict(sorted(normalized.items())))


def _validate_identifier(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string.")
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"{label} must be 1-128 filesystem-safe letters, digits, '.', '_' or '-'."
        )


def _validate_relative_posix_path(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string.")
    if not value or "\\" in value or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty canonical POSIX path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must stay inside the snapshot workspace.")
    if path.as_posix() != value:
        raise ValueError(f"{label} must be a canonical POSIX path.")


def _validate_positive_int(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")


def _validate_non_negative_int(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer.")


def _sha256_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(path)

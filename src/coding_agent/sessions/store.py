from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from ..path_safety import (
    ensure_workspace_parent_directory,
    resolve_workspace_path,
)
from .codec import (
    SessionCodecError,
    artifact_ref_to_dict,
    create_session_event,
    decode_event,
    encode_event,
    verify_event_chain,
)
from .models import (
    ArtifactRef,
    SessionEvent,
    SessionEventType,
    SessionStatus,
)
from .privacy import SessionPrivacyPolicy

SESSION_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8,}$")
_SESSION_ID_ATTEMPTS = 100
_TERMINAL_STATUSES: dict[SessionEventType, SessionStatus] = {
    "session.completed": "completed",
    "session.failed": "failed",
    "session.interrupted": "interrupted",
}
_PROCESS_LOCK_GUARD = threading.Lock()
_PROCESS_LOCKS: set[Path] = set()


class SessionStoreError(RuntimeError):
    """Base error for safe session persistence operations."""


class SessionNotFoundError(SessionStoreError):
    """Raised when a requested session log does not exist."""


class ConcurrentSessionWriteError(SessionStoreError):
    """Raised when another process or thread owns the session writer lock."""


class ArtifactNotFoundError(SessionStoreError):
    """Raised when a referenced artifact file is missing."""


class ArtifactIntegrityError(SessionStoreError):
    """Raised when content-addressed artifact bytes fail validation."""


class ArtifactTooLargeError(SessionStoreError):
    """Raised when a direct artifact write exceeds the configured limit."""


class ReadOnlySessionStoreError(SessionStoreError):
    """Raised when a mutating operation is attempted through a read-only store."""


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    task: str | None
    status: SessionStatus
    event_count: int
    started_at: str
    updated_at: str
    last_event_type: SessionEventType


class SessionStore:
    """Append-only JSONL storage with an optional strict read-only mode."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        privacy_policy: SessionPrivacyPolicy | None = None,
        read_only: bool = False,
    ) -> None:
        workspace_path = resolve_workspace_path(
            workspace,
            ".",
            operation="write",
            allow_missing=False,
        )

        if privacy_policy is not None and not isinstance(
            privacy_policy, SessionPrivacyPolicy
        ):
            raise TypeError("privacy_policy must be a SessionPrivacyPolicy.")
        if not isinstance(read_only, bool):
            raise TypeError("read_only must be a boolean.")

        self.workspace = workspace_path
        self.privacy_policy = privacy_policy or SessionPrivacyPolicy()
        self.read_only = read_only
        self._exclusive_writer_owners: dict[str, int] = {}
        self.root = self._resolve_store_path(".", allow_missing=True)
        self.sessions_dir = self._resolve_store_path("sessions", allow_missing=True)
        self.artifacts_dir = self._resolve_store_path("artifacts", allow_missing=True)
        self.locks_dir = self._resolve_store_path("locks", allow_missing=True)
        if not self.read_only:
            for relative_directory in (".", "sessions", "artifacts", "locks"):
                directory = self._resolve_store_path(
                    relative_directory,
                    allow_missing=True,
                )
                directory.mkdir(exist_ok=True)
                self._resolve_store_path(
                    relative_directory,
                    allow_missing=False,
                )

    def create(self, started_payload: Mapping[str, object]) -> str:
        """Create a new durable session containing one ``session.started`` event."""

        if not isinstance(started_payload, Mapping):
            raise TypeError("started_payload must be a mapping.")
        self._require_writable("create sessions")

        for _ in range(_SESSION_ID_ATTEMPTS):
            session_id = _new_session_id()
            _validate_session_id(session_id)
            event_path = self._session_path(session_id)
            if event_path.exists():
                continue

            with self._session_lock(session_id):
                if event_path.exists():
                    continue
                sanitized_payload = self._sanitize_payload_unlocked(
                    session_id,
                    started_payload,
                )
                event = self._new_event(
                    session_id=session_id,
                    seq=1,
                    event_type="session.started",
                    prev_hash=None,
                    payload=sanitized_payload,
                )
                self._write_new_event_file(event_path, event)
                _fsync_directory(self.sessions_dir)
                return session_id

        raise SessionStoreError("Could not allocate a unique session ID.")

    def append(
        self,
        session_id: str,
        event_type: SessionEventType,
        payload: Mapping[str, object],
    ) -> SessionEvent:
        """Validate the complete log and durably append one event."""

        _validate_session_id(session_id)
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping.")
        self._require_writable("append session events")

        self._require_session_exists(session_id)
        with self._session_lock(session_id):
            events = self._load_unlocked(session_id, repair_tail=False)
            previous = events[-1]
            sanitized_payload = self._sanitize_payload_unlocked(
                session_id,
                payload,
            )
            event = self._new_event(
                session_id=session_id,
                seq=previous.seq + 1,
                event_type=event_type,
                prev_hash=previous.event_hash,
                payload=sanitized_payload,
            )
            self._append_event_unlocked(self._session_path(session_id), event)
            return event

    def load(
        self,
        session_id: str,
        *,
        repair_tail: bool = False,
    ) -> tuple[SessionEvent, ...]:
        """Load and verify a session, optionally repairing one uncommitted tail."""

        _validate_session_id(session_id)
        if not isinstance(repair_tail, bool):
            raise TypeError("repair_tail must be a boolean.")
        if self.read_only:
            if repair_tail:
                raise ReadOnlySessionStoreError(
                    "read-only SessionStore cannot repair an incomplete tail."
                )
            self._require_session_exists(session_id)
            return self._load_unlocked(session_id, repair_tail=False)
        self._require_session_exists(session_id)
        with self._session_lock(session_id):
            return self._load_unlocked(session_id, repair_tail=repair_tail)

    def list_sessions(self) -> tuple[SessionSummary, ...]:
        """Return validated session summaries ordered by descending session ID."""

        session_ids = sorted(
            (
                path.stem
                for path in self.sessions_dir.glob("*.jsonl")
                if SESSION_ID_PATTERN.fullmatch(path.stem)
            ),
            reverse=True,
        )
        summaries: list[SessionSummary] = []
        for session_id in session_ids:
            events = self.load(session_id)
            first = events[0]
            last = events[-1]
            task_value = first.payload.get("task")
            task = task_value if isinstance(task_value, str) else None
            summaries.append(
                SessionSummary(
                    session_id=session_id,
                    task=task,
                    status=_TERMINAL_STATUSES.get(last.type, "running"),
                    event_count=len(events),
                    started_at=first.recorded_at,
                    updated_at=last.recorded_at,
                    last_event_type=last.type,
                )
            )
        return tuple(summaries)

    def put_artifact(
        self,
        session_id: str,
        content: bytes,
        media_type: str,
        *,
        encoding: str | None = None,
    ) -> ArtifactRef:
        """Privacy-filter and atomically persist content-addressed bytes."""

        _validate_session_id(session_id)
        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes.")
        _validate_media_type(media_type)
        _validate_encoding(encoding)
        self._require_writable("write session artifacts")
        if len(content) > self.privacy_policy.artifact_max_bytes:
            raise ArtifactTooLargeError(
                "Artifact exceeds the configured artifact_max_bytes limit."
            )

        self._require_session_exists(session_id)
        with self._session_lock(session_id):
            self._require_session_exists(session_id)
            return self._put_artifact_unlocked(
                session_id,
                content,
                media_type,
                encoding,
            )

    def get_artifact(self, session_id: str, ref: ArtifactRef) -> bytes:
        """Read an artifact only after path, size, and SHA-256 validation."""

        _validate_session_id(session_id)
        if not isinstance(ref, ArtifactRef):
            raise TypeError("ref must be an ArtifactRef.")

        prefix = f"{session_id}/"
        if not ref.path.startswith(prefix):
            raise ValueError(
                f"Artifact {ref.path!r} does not belong to session {session_id}."
            )
        expected_path = f"{session_id}/{ref.sha256}.blob"
        if ref.path != expected_path:
            raise ValueError("ArtifactRef path must match its content-addressed path.")

        self._require_session_exists(session_id)
        if self.read_only:
            return self._get_artifact_unlocked(session_id, ref)
        with self._session_lock(session_id):
            self._require_session_exists(session_id)
            return self._get_artifact_unlocked(session_id, ref)

    @contextmanager
    def exclusive_writer(self, session_id: str) -> Iterator[None]:
        """Hold the session writer lock across a complete resume operation."""

        _validate_session_id(session_id)
        self._require_writable("acquire a session writer lease")
        self._require_session_exists(session_id)
        owner = threading.get_ident()
        with _PROCESS_LOCK_GUARD:
            if session_id in self._exclusive_writer_owners:
                raise ConcurrentSessionWriteError(
                    f"Session {session_id} already has an active writer."
                )

        with self._writer_lock(session_id):
            with _PROCESS_LOCK_GUARD:
                self._exclusive_writer_owners[session_id] = owner
            try:
                yield
            finally:
                with _PROCESS_LOCK_GUARD:
                    self._exclusive_writer_owners.pop(session_id, None)

    @contextmanager
    def _session_lock(self, session_id: str) -> Iterator[None]:
        """Reuse an exclusive writer lease owned by the current thread."""

        owner = threading.get_ident()
        with _PROCESS_LOCK_GUARD:
            lease_owned = self._exclusive_writer_owners.get(session_id) == owner
        if lease_owned:
            yield
            return
        with self._writer_lock(session_id):
            yield

    @contextmanager
    def _writer_lock(self, session_id: str) -> Iterator[None]:
        """Acquire a non-blocking process and OS lock for one session."""

        _validate_session_id(session_id)
        self._require_writable("acquire a session writer lock")
        lock_path = self._lock_path(session_id)
        ensure_workspace_parent_directory(
            self.workspace,
            lock_path.relative_to(self.workspace),
        )

        with _PROCESS_LOCK_GUARD:
            if lock_path in _PROCESS_LOCKS:
                raise ConcurrentSessionWriteError(
                    f"Session {session_id} already has an active writer."
                )
            _PROCESS_LOCKS.add(lock_path)

        stream: BinaryIO | None = None
        locked = False
        try:
            lock_path = self._revalidate_store_path(
                lock_path,
                allow_missing=True,
            )
            stream = lock_path.open("a+b")
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            stream.seek(0)
            try:
                _lock_file(stream)
            except OSError as exc:
                raise ConcurrentSessionWriteError(
                    f"Session {session_id} already has an active writer."
                ) from exc
            locked = True
            yield
        finally:
            if stream is not None:
                if locked:
                    with suppress(OSError):
                        _unlock_file(stream)
                stream.close()
            with _PROCESS_LOCK_GUARD:
                _PROCESS_LOCKS.discard(lock_path)

    def _load_unlocked(
        self,
        session_id: str,
        *,
        repair_tail: bool,
    ) -> tuple[SessionEvent, ...]:
        event_path = self._session_path(session_id)
        if not event_path.is_file():
            raise SessionNotFoundError(f"Session {session_id} was not found.")

        event_path = self._revalidate_store_path(
            event_path,
            allow_missing=False,
        )
        raw = event_path.read_bytes()
        if not raw:
            raise SessionCodecError("session log is empty", source=str(event_path))

        complete_bytes, tail = _split_committed_jsonl(raw)
        events = self._decode_committed_events(
            session_id,
            event_path,
            complete_bytes,
        )
        if not tail:
            if not events:
                raise SessionCodecError(
                    "session log contains no events",
                    source=str(event_path),
                )
            return events

        tail_line = len(events) + 1
        if not repair_tail:
            raise SessionCodecError(
                "incomplete final JSONL line; pass repair_tail=True to repair it",
                source=str(event_path),
                line_number=tail_line,
            )
        if not events:
            raise SessionCodecError(
                "cannot repair a session without a committed first event",
                source=str(event_path),
                line_number=tail_line,
            )

        diagnostic = self._put_artifact_unlocked(
            session_id,
            tail,
            "application/octet-stream",
            None,
        )
        resumed = self._new_event(
            session_id=session_id,
            seq=events[-1].seq + 1,
            event_type="session.resumed",
            prev_hash=events[-1].event_hash,
            payload={
                "reason": "incomplete_final_line_repaired",
                "discarded_bytes": len(tail),
                "diagnostic_artifact": artifact_ref_to_dict(diagnostic),
            },
        )
        self._replace_incomplete_tail(event_path, complete_bytes, resumed)
        repaired = (*events, resumed)
        verify_event_chain(repaired, source=str(event_path))
        return repaired

    def _decode_committed_events(
        self,
        session_id: str,
        event_path: Path,
        complete_bytes: bytes,
    ) -> tuple[SessionEvent, ...]:
        if not complete_bytes:
            return ()
        raw_lines = complete_bytes.split(b"\n")[:-1]
        events: list[SessionEvent] = []
        for line_number, raw_line in enumerate(raw_lines, start=1):
            event = decode_event(
                raw_line,
                source=str(event_path),
                line_number=line_number,
            )
            if event.session_id != session_id:
                raise SessionCodecError(
                    f"event does not belong to requested session {session_id}",
                    source=str(event_path),
                    line_number=line_number,
                )
            events.append(event)
        verify_event_chain(events, source=str(event_path))
        return tuple(events)

    def _sanitize_payload_unlocked(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        sanitized = self.privacy_policy.sanitize_payload(
            payload,
            artifact_writer=lambda content, media_type, encoding: (
                self._put_artifact_unlocked(
                    session_id,
                    content,
                    media_type,
                    encoding,
                )
            ),
        )
        if not isinstance(sanitized, Mapping):
            raise TypeError("Sanitized session payload must remain a mapping.")
        return sanitized

    def _put_artifact_unlocked(
        self,
        session_id: str,
        content: bytes,
        media_type: str,
        encoding: str | None,
    ) -> ArtifactRef:
        if len(content) > self.privacy_policy.artifact_max_bytes:
            raise ArtifactTooLargeError(
                "Artifact exceeds the configured artifact_max_bytes limit."
            )
        content = self.privacy_policy.sanitize_artifact_content(content)
        if len(content) > self.privacy_policy.artifact_max_bytes:
            raise ArtifactTooLargeError(
                "Redacted artifact exceeds the configured artifact_max_bytes limit."
            )

        digest = hashlib.sha256(content).hexdigest()
        relative_path = f"{session_id}/{digest}.blob"
        ref = ArtifactRef(
            path=relative_path,
            sha256=digest,
            byte_count=len(content),
            media_type=media_type,
            encoding=encoding,
        )
        session_dir = self._resolve_store_path(
            f"artifacts/{session_id}",
            allow_missing=True,
        )
        session_dir.mkdir(exist_ok=True)
        session_dir = self._revalidate_store_path(
            session_dir,
            allow_missing=False,
        )
        destination = self._resolve_store_path(
            f"artifacts/{session_id}/{digest}.blob",
            allow_missing=True,
        )

        if destination.exists():
            destination = self._revalidate_store_path(
                destination,
                allow_missing=False,
            )
            existing = destination.read_bytes()
            try:
                self._verify_artifact_bytes(existing, ref, label="existing artifact")
            except ArtifactIntegrityError as exc:
                raise ArtifactIntegrityError(
                    f"Refusing to replace existing artifact {destination}: {exc}"
                ) from exc
            return ref

        temporary = self._unique_artifact_temp_path(session_dir, digest)
        try:
            temporary = self._revalidate_store_path(
                temporary,
                allow_missing=True,
            )
            with temporary.open("xb", buffering=0) as stream:
                _write_all(stream, content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary = self._revalidate_store_path(
                temporary,
                allow_missing=False,
            )
            destination = self._revalidate_store_path(
                destination,
                allow_missing=True,
            )
            os.replace(temporary, destination)
            session_dir = self._revalidate_store_path(
                session_dir,
                allow_missing=False,
            )
            _fsync_directory(session_dir)
        finally:
            with suppress(FileNotFoundError, OSError, ValueError):
                self._revalidate_store_path(
                    temporary,
                    allow_missing=True,
                ).unlink()

        destination = self._revalidate_store_path(
            destination,
            allow_missing=False,
        )
        stored = destination.read_bytes()
        self._verify_artifact_bytes(stored, ref, label="stored artifact")
        return ref

    def _unique_artifact_temp_path(self, session_dir: Path, digest: str) -> Path:
        for _ in range(_SESSION_ID_ATTEMPTS):
            name = f".{digest}.{secrets.token_hex(8)}.tmp"
            candidate = self._revalidate_store_path(
                session_dir / name,
                allow_missing=True,
            )
            if not candidate.exists():
                return candidate
        raise SessionStoreError("Could not allocate an artifact temporary file.")

    def _replace_incomplete_tail(
        self,
        event_path: Path,
        complete_bytes: bytes,
        resumed: SessionEvent,
    ) -> None:
        record = encode_event(resumed) + b"\n"
        event_path = self._revalidate_store_path(
            event_path,
            allow_missing=False,
        )
        with event_path.open("r+b", buffering=0) as stream:
            stream.truncate(len(complete_bytes))
            stream.seek(0, os.SEEK_END)
            _write_all(stream, record)
            stream.flush()
            os.fsync(stream.fileno())

    def _new_event(
        self,
        *,
        session_id: str,
        seq: int,
        event_type: SessionEventType,
        prev_hash: str | None,
        payload: Mapping[str, object],
    ) -> SessionEvent:
        return create_session_event(
            session_id=session_id,
            seq=seq,
            event_id=f"event-{seq}-{secrets.token_hex(8)}",
            recorded_at=_utc_timestamp(),
            event_type=event_type,
            prev_hash=prev_hash,
            payload=payload,
        )

    def _write_new_event_file(self, path: Path, event: SessionEvent) -> None:
        record = encode_event(event) + b"\n"
        created = False
        try:
            path = self._revalidate_store_path(path, allow_missing=True)
            with path.open("xb", buffering=0) as stream:
                created = True
                _write_all(stream, record)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            if created:
                with suppress(OSError, ValueError):
                    self._revalidate_store_path(
                        path,
                        allow_missing=True,
                    ).unlink()
            raise

    def _append_event_unlocked(self, path: Path, event: SessionEvent) -> None:
        record = encode_event(event) + b"\n"
        path = self._revalidate_store_path(path, allow_missing=False)
        with path.open("ab", buffering=0) as stream:
            _write_all(stream, record)
            stream.flush()
            os.fsync(stream.fileno())

    def _get_artifact_unlocked(
        self,
        session_id: str,
        ref: ArtifactRef,
    ) -> bytes:
        artifact_path = self._resolve_store_path(
            f"artifacts/{ref.path}",
            allow_missing=True,
        )
        artifact_path = self._revalidate_store_path(
            artifact_path,
            allow_missing=True,
        )
        if not artifact_path.is_file():
            raise ArtifactNotFoundError(
                f"Artifact {ref.sha256} was not found for session {session_id}."
            )
        artifact_path = self._revalidate_store_path(
            artifact_path,
            allow_missing=False,
        )
        content = artifact_path.read_bytes()
        self._verify_artifact_bytes(content, ref, label="artifact")
        return content

    def _verify_artifact_bytes(
        self,
        content: bytes,
        ref: ArtifactRef,
        *,
        label: str,
    ) -> None:
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != ref.sha256:
            raise ArtifactIntegrityError(
                f"{label} SHA-256 mismatch: expected {ref.sha256}, found {actual_hash}."
            )
        if len(content) != ref.byte_count:
            raise ArtifactIntegrityError(
                f"{label} byte count mismatch: expected {ref.byte_count}, "
                f"found {len(content)}."
            )

    def _require_writable(self, operation: str) -> None:
        if self.read_only:
            raise ReadOnlySessionStoreError(
                f"read-only SessionStore cannot {operation}."
            )

    def _require_session_exists(self, session_id: str) -> None:
        session_path = self._revalidate_store_path(
            self._session_path(session_id),
            allow_missing=True,
        )
        if not session_path.is_file():
            raise SessionNotFoundError(f"Session {session_id} was not found.")

    def _session_path(self, session_id: str) -> Path:
        _validate_session_id(session_id)
        return self._resolve_store_path(
            f"sessions/{session_id}.jsonl",
            allow_missing=True,
        )

    def _lock_path(self, session_id: str) -> Path:
        _validate_session_id(session_id)
        return self._resolve_store_path(
            f"locks/{session_id}.lock",
            allow_missing=True,
        )

    def _resolve_store_path(
        self,
        relative_path: str,
        *,
        allow_missing: bool,
    ) -> Path:
        requested = (
            ".coding-agent"
            if relative_path == "."
            else f".coding-agent/{relative_path}"
        )
        return resolve_workspace_path(
            self.workspace,
            requested,
            operation="write",
            allow_missing=allow_missing,
        )

    def _revalidate_store_path(
        self,
        path: Path,
        *,
        allow_missing: bool,
    ) -> Path:
        try:
            relative = path.relative_to(self.workspace)
        except ValueError as exc:
            raise SessionStoreError(
                f"Session path escapes workspace: {path}"
            ) from exc
        return resolve_workspace_path(
            self.workspace,
            relative,
            operation="write",
            allow_missing=allow_missing,
        )


def _validate_session_id(session_id: object) -> None:
    if (
        not isinstance(session_id, str)
        or "/" in session_id
        or "\\" in session_id
        or ".." in session_id
        or not SESSION_ID_PATTERN.fullmatch(session_id)
    ):
        raise ValueError(
            "session_id must use YYYYMMDDTHHMMSSZ-<8+ lowercase hex> format."
        )


def _validate_media_type(media_type: object) -> None:
    if not isinstance(media_type, str) or not media_type.strip():
        raise ValueError("media_type must be a non-empty string.")


def _validate_encoding(encoding: object) -> None:
    if encoding is not None and (
        not isinstance(encoding, str) or not encoding.strip()
    ):
        raise ValueError("encoding must be null or a non-empty string.")


def _new_session_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(8)}"


def _utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _split_committed_jsonl(raw: bytes) -> tuple[bytes, bytes]:
    if raw.endswith(b"\n"):
        return raw, b""
    last_newline = raw.rfind(b"\n")
    if last_newline < 0:
        return b"", raw
    return raw[: last_newline + 1], raw[last_newline + 1 :]


def _write_all(stream: BinaryIO, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = stream.write(view)
        if written is None or written <= 0:
            raise OSError("Could not complete durable file write.")
        view = view[written:]


def _lock_file(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from coding_agent.sessions.codec import (
    SessionCodecError,
    artifact_ref_from_dict,
    canonical_json_bytes,
    create_session_event,
    encode_event,
    session_event_to_dict,
)
from coding_agent.sessions.models import ArtifactRef
from coding_agent.sessions.store import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ConcurrentSessionWriteError,
    SessionNotFoundError,
    SessionStore,
)

TIMESTAMP = "2026-07-14T03:15:04.125Z"
SHA_A = "a" * 64


def test_create_writes_a_durable_session_started_event(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)

    session_id = store.create({"task": "修复 Unicode", "workspace": str(tmp_path)})

    event_path = store.sessions_dir / f"{session_id}.jsonl"
    raw = event_path.read_bytes()
    events = store.load(session_id)

    assert event_path.resolve().is_relative_to(tmp_path.resolve())
    assert raw.endswith(b"\n")
    assert len(raw.splitlines()) == 1
    assert len(events) == 1
    assert events[0].session_id == session_id
    assert events[0].seq == 1
    assert events[0].type == "session.started"
    assert events[0].prev_hash is None
    assert events[0].payload["task"] == "修复 Unicode"


def test_append_uses_contiguous_sequences_and_hash_chain(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "inspect"})

    context = store.append(session_id, "context.created", {"files": 3})
    requested = store.append(session_id, "model.requested", {"turn": 1})
    events = store.load(session_id)

    assert [event.seq for event in events] == [1, 2, 3]
    assert context.prev_hash == events[0].event_hash
    assert requested.prev_hash == context.event_hash
    assert [event.type for event in events] == [
        "session.started",
        "context.created",
        "model.requested",
    ]
    assert (store.sessions_dir / f"{session_id}.jsonl").read_bytes().count(b"\n") == 3


@pytest.mark.parametrize(
    "session_id",
    [
        "../other",
        "..\\other",
        "20260714T031500Z-abc",
        "20260714T031500Z-A1B2C3D4",
        "20260714T031500Z-a1b2c3d4/other",
        "20260714T031500Z-a1b2c3d4\\other",
    ],
)
def test_session_ids_are_validated_before_path_construction(
    tmp_path: Path,
    session_id: str,
) -> None:
    store = SessionStore(tmp_path)
    ref = ArtifactRef(
        path="20260714T031500Z-a1b2c3d4/" + SHA_A + ".blob",
        sha256=SHA_A,
        byte_count=0,
        media_type="application/octet-stream",
    )

    with pytest.raises(ValueError, match="session_id"):
        store.load(session_id)
    with pytest.raises(ValueError, match="session_id"):
        store.append(session_id, "context.created", {})
    with pytest.raises(ValueError, match="session_id"):
        store.put_artifact(session_id, b"", "application/octet-stream")
    with pytest.raises(ValueError, match="session_id"):
        store.get_artifact(session_id, ref)


def test_missing_session_operations_fail_without_creating_a_log(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    missing = "20260714T031500Z-a1b2c3d4"

    with pytest.raises(SessionNotFoundError, match=missing):
        store.load(missing)
    with pytest.raises(SessionNotFoundError, match=missing):
        store.append(missing, "context.created", {})
    with pytest.raises(SessionNotFoundError, match=missing):
        store.put_artifact(missing, b"data", "text/plain")

    assert not (store.sessions_dir / f"{missing}.jsonl").exists()


def test_list_sessions_returns_validated_summaries_newest_id_first(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    first_id = store.create({"task": "first"})
    second_id = store.create({"task": "second"})
    store.append(first_id, "session.completed", {"answer": "done"})

    summaries = store.list_sessions()
    by_id = {summary.session_id: summary for summary in summaries}

    assert [summary.session_id for summary in summaries] == sorted(
        [first_id, second_id], reverse=True
    )
    assert by_id[first_id].task == "first"
    assert by_id[first_id].event_count == 2
    assert by_id[first_id].last_event_type == "session.completed"
    assert by_id[first_id].status == "completed"
    assert by_id[second_id].status == "running"
    assert by_id[first_id].started_at <= by_id[first_id].updated_at


def test_incomplete_final_line_requires_explicit_repair_and_is_unchanged_by_default(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "repair"})
    store.append(session_id, "context.created", {})
    event_path = store.sessions_dir / f"{session_id}.jsonl"
    tail = b'{"schema_version":1,"partial"'
    with event_path.open("ab") as stream:
        stream.write(tail)
    damaged = event_path.read_bytes()

    with pytest.raises(SessionCodecError, match="incomplete final JSONL line"):
        store.load(session_id)

    assert event_path.read_bytes() == damaged


def test_tail_repair_saves_diagnostic_artifact_truncates_and_appends_resume(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "repair"})
    previous = store.append(session_id, "context.created", {})
    event_path = store.sessions_dir / f"{session_id}.jsonl"
    tail = b"\xffpartial-event"
    with event_path.open("ab") as stream:
        stream.write(tail)

    events = store.load(session_id, repair_tail=True)

    resumed = events[-1]
    diagnostic_data = resumed.payload["diagnostic_artifact"]
    assert isinstance(diagnostic_data, Mapping)
    artifact = artifact_ref_from_dict(diagnostic_data)
    assert resumed.type == "session.resumed"
    assert resumed.seq == 3
    assert resumed.prev_hash == previous.event_hash
    assert resumed.payload["reason"] == "incomplete_final_line_repaired"
    assert resumed.payload["discarded_bytes"] == len(tail)
    assert store.get_artifact(session_id, artifact) == tail
    assert event_path.read_bytes().endswith(b"\n")
    assert tail not in event_path.read_bytes()
    assert store.load(session_id) == events


@pytest.mark.parametrize("damage", ["invalid_json", "hash_mismatch", "sequence_jump"])
def test_repair_never_changes_complete_corrupt_lines(
    tmp_path: Path,
    damage: str,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "do not repair"})
    event_path = store.sessions_dir / f"{session_id}.jsonl"
    first = store.load(session_id)[0]

    if damage == "invalid_json":
        with event_path.open("ab") as stream:
            stream.write(b"not-json\n")
    elif damage == "hash_mismatch":
        data = session_event_to_dict(first)
        data["payload"] = {"task": "tampered"}
        event_path.write_bytes(canonical_json_bytes(data) + b"\n")
    else:
        jumped = create_session_event(
            session_id=session_id,
            seq=3,
            event_id="event-jump",
            recorded_at=TIMESTAMP,
            event_type="context.created",
            prev_hash=first.event_hash,
            payload={},
        )
        with event_path.open("ab") as stream:
            stream.write(encode_event(jumped) + b"\n")
    damaged = event_path.read_bytes()

    with pytest.raises(SessionCodecError):
        store.load(session_id, repair_tail=True)

    assert event_path.read_bytes() == damaged
    artifact_dir = store.artifacts_dir / session_id
    assert not artifact_dir.exists() or not tuple(artifact_dir.iterdir())


def test_load_rejects_an_event_from_another_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "one"})
    other_id = store.create({"task": "two"})
    first = store.load(session_id)[0]
    foreign = create_session_event(
        session_id=other_id,
        seq=2,
        event_id="foreign-event",
        recorded_at=TIMESTAMP,
        event_type="context.created",
        prev_hash=first.event_hash,
        payload={},
    )
    event_path = store.sessions_dir / f"{session_id}.jsonl"
    with event_path.open("ab") as stream:
        stream.write(encode_event(foreign) + b"\n")

    with pytest.raises(SessionCodecError, match="requested session"):
        store.load(session_id)


def test_artifacts_are_content_addressed_atomic_and_integrity_checked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "artifact"})
    content = "你好 artifact".encode()
    replace_calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
    ) -> None:
        replace_calls.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr("coding_agent.sessions.store.os.replace", recording_replace)

    ref = store.put_artifact(session_id, content, "text/plain")
    duplicate = store.put_artifact(session_id, content, "text/plain")

    assert ref == duplicate
    assert ref.path == f"{session_id}/{ref.sha256}.blob"
    assert ref.byte_count == len(content)
    assert store.get_artifact(session_id, ref) == content
    assert len(replace_calls) == 1
    assert replace_calls[0][0].parent == replace_calls[0][1].parent
    assert not tuple(replace_calls[0][1].parent.glob("*.tmp"))

    artifact_path = store.artifacts_dir / ref.path
    artifact_path.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError, match="SHA-256"):
        store.get_artifact(session_id, ref)
    with pytest.raises(ArtifactIntegrityError, match="existing artifact"):
        store.put_artifact(session_id, content, "text/plain")


def test_artifact_replace_failure_removes_the_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "replace failure"})

    def fail_replace(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
    ) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr("coding_agent.sessions.store.os.replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        store.put_artifact(session_id, b"content", "application/octet-stream")

    artifact_dir = store.artifacts_dir / session_id
    assert artifact_dir.is_dir()
    assert not tuple(artifact_dir.iterdir())


def test_get_artifact_rejects_missing_mismatched_or_cross_session_refs(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    first_id = store.create({"task": "one"})
    second_id = store.create({"task": "two"})
    ref = store.put_artifact(first_id, b"content", "application/octet-stream")

    with pytest.raises(ValueError, match="does not belong"):
        store.get_artifact(second_id, ref)

    missing = replace(ref, sha256=SHA_A, path=f"{first_id}/{SHA_A}.blob")
    with pytest.raises(ArtifactNotFoundError, match=SHA_A):
        store.get_artifact(first_id, missing)

    wrong_path = replace(ref, path=f"{first_id}/other.blob")
    with pytest.raises(ValueError, match="content-addressed path"):
        store.get_artifact(first_id, wrong_path)



def test_exclusive_writer_reuses_its_lease_for_normal_store_operations(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "lease"})

    with store.exclusive_writer(session_id):
        assert len(store.load(session_id)) == 1
        appended = store.append(session_id, "context.created", {"files": 1})
        ref = store.put_artifact(session_id, b"leased", "text/plain", encoding="utf-8")
        assert store.get_artifact(session_id, ref) == b"leased"

    assert appended.seq == 2
    assert len(store.load(session_id)) == 2


def test_exclusive_writer_rejects_a_second_store_instance(tmp_path: Path) -> None:
    first = SessionStore(tmp_path)
    session_id = first.create({"task": "exclusive lease"})
    second = SessionStore(tmp_path)

    with first.exclusive_writer(session_id):
        with pytest.raises(ConcurrentSessionWriteError, match=session_id):
            with second.exclusive_writer(session_id):
                pytest.fail("second writer unexpectedly acquired the lease")

def test_writer_lock_rejects_a_second_writer(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "lock"})

    with store._writer_lock(session_id):
        with pytest.raises(ConcurrentSessionWriteError, match=session_id):
            store.append(session_id, "context.created", {})

    appended = store.append(session_id, "context.created", {})
    assert appended.seq == 2


def test_writer_lock_rejects_a_writer_in_another_process(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "cross-process lock"})
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    existing_pythonpath = environment.get("PYTHONPATH")
    source_path = str(repository_root / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not existing_pythonpath
        else source_path + os.pathsep + existing_pythonpath
    )
    child_code = """
import sys
from coding_agent.sessions.store import SessionStore

store = SessionStore(sys.argv[1])
with store._writer_lock(sys.argv[2]):
    print("locked", flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(tmp_path), session_id],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        assert child.stdout is not None
        ready = child.stdout.readline().strip()
        if ready != "locked":
            assert child.stderr is not None
            pytest.fail(f"lock holder failed to start: {child.stderr.read()}")

        with pytest.raises(ConcurrentSessionWriteError, match=session_id):
            store.append(session_id, "context.created", {})
    finally:
        if child.stdin is not None and child.poll() is None:
            child.stdin.write("\n")
            child.stdin.flush()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)

    assert child.returncode == 0


def test_append_and_artifact_writes_call_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.create({"task": "durability"})
    real_fsync = os.fsync
    calls: list[int] = []

    def recording_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr("coding_agent.sessions.store.os.fsync", recording_fsync)

    store.append(session_id, "context.created", {})
    store.put_artifact(session_id, b"durable", "application/octet-stream")

    assert len(calls) >= 2


def test_store_directories_remain_under_workspace(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    workspace = tmp_path.resolve()

    assert store.root == workspace / ".coding-agent"
    assert store.sessions_dir == store.root / "sessions"
    assert store.artifacts_dir == store.root / "artifacts"
    assert store.locks_dir == store.root / "locks"
    assert all(
        path.resolve().is_relative_to(workspace)
        for path in (
            store.root,
            store.sessions_dir,
            store.artifacts_dir,
            store.locks_dir,
        )
    )


def test_session_store_rejects_symlinked_internal_state_directory(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-session-outside"
    outside.mkdir()
    state_directory = tmp_path / ".coding-agent"
    try:
        state_directory.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable on this platform: {exc}")

    with pytest.raises(ValueError, match="symlink or reparse"):
        SessionStore(tmp_path)

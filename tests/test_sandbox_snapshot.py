from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import coding_agent.security.snapshot as snapshot_module
from coding_agent.security import (
    SandboxWorkspaceSnapshot,
    SnapshotAlreadyExistsError,
    SnapshotBudgetExceededError,
    SnapshotSourceChangedError,
    cleanup_sandbox_workspace_snapshot,
    create_sandbox_workspace_snapshot,
)


def _manifest_paths(snapshot: SandboxWorkspaceSnapshot) -> set[str]:
    return {entry.path for entry in snapshot.manifest.files}


def _snapshot_files(snapshot: SandboxWorkspaceSnapshot) -> set[str]:
    return {
        path.relative_to(snapshot.workspace_directory).as_posix()
        for path in snapshot.workspace_directory.rglob("*")
        if path.is_file()
    }


def _assert_no_partial_snapshot(root: Path, session_id: str, call_id: str) -> None:
    session_directory = root / ".coding-agent" / "sandboxes" / session_id
    assert not (session_directory / call_id).exists()
    if session_directory.exists():
        assert not list(session_directory.glob(f".{call_id}.*.tmp"))


def test_snapshot_filters_sensitive_ignored_and_large_binary_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("public", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (tmp_path / ".env.example").write_text("TOKEN=", encoding="utf-8")
    (tmp_path / "server.key").write_text("private-key", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "large.png").write_bytes(b"binary-data")
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    agent_state = tmp_path / ".coding-agent"
    agent_state.mkdir()
    (agent_state / "events.jsonl").write_text("secret event", encoding="utf-8")
    virtualenv = tmp_path / ".venv"
    virtualenv.mkdir()
    (virtualenv / "installed.py").write_text("noise", encoding="utf-8")

    snapshot = create_sandbox_workspace_snapshot(
        tmp_path,
        session_id="session-1",
        call_id="call-1",
        max_binary_file_bytes=4,
    )

    expected = {".env.example", ".gitignore", "README.md"}
    assert _manifest_paths(snapshot) == expected
    assert _snapshot_files(snapshot) == expected
    assert snapshot.excluded_counts["sensitive"] >= 2
    assert snapshot.excluded_counts["ignored"] >= 2
    assert snapshot.excluded_counts["large_binary"] == 1
    assert snapshot.manifest_path.parent == snapshot.call_directory
    assert not (snapshot.workspace_directory / "manifest.json").exists()
    stored = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
    assert stored == snapshot.manifest.to_dict()
    assert "TOKEN=secret" not in json.dumps(snapshot.audit_summary())


def test_snapshot_honors_nested_gitignore_and_negation(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.log\n!keep.log\n", encoding="utf-8")
    (tmp_path / "drop.log").write_text("drop", encoding="utf-8")
    (tmp_path / "keep.log").write_text("keep", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    (nested / "secret.txt").write_text("secret", encoding="utf-8")
    (nested / "visible.txt").write_text("visible", encoding="utf-8")

    snapshot = create_sandbox_workspace_snapshot(
        tmp_path,
        session_id="nested-session",
        call_id="nested-call",
    )

    assert _manifest_paths(snapshot) == {
        ".gitignore",
        "keep.log",
        "nested/.gitignore",
        "nested/visible.txt",
    }
    assert snapshot.excluded_counts["ignored"] == 2


def test_snapshot_excludes_internal_and_external_symlinks(tmp_path: Path) -> None:
    internal = tmp_path / "internal.txt"
    internal.write_text("inside", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    internal_link = tmp_path / "internal-link.txt"
    external_link = tmp_path / "external-link.txt"
    try:
        internal_link.symlink_to(internal)
        external_link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    snapshot = create_sandbox_workspace_snapshot(
        tmp_path,
        session_id="link-session",
        call_id="link-call",
    )

    assert _manifest_paths(snapshot) == {"internal.txt"}
    assert snapshot.excluded_counts["symlink"] == 2
    assert "outside" not in snapshot.manifest_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("files", "limits"),
    [
        ({"a.txt": b"a", "b.txt": b"b"}, {"max_files": 1}),
        ({"large.txt": b"1234"}, {"max_bytes": 3}),
    ],
)
def test_snapshot_budget_failure_publishes_no_snapshot(
    tmp_path: Path,
    files: dict[str, bytes],
    limits: dict[str, int],
) -> None:
    for relative_path, content in files.items():
        (tmp_path / relative_path).write_bytes(content)

    with pytest.raises(SnapshotBudgetExceededError):
        create_sandbox_workspace_snapshot(
            tmp_path,
            session_id="budget-session",
            call_id="budget-call",
            **limits,
        )

    _assert_no_partial_snapshot(tmp_path, "budget-session", "budget-call")


def test_snapshot_detects_source_change_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source.txt"
    source_path.write_text("original", encoding="utf-8")
    original_copy = snapshot_module._copy_source_file

    def changing_copy(root, source, entry, destination) -> None:
        original_copy(root, source, entry, destination)
        source.path.write_text("changed-after-copy", encoding="utf-8")

    monkeypatch.setattr(snapshot_module, "_copy_source_file", changing_copy)

    with pytest.raises(SnapshotSourceChangedError):
        create_sandbox_workspace_snapshot(
            tmp_path,
            session_id="change-session",
            call_id="change-call",
        )

    _assert_no_partial_snapshot(tmp_path, "change-session", "change-call")


def test_snapshot_partial_copy_failure_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    original_copy = snapshot_module._copy_source_file
    copied = 0

    def failing_copy(root, source, entry, destination) -> None:
        nonlocal copied
        copied += 1
        if copied == 2:
            raise OSError("simulated copy failure")
        original_copy(root, source, entry, destination)

    monkeypatch.setattr(snapshot_module, "_copy_source_file", failing_copy)

    with pytest.raises(OSError, match="simulated copy failure"):
        create_sandbox_workspace_snapshot(
            tmp_path,
            session_id="failure-session",
            call_id="failure-call",
        )

    _assert_no_partial_snapshot(tmp_path, "failure-session", "failure-call")


def test_snapshot_manifest_is_deterministic_across_session_and_call_ids(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")

    first = create_sandbox_workspace_snapshot(
        tmp_path,
        session_id="session-a",
        call_id="call-a",
    )
    second = create_sandbox_workspace_snapshot(
        tmp_path,
        session_id="session-b",
        call_id="call-b",
    )

    assert first.manifest.canonical_bytes() == second.manifest.canonical_bytes()
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.call_directory != second.call_directory


def test_snapshot_changes_do_not_modify_source_workspace(tmp_path: Path) -> None:
    source_path = tmp_path / "app.py"
    source_path.write_text("original\n", encoding="utf-8")
    snapshot = create_sandbox_workspace_snapshot(
        tmp_path,
        session_id="isolation-session",
        call_id="isolation-call",
    )

    snapshot_path = snapshot.workspace_directory / "app.py"
    snapshot_path.write_text("sandbox change\n", encoding="utf-8")

    assert source_path.read_text(encoding="utf-8") == "original\n"
    assert snapshot_path.read_text(encoding="utf-8") == "sandbox change\n"


def test_snapshot_cleanup_is_successful_and_idempotent(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("content", encoding="utf-8")
    snapshot = create_sandbox_workspace_snapshot(
        tmp_path,
        session_id="cleanup-session",
        call_id="cleanup-call",
    )

    first = cleanup_sandbox_workspace_snapshot(snapshot)
    second = cleanup_sandbox_workspace_snapshot(snapshot)

    assert first.removed is True
    assert first.cleanup_error is None
    assert second.removed is True
    assert second.cleanup_error is None
    assert not snapshot.call_directory.exists()


def test_snapshot_cleanup_refuses_replaced_symlink(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("content", encoding="utf-8")
    snapshot = create_sandbox_workspace_snapshot(
        tmp_path,
        session_id="replace-session",
        call_id="replace-call",
    )
    outside = tmp_path.parent / f"{tmp_path.name}-cleanup-target"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    shutil.rmtree(snapshot.call_directory)
    try:
        snapshot.call_directory.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Directory symlink creation is unavailable: {exc}")

    result = cleanup_sandbox_workspace_snapshot(snapshot)

    assert result.removed is False
    assert result.cleanup_error is not None
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert snapshot.call_directory.is_symlink()
    snapshot.call_directory.unlink()


def test_snapshot_refuses_to_replace_existing_call_directory(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("content", encoding="utf-8")
    create_sandbox_workspace_snapshot(
        tmp_path,
        session_id="existing-session",
        call_id="existing-call",
    )

    with pytest.raises(SnapshotAlreadyExistsError):
        create_sandbox_workspace_snapshot(
            tmp_path,
            session_id="existing-session",
            call_id="existing-call",
        )


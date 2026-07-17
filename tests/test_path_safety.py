from pathlib import Path
from types import SimpleNamespace

import pytest

from coding_agent.path_safety import resolve_inside_workspace, resolve_workspace_path


def test_resolve_inside_workspace_allows_files_inside_workspace(tmp_path: Path) -> None:
    resolved = resolve_inside_workspace(tmp_path, "src/index.py")
    assert resolved == tmp_path / "src" / "index.py"


def test_resolve_inside_workspace_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Path escapes workspace"):
        resolve_inside_workspace(tmp_path, "../secret.txt")


def test_workspace_path_allows_absolute_path_only_when_contained(tmp_path: Path) -> None:
    target = tmp_path / "inside.txt"
    target.write_text("inside\n", encoding="utf-8")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")

    assert resolve_workspace_path(
        tmp_path,
        target,
        operation="read",
    ) == target
    with pytest.raises(ValueError, match="Path escapes workspace"):
        resolve_workspace_path(tmp_path, outside, operation="read")


@pytest.mark.parametrize(
    "requested_path",
    [
        "bad\x00name.txt",
        r"\\server\share\secret.txt",
        r"\\?\C:\workspace\secret.txt",
        r"\\.\PhysicalDrive0",
        "C:relative.txt",
        "file.txt:metadata",
    ],
)
def test_workspace_path_rejects_unsafe_windows_syntax(
    tmp_path: Path,
    requested_path: str,
) -> None:
    with pytest.raises(ValueError):
        resolve_workspace_path(
            tmp_path,
            requested_path,
            operation="read",
            allow_missing=True,
        )


def test_workspace_path_rejects_missing_read_but_can_validate_missing_write(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError, match="Path does not exist"):
        resolve_workspace_path(tmp_path, "new/file.txt", operation="read")

    target = resolve_workspace_path(
        tmp_path,
        "new/file.txt",
        operation="write",
        allow_missing=True,
    )
    assert target == tmp_path / "new" / "file.txt"


def test_read_allows_internal_symlink_but_write_and_snapshot_reject_it(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("safe\n", encoding="utf-8")
    link = tmp_path / "internal-link.txt"
    _create_symlink_or_skip(link, target)

    assert resolve_workspace_path(tmp_path, link, operation="read") == target
    for operation in ("write", "snapshot"):
        with pytest.raises(ValueError, match="symlink or reparse"):
            resolve_workspace_path(
                tmp_path,
                link,
                operation=operation,  # type: ignore[arg-type]
                allow_missing=True,
            )


def test_external_symlink_and_symlink_parent_are_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "secret.txt"
    outside_file.write_text("secret\n", encoding="utf-8")

    external_link = workspace / "external.txt"
    _create_symlink_or_skip(external_link, outside_file)
    with pytest.raises(ValueError, match="realpath boundary"):
        resolve_workspace_path(workspace, external_link, operation="read")

    linked_parent = workspace / "linked-parent"
    _create_symlink_or_skip(linked_parent, outside, is_directory=True)
    with pytest.raises(ValueError, match="symlink or reparse"):
        resolve_workspace_path(
            workspace,
            "linked-parent/created.txt",
            operation="write",
            allow_missing=True,
        )


def test_broken_symlink_is_not_treated_as_a_safe_missing_write_target(
    tmp_path: Path,
) -> None:
    link = tmp_path / "broken.txt"
    _create_symlink_or_skip(link, tmp_path / "missing.txt")

    with pytest.raises(FileNotFoundError, match="Path does not exist"):
        resolve_workspace_path(tmp_path, link, operation="read")
    with pytest.raises(ValueError, match="symlink or reparse"):
        resolve_workspace_path(
            tmp_path,
            link,
            operation="write",
            allow_missing=True,
        )


def test_reparse_point_attribute_is_rejected_for_non_read_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "reparse-like.txt"
    target.write_text("safe\n", encoding="utf-8")
    original_lstat = Path.lstat

    def reparse_lstat(path: Path):
        result = original_lstat(path)
        if path == target:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_file_attributes=0x0400,
            )
        return result

    monkeypatch.setattr(Path, "lstat", reparse_lstat)

    with pytest.raises(ValueError, match="symlink or reparse"):
        resolve_workspace_path(
            tmp_path,
            target,
            operation="write",
            allow_missing=False,
        )


def _create_symlink_or_skip(
    link: Path,
    target: Path,
    *,
    is_directory: bool = False,
) -> None:
    try:
        link.symlink_to(target, target_is_directory=is_directory)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable on this platform: {exc}")

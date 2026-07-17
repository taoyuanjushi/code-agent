from pathlib import Path

import pytest

from coding_agent.patch import apply_patch_plan, plan_patch, summarize_patch_plan


def test_plan_patch_applies_file_modification(tmp_path: Path) -> None:
    source = tmp_path / "src" / "hello.txt"
    source.parent.mkdir()
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")

    patch = "\n".join(
        [
            "--- a/src/hello.txt",
            "+++ b/src/hello.txt",
            "@@ -1,3 +1,3 @@",
            " one",
            "-two",
            "+changed",
            " three",
            "",
        ]
    )

    patch_plan = plan_patch(tmp_path, patch)
    assert "modify src/hello.txt" in summarize_patch_plan(patch_plan)

    apply_patch_plan(patch_plan)

    assert source.read_text(encoding="utf-8") == "one\nchanged\nthree\n"


def test_plan_patch_applies_new_file(tmp_path: Path) -> None:
    patch = "\n".join(
        [
            "--- /dev/null",
            "+++ b/README.md",
            "@@ -0,0 +1,2 @@",
            "+hello",
            "+world",
            "",
        ]
    )

    patch_plan = plan_patch(tmp_path, patch)
    assert patch_plan.files[0].change_type == "add"

    apply_patch_plan(patch_plan)

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "hello\nworld\n"


def test_plan_patch_applies_file_deletion(tmp_path: Path) -> None:
    target = tmp_path / "delete-me.txt"
    target.write_text("remove\n", encoding="utf-8")

    patch = "\n".join(
        [
            "--- a/delete-me.txt",
            "+++ /dev/null",
            "@@ -1 +0,0 @@",
            "-remove",
            "",
        ]
    )

    patch_plan = plan_patch(tmp_path, patch)
    assert patch_plan.files[0].change_type == "delete"

    apply_patch_plan(patch_plan)

    assert not target.exists()


def test_plan_patch_rejects_mismatched_context(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("actual\n", encoding="utf-8")

    patch = "\n".join(
        [
            "--- a/file.txt",
            "+++ b/file.txt",
            "@@ -1 +1 @@",
            "-expected",
            "+changed",
            "",
        ]
    )

    with pytest.raises(ValueError, match="Patch context mismatch"):
        plan_patch(tmp_path, patch)


def test_plan_patch_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    patch = "\n".join(
        [
            "--- /dev/null",
            "+++ b/../outside.txt",
            "@@ -0,0 +1 @@",
            "+nope",
            "",
        ]
    )

    with pytest.raises(ValueError, match="Path escapes workspace"):
        plan_patch(tmp_path, patch)


def test_patch_plan_rejects_internal_symlink_write_target(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    link = tmp_path / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable on this platform: {exc}")

    patch = "\n".join(
        [
            "--- a/linked.txt",
            "+++ b/linked.txt",
            "@@ -1 +1 @@",
            "-before",
            "+after",
            "",
        ]
    )

    with pytest.raises(ValueError, match="symlink or reparse"):
        plan_patch(tmp_path, patch)
    assert target.read_text(encoding="utf-8") == "before\n"


def test_apply_patch_plan_revalidates_parent_after_planning(tmp_path: Path) -> None:
    source_directory = tmp_path / "src"
    source_directory.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    patch = "\n".join(
        [
            "--- /dev/null",
            "+++ b/src/created.txt",
            "@@ -0,0 +1 @@",
            "+safe",
            "",
        ]
    )
    patch_plan = plan_patch(tmp_path, patch)

    source_directory.rmdir()
    try:
        source_directory.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable on this platform: {exc}")

    with pytest.raises(ValueError, match="symlink or reparse"):
        apply_patch_plan(patch_plan)
    assert not (outside / "created.txt").exists()

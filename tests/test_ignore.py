import json
from pathlib import Path

import pytest

from coding_agent.ignore import DEFAULT_IGNORES, load_ignore_policy
from coding_agent.tools import execute_tool
from coding_agent.types import AgentConfig


def _config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="read-only",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=10,
        context_max_bytes_per_file=1_000,
    )


def test_ignore_policy_supports_exact_directory_glob_and_negation(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text(
        "# generated files\nexact.txt\ncache/\n*.log\n!keep.log\n",
        encoding="utf-8",
    )
    (tmp_path / "cache").mkdir()
    paths = {
        "exact": tmp_path / "exact.txt",
        "cached": tmp_path / "cache" / "value.py",
        "log": tmp_path / "debug.log",
        "keep": tmp_path / "keep.log",
        "source": tmp_path / "source.py",
    }
    for path in paths.values():
        path.write_text("content\n", encoding="utf-8")

    policy = load_ignore_policy(tmp_path)

    assert policy.is_ignored(paths["exact"]) is True
    assert policy.is_ignored(paths["cached"]) is True
    assert policy.is_ignored(paths["log"]) is True
    assert policy.is_ignored(paths["keep"]) is False
    assert policy.is_ignored(paths["source"]) is False


def test_nested_gitignore_overrides_parent_only_in_its_subtree(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / ".gitignore").write_text(
        "!keep.tmp\nprivate/\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "private").mkdir()
    (tmp_path / "docs").mkdir()

    root_keep = tmp_path / "keep.tmp"
    src_keep = tmp_path / "src" / "keep.tmp"
    docs_keep = tmp_path / "docs" / "keep.tmp"
    private_file = tmp_path / "src" / "private" / "secret.py"
    for path in [root_keep, src_keep, docs_keep, private_file]:
        path.write_text("content\n", encoding="utf-8")

    policy = load_ignore_policy(tmp_path)

    assert [path.relative_to(tmp_path).as_posix() for path in policy.gitignore_files] == [
        ".gitignore",
        "src/.gitignore",
    ]
    assert policy.is_ignored(root_keep) is True
    assert policy.is_ignored(src_keep) is False
    assert policy.is_ignored(docs_keep) is True
    assert policy.is_ignored(private_file) is True


def test_ignored_parent_directory_cannot_reinclude_a_child(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(
        "ignored/\n!ignored/keep.txt\n",
        encoding="utf-8",
    )
    (tmp_path / "ignored").mkdir()
    keep = tmp_path / "ignored" / "keep.txt"
    keep.write_text("content\n", encoding="utf-8")

    policy = load_ignore_policy(tmp_path)

    assert policy.is_ignored(keep) is True


def test_default_ignores_use_exact_path_components(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv-tools").mkdir()
    ignored = tmp_path / ".venv" / "module.py"
    allowed = tmp_path / ".venv-tools" / "module.py"
    ignored.write_text("content\n", encoding="utf-8")
    allowed.write_text("content\n", encoding="utf-8")

    policy = load_ignore_policy(tmp_path)

    assert ".venv" in DEFAULT_IGNORES
    assert policy.is_ignored(ignored) is True
    assert policy.is_ignored(allowed) is False


def test_binary_suffix_detection_is_case_insensitive(tmp_path: Path) -> None:
    policy = load_ignore_policy(tmp_path)

    assert policy.is_binary(tmp_path / "asset.PNG") is True
    assert policy.is_binary(tmp_path / "archive.zip") is True
    assert policy.is_binary(tmp_path / "module.py") is False


def test_ignore_policy_rejects_paths_outside_root(tmp_path: Path) -> None:
    policy = load_ignore_policy(tmp_path)

    with pytest.raises(ValueError, match="outside ignore policy root"):
        policy.is_ignored(tmp_path.parent / "outside.txt")


def test_list_files_uses_shared_ignore_policy(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored.txt\nignored-dir/\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("hidden\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("visible\n", encoding="utf-8")
    (tmp_path / "ignored-dir").mkdir()

    result = execute_tool(_config(tmp_path), "list_files", json.dumps({"path": "."}))
    ignored_result = execute_tool(
        _config(tmp_path),
        "list_files",
        json.dumps({"path": "ignored-dir"}),
    )

    assert result.ok is True
    assert "file visible.txt" in result.output
    assert "ignored.txt" not in result.output
    assert "ignored-dir" not in result.output
    assert ignored_result.ok is False
    assert "ignored" in ignored_result.output.lower()

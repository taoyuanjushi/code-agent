import json
from pathlib import Path

import pytest

from coding_agent.reader import format_file_read_results, read_many_files
from coding_agent.tools import TOOL_DEFINITIONS, execute_tool
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


def test_read_many_files_keeps_partial_successes_for_unsafe_or_unreadable_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "allowed.txt").write_text("allowed\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    (tmp_path / "asset.PNG").write_bytes(b"not really an image")
    (tmp_path / "binary.txt").write_bytes(b"text\0binary")

    results = read_many_files(
        str(tmp_path),
        [
            "allowed.txt",
            "ignored.txt",
            "folder",
            "asset.PNG",
            "binary.txt",
            "../outside.txt",
        ],
        max_files=10,
        max_bytes_per_file=100,
        max_total_bytes=1_000,
    )

    assert results[0].ok is True
    assert results[0].content.strip() == "allowed"
    assert all(result.ok is False for result in results[1:])
    assert "ignored" in (results[1].error or "").lower()
    assert "not a file" in (results[2].error or "").lower()
    assert "binary" in (results[3].error or "").lower()
    assert "binary" in (results[4].error or "").lower()
    assert "workspace" in (results[5].error or "").lower()


def test_read_many_files_returns_scoped_instruction_paths_and_formats_bodies_once(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text("ROOT_READER_RULE\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "AGENTS.md").write_text(
        "SRC_READER_RULE\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "src" / "test_service.py").write_text(
        "def test_value(): pass\n",
        encoding="utf-8",
    )

    results = read_many_files(
        str(tmp_path),
        ["src/service.py", "src/test_service.py"],
    )
    formatted = format_file_read_results(str(tmp_path), results)

    assert [result.instruction_paths for result in results] == [
        ("AGENTS.md", "src/AGENTS.md"),
        ("AGENTS.md", "src/AGENTS.md"),
    ]
    assert formatted.count("ROOT_READER_RULE") == 1
    assert formatted.count("SRC_READER_RULE") == 1
    assert formatted.index("===== src/service.py =====") < formatted.index(
        "===== src/test_service.py ====="
    )
    assert "applicable AGENTS.md: AGENTS.md, src/AGENTS.md" in formatted


def test_read_many_files_respects_utf8_byte_boundaries_and_rejects_invalid_text(
    tmp_path: Path,
) -> None:
    (tmp_path / "unicode.txt").write_text("你好", encoding="utf-8")
    (tmp_path / "invalid.txt").write_bytes(b"valid\xffinvalid")

    results = read_many_files(
        str(tmp_path),
        ["unicode.txt", "invalid.txt"],
        max_bytes_per_file=4,
        max_total_bytes=10,
    )

    assert results[0].ok is True
    assert results[0].content == "你"
    assert results[0].truncated is True
    assert len(results[0].content.encode("utf-8")) <= 4
    assert results[1].ok is False
    assert results[1].error


def test_read_many_files_tool_is_advertised_and_uses_stable_boundaries(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("abcdefgh", encoding="utf-8")
    (tmp_path / "b.txt").write_text("klmnopqr", encoding="utf-8")

    assert "read_many_files" in {
        definition["name"] for definition in TOOL_DEFINITIONS
    }

    result = execute_tool(
        _config(tmp_path),
        "read_many_files",
        json.dumps(
            {
                "paths": ["a.txt", "b.txt"],
                "max_files": 2,
                "max_bytes_per_file": 4,
                "max_total_bytes": 6,
            }
        ),
    )

    assert result.ok is True
    assert "===== a.txt =====" in result.output
    assert "===== b.txt =====" in result.output
    assert "abcd" in result.output
    assert "kl" in result.output


def test_read_many_files_rejects_non_positive_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_files"):
        read_many_files(str(tmp_path), [], max_files=0)


def test_read_many_files_reports_file_count_and_total_budget_exhaustion(
    tmp_path: Path,
) -> None:
    for name in ["first.txt", "second.txt", "third.txt"]:
        (tmp_path / name).write_text("abcd", encoding="utf-8")

    file_limited = read_many_files(
        str(tmp_path),
        ["first.txt", "second.txt"],
        max_files=1,
        max_bytes_per_file=10,
        max_total_bytes=20,
    )
    byte_limited = read_many_files(
        str(tmp_path),
        ["first.txt", "second.txt", "third.txt"],
        max_files=3,
        max_bytes_per_file=10,
        max_total_bytes=5,
    )

    assert file_limited[0].ok is True
    assert file_limited[1].ok is False
    assert file_limited[1].error == "File count limit exceeded (max_files=1)."

    assert byte_limited[0].content == "abcd"
    assert byte_limited[0].truncated is False
    assert byte_limited[1].content == "a"
    assert byte_limited[1].truncated is True
    assert byte_limited[2].ok is False
    assert byte_limited[2].error == (
        "Total byte limit exhausted (max_total_bytes=5)."
    )


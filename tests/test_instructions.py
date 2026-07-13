import json
from pathlib import Path

import pytest

from coding_agent.context import collect_workspace_snapshot
from coding_agent.instructions import (
    discover_agent_instructions,
    format_agent_instructions,
    instructions_for_path,
)
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


def test_discovery_loads_root_and_nested_agents_even_when_files_are_ignored(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text(
        "AGENTS.md\nvendor/\n",
        encoding="utf-8",
    )
    (tmp_path / "AGENTS.md").write_text("root rule\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "AGENTS.md").write_text("src rule\n", encoding="utf-8")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "AGENTS.md").write_text(
        "ignored vendor rule\n",
        encoding="utf-8",
    )

    instructions = discover_agent_instructions(tmp_path)
    snapshot = collect_workspace_snapshot(
        str(tmp_path),
        task="inspect repository instructions",
        max_inventory_files=20,
        max_sample_files=20,
        max_bytes_per_file=1_000,
        max_total_sample_bytes=8_000,
    )
    ignored_instruction_result = execute_tool(
        _config(tmp_path),
        "read_file",
        json.dumps({"path": "vendor/AGENTS.md"}),
    )

    assert [instruction.path for instruction in instructions] == [
        "AGENTS.md",
        "src/AGENTS.md",
    ]
    assert [instruction.directory for instruction in instructions] == [".", "src"]
    assert [instruction.content.strip() for instruction in instructions] == [
        "root rule",
        "src rule",
    ]
    assert "AGENTS.md" in {file.path for file in snapshot.files}
    assert "src/AGENTS.md" in {file.path for file in snapshot.files}
    assert "vendor/AGENTS.md" not in {file.path for file in snapshot.files}
    assert ignored_instruction_result.ok is False
    assert "ignored" in ignored_instruction_result.output.lower()


def test_instructions_for_path_returns_root_to_most_specific_scope(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text("root rule\n", encoding="utf-8")
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "AGENTS.md").write_text("src rule\n", encoding="utf-8")
    (tmp_path / "src" / "api" / "AGENTS.md").write_text(
        "api rule\n",
        encoding="utf-8",
    )

    instructions = discover_agent_instructions(tmp_path)

    assert [
        instruction.path
        for instruction in instructions_for_path(instructions, "src\\api\\routes.py")
    ] == ["AGENTS.md", "src/AGENTS.md", "src/api/AGENTS.md"]
    assert [
        instruction.path
        for instruction in instructions_for_path(instructions, "src/service.py")
    ] == ["AGENTS.md", "src/AGENTS.md"]
    assert [
        instruction.path
        for instruction in instructions_for_path(instructions, "docs/guide.md")
    ] == ["AGENTS.md"]


def test_instructions_for_path_rejects_absolute_and_parent_paths(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root rule\n", encoding="utf-8")
    instructions = discover_agent_instructions(tmp_path)

    with pytest.raises(ValueError, match="workspace-relative"):
        instructions_for_path(instructions, "../outside.py")
    with pytest.raises(ValueError, match="workspace-relative"):
        instructions_for_path(instructions, "/absolute.py")
    with pytest.raises(ValueError, match="workspace-relative"):
        instructions_for_path(instructions, r"C:\outside.py")
    with pytest.raises(ValueError, match="workspace-relative"):
        instructions_for_path(instructions, r"\server\share\outside.py")


def test_instruction_content_is_bounded_and_reports_truncation(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("abcdefghij", encoding="utf-8")

    instructions = discover_agent_instructions(tmp_path, max_bytes_per_file=5)

    assert len(instructions) == 1
    assert instructions[0].truncated is True
    assert instructions[0].content.startswith("abcde")
    assert "[Truncated after 5 bytes]" in instructions[0].content


def test_format_agent_instructions_keeps_source_and_scope(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root rule\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "AGENTS.md").write_text("src rule\n", encoding="utf-8")
    instructions = discover_agent_instructions(tmp_path)

    formatted = format_agent_instructions(instructions)

    assert "### AGENTS.md" in formatted
    assert "Scope: workspace root" in formatted
    assert "### src/AGENTS.md" in formatted
    assert "Scope: src" in formatted
    assert formatted.index("root rule") < formatted.index("src rule")


def test_agents_files_stay_in_inventory_but_not_regular_samples(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("AGENTS.md\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("root rule\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "AGENTS.md").write_text("src rule\n", encoding="utf-8")
    (tmp_path / "src" / "service.py").write_text("VALUE = 1\n", encoding="utf-8")

    snapshot = collect_workspace_snapshot(
        str(tmp_path),
        task="inspect repository instructions",
        max_inventory_files=20,
        max_sample_files=20,
        max_bytes_per_file=1_000,
        max_total_sample_bytes=8_000,
    )

    inventory = {file.path for file in snapshot.files}
    sample_paths = {sample.path for sample in snapshot.samples}
    assert "AGENTS.md" in inventory
    assert "src/AGENTS.md" in inventory
    assert "AGENTS.md" not in sample_paths
    assert "src/AGENTS.md" not in sample_paths
    assert "src/service.py" not in sample_paths


def test_read_file_reports_the_applicable_instruction_chain(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root rule\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "AGENTS.md").write_text("src rule\n", encoding="utf-8")
    (tmp_path / "src" / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")

    source_result = execute_tool(
        _config(tmp_path),
        "read_file",
        json.dumps({"path": "src/service.py"}),
    )
    docs_result = execute_tool(
        _config(tmp_path),
        "read_file",
        json.dumps({"path": "docs/guide.md"}),
    )

    assert source_result.ok is True
    assert "### AGENTS.md" in source_result.output
    assert "### src/AGENTS.md" in source_result.output
    assert source_result.output.index("root rule") < source_result.output.index("src rule")
    assert "[File contents: src/service.py]" in source_result.output
    assert "VALUE = 1" in source_result.output

    assert docs_result.ok is True
    assert "### AGENTS.md" in docs_result.output
    assert "src/AGENTS.md" not in docs_result.output
    assert "guide" in docs_result.output
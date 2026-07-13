import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coding_agent.search as search_module
from coding_agent.agent import run_agent
from coding_agent.context import collect_workspace_snapshot, format_snapshot
from coding_agent.search import search_text
from coding_agent.types import AgentConfig

M2_MAX_INITIAL_SAMPLES = 6
M2_MAX_INITIAL_CONTENT_BYTES = 64 * 1024



def _config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="read-only",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=M2_MAX_INITIAL_SAMPLES,
        context_max_bytes_per_file=8_000,
    )


def test_context_and_search_share_gitignore_policy(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(
        "*.log\n!important.log\nbuild/\n",
        encoding="utf-8",
    )
    (tmp_path / "ignored.log").write_text("M2_SECRET\n", encoding="utf-8")
    (tmp_path / "important.log").write_text("M2_SECRET\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "cache.py").write_text("M2_SECRET\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "local.py").write_text("M2_SECRET\n", encoding="utf-8")

    snapshot = collect_workspace_snapshot(
        str(tmp_path),
        task="find M2_SECRET",
        max_inventory_files=20,
        max_sample_files=20,
        max_bytes_per_file=2_000,
        max_total_sample_bytes=8_000,
    )
    inventory = {file.path for file in snapshot.files}
    sample_paths = {sample.path for sample in snapshot.samples}
    matches = search_text(workspace=str(tmp_path), pattern="M2_SECRET")

    assert "important.log" in inventory
    assert "important.log" not in sample_paths
    assert "ignored.log" not in inventory
    assert "ignored.log" not in sample_paths
    assert "build/cache.py" not in inventory
    assert ".venv/local.py" not in inventory
    assert [match.path for match in matches] == ["important.log"]


class _PromptCaptureClient:
    def __init__(self) -> None:
        self.instructions = ""
        self.input_text = ""

    def create_initial_response(
        self,
        *,
        config: AgentConfig,
        instructions: str,
        input_text: str,
    ) -> dict[str, Any]:
        del config
        self.instructions = instructions
        self.input_text = input_text
        return {
            "id": "response-1",
            "output_text": "Captured repository instructions.",
            "output": [],
        }

    def create_tool_response(
        self,
        *,
        config: AgentConfig,
        previous_response_id: str,
        tool_outputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        del config, previous_response_id, tool_outputs
        raise AssertionError("No tool response is expected in this test.")


def test_root_agents_md_is_injected_into_system_instructions(tmp_path: Path) -> None:
    rule = "M2_ROOT_RULE: Always inspect tests before editing."
    (tmp_path / "AGENTS.md").write_text(f"# Rules\n\n{rule}\n", encoding="utf-8")
    client = _PromptCaptureClient()

    answer = run_agent("Inspect the project.", _config(tmp_path), model_client=client)

    assert answer == "Captured repository instructions."
    assert rule in client.instructions
    assert rule not in client.input_text


def test_nested_agents_md_only_applies_to_its_directory_tree(tmp_path: Path) -> None:
    from coding_agent.instructions import (
        discover_agent_instructions,
        instructions_for_path,
    )

    (tmp_path / "AGENTS.md").write_text("root rule\n", encoding="utf-8")
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "AGENTS.md").write_text("src rule\n", encoding="utf-8")
    (tmp_path / "src" / "api" / "AGENTS.md").write_text(
        "api rule\n",
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()

    instructions = discover_agent_instructions(tmp_path)
    api_rules = instructions_for_path(instructions, "src/api/routes.py")
    docs_rules = instructions_for_path(instructions, "docs/guide.md")

    assert [rule.path for rule in api_rules] == [
        "AGENTS.md",
        "src/AGENTS.md",
        "src/api/AGENTS.md",
    ]
    assert [rule.content.strip() for rule in api_rules] == [
        "root rule",
        "src rule",
        "api rule",
    ]
    assert [rule.path for rule in docs_rules] == ["AGENTS.md"]


def test_read_many_files_preserves_order_and_enforces_all_budgets(
    tmp_path: Path,
) -> None:
    from coding_agent.reader import read_many_files

    (tmp_path / "a.txt").write_text("abcdefghij", encoding="utf-8")
    (tmp_path / "b.txt").write_text("klmnopqrst", encoding="utf-8")
    (tmp_path / "c.txt").write_text("uvwxyz", encoding="utf-8")

    results = read_many_files(
        str(tmp_path),
        ["a.txt", "missing.txt", "b.txt", "c.txt"],
        max_files=3,
        max_bytes_per_file=8,
        max_total_bytes=12,
    )

    assert [result.path for result in results] == [
        "a.txt",
        "missing.txt",
        "b.txt",
        "c.txt",
    ]
    assert results[0].ok is True
    assert results[0].content == "abcdefgh"
    assert results[0].truncated is True
    assert results[1].ok is False
    assert results[1].error
    assert results[2].ok is True
    assert results[2].content == "klmn"
    assert results[2].truncated is True
    assert results[3].ok is False
    assert results[3].error
    assert "limit" in results[3].error.lower()
    assert sum(
        len(result.content.encode("utf-8")) for result in results if result.ok
    ) <= 12


def test_search_prefers_rg_and_python_fallback_returns_the_same_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text(
        "def refund():\n    return True\n",
        encoding="utf-8",
    )
    rg_events = [
        {
            "type": "match",
            "data": {
                "path": {"text": "src/service.py"},
                "lines": {"text": "def refund():\n"},
                "line_number": 1,
                "absolute_offset": 0,
                "submatches": [
                    {
                        "match": {"text": "refund"},
                        "start": 4,
                        "end": 10,
                    }
                ],
            },
        }
    ]
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(args: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(json.dumps(event) for event in rg_events),
            stderr="",
        )

    fake_shutil = SimpleNamespace(which=lambda name: "C:/tools/rg.exe" if name == "rg" else None)
    fake_subprocess = SimpleNamespace(run=fake_run)
    monkeypatch.setattr(search_module, "shutil", fake_shutil, raising=False)
    monkeypatch.setattr(search_module, "subprocess", fake_subprocess, raising=False)

    rg_matches = search_text(workspace=str(tmp_path), pattern="refund", path="src")
    assert len(calls) == 1
    assert calls[0][1].get("shell") is False

    fake_shutil.which = lambda _name: None
    fallback_matches = search_text(workspace=str(tmp_path), pattern="refund", path="src")

    def identity(matches: list[Any]) -> list[tuple[str, int, int]]:
        return [(match.path, match.line, match.column) for match in matches]

    assert identity(rg_matches) == identity(fallback_matches) == [
        ("src/service.py", 1, 5)
    ]


def test_initial_inventory_is_ranked_truncated_and_reports_omissions(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("notes\n", encoding="utf-8")
    (tmp_path / "zeta.txt").write_text("zeta\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "refund_service.py").write_text(
        "def calculate_refund():\n    return 0\n",
        encoding="utf-8",
    )

    snapshot = collect_workspace_snapshot(
        str(tmp_path),
        task="find the refund calculation",
        max_inventory_files=2,
        max_sample_files=2,
        max_bytes_per_file=1_000,
        max_total_sample_bytes=2_000,
    )
    formatted = format_snapshot(snapshot)

    assert [file.path for file in snapshot.files] == [
        "src/refund_service.py",
        "README.md",
    ]
    assert snapshot.total_file_count == 4
    assert snapshot.omitted_file_count == 2
    assert "showing 2 of 4 files; 2 omitted" in formatted
    assert "Use search_text" in formatted
    assert "read_many_files" in formatted


def test_only_explicitly_named_source_files_are_sampled(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text(
        "SERVICE_MARKER = True\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "other.py").write_text(
        "OTHER_MARKER = True\n",
        encoding="utf-8",
    )

    snapshot = collect_workspace_snapshot(
        str(tmp_path),
        task="inspect service.py",
        max_inventory_files=20,
        max_sample_files=2,
        max_bytes_per_file=1_000,
        max_total_sample_bytes=2_000,
    )

    assert [sample.path for sample in snapshot.samples] == [
        "src/service.py",
        "README.md",
    ]
    combined_content = "\n".join(sample.content for sample in snapshot.samples)
    assert "SERVICE_MARKER" in combined_content
    assert "OTHER_MARKER" not in combined_content


def test_initial_sample_total_byte_limit_is_strict(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("R" * 100, encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("G" * 100, encoding="utf-8")

    snapshot = collect_workspace_snapshot(
        str(tmp_path),
        task="read guide.md",
        max_inventory_files=20,
        max_sample_files=2,
        max_bytes_per_file=100,
        max_total_sample_bytes=17,
    )

    assert [sample.path for sample in snapshot.samples] == ["docs/guide.md"]
    assert sum(
        len(sample.content.encode("utf-8")) for sample in snapshot.samples
    ) == 17


def test_initial_context_has_fixed_sample_and_total_byte_budgets(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("# Medium project\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'medium-project'\n",
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    for index in range(12):
        (tmp_path / "src" / f"module_{index:02d}.py").write_text(
            f"# module {index}\n" + ("x = 1\n" * 2_000),
            encoding="utf-8",
        )
    target = tmp_path / "src" / "refund_service.py"
    target.write_text(
        "M2_TARGET_SOURCE_MUST_BE_READ_ON_DEMAND = True\n",
        encoding="utf-8",
    )

    snapshot = collect_workspace_snapshot(
        str(tmp_path),
        task="find the refund calculation",
        max_inventory_files=200,
        max_sample_files=M2_MAX_INITIAL_SAMPLES,
        max_bytes_per_file=16_000,
        max_total_sample_bytes=M2_MAX_INITIAL_CONTENT_BYTES,
    )

    assert len(snapshot.samples) <= M2_MAX_INITIAL_SAMPLES
    assert sum(
        len(sample.content.encode("utf-8")) for sample in snapshot.samples
    ) <= M2_MAX_INITIAL_CONTENT_BYTES
    assert "src/refund_service.py" in {file.path for file in snapshot.files}
    assert "src/refund_service.py" not in {sample.path for sample in snapshot.samples}

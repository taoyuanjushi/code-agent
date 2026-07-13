import json
from pathlib import Path

import pytest

import coding_agent.search as search_module
from coding_agent.tools import (
    MAX_COMMAND_TIMEOUT_MS,
    MAX_GLOB_PATTERNS,
    MAX_READ_BYTES_PER_FILE,
    MAX_READ_FILES,
    MAX_READ_TOTAL_BYTES,
    MAX_SEARCH_RESULTS,
    TOOL_DEFINITIONS,
    execute_tool,
)
from coding_agent.types import AgentConfig


def _config(tmp_path: Path, *, auto_approve_edits: bool = False) -> AgentConfig:
    return AgentConfig(
        workspace=str(tmp_path),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="workspace-write",
        auto_approve_commands=False,
        auto_approve_edits=auto_approve_edits,
        context_max_files=10,
        context_max_bytes_per_file=1000,
    )


def test_execute_tool_search_text(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("hello search\n", encoding="utf-8")

    result = execute_tool(_config(tmp_path), "search_text", json.dumps({"pattern": "search"}))

    assert result.ok is True
    assert "file.txt:1:7" in result.output


def test_write_file_is_not_advertised_and_cannot_write(tmp_path: Path) -> None:
    assert "write_file" not in {definition["name"] for definition in TOOL_DEFINITIONS}

    result = execute_tool(
        _config(tmp_path),
        "write_file",
        json.dumps({"path": "unsafe.txt", "content": "direct write"}),
    )

    assert result.ok is False
    assert "apply_patch" in result.output
    assert not (tmp_path / "unsafe.txt").exists()


def test_apply_patch_displays_complete_diff_before_approval(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    long_line = f"{'x' * 4500}DIFF_TAIL"
    patch = "\n".join(
        [
            "--- /dev/null",
            "+++ b/large.txt",
            "@@ -0,0 +1 @@",
            f"+{long_line}",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    result = execute_tool(_config(tmp_path), "apply_patch", json.dumps({"patch": patch}))
    console = capsys.readouterr().out

    assert result.ok is False
    assert "Change summary:" in console
    assert "Unified diff:" in console
    assert "DIFF_TAIL" in console
    assert "patch truncated" not in console
    assert not (tmp_path / "large.txt").exists()


def test_tool_schemas_expose_runtime_types_and_hard_limits() -> None:
    definitions = {item["name"]: item for item in TOOL_DEFINITIONS}
    read_file = definitions["read_file"]["parameters"]["properties"]
    read_many = definitions["read_many_files"]["parameters"]["properties"]
    search = definitions["search_text"]["parameters"]["properties"]
    run_command = definitions["run_command"]["parameters"]["properties"]

    assert read_file["max_bytes"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_READ_BYTES_PER_FILE,
        "description": "Maximum bytes to read. Defaults to 30000.",
    }
    assert read_many["paths"]["minItems"] == 1
    assert read_many["paths"]["maxItems"] == MAX_READ_FILES
    assert read_many["paths"]["items"]["minLength"] == 1
    assert read_many["max_files"]["maximum"] == MAX_READ_FILES
    assert read_many["max_bytes_per_file"]["maximum"] == MAX_READ_BYTES_PER_FILE
    assert read_many["max_total_bytes"]["maximum"] == MAX_READ_TOTAL_BYTES
    assert search["regex"]["type"] == "boolean"
    assert search["case_sensitive"]["type"] == "boolean"
    assert search["glob"]["type"] == "array"
    assert search["glob"]["maxItems"] == MAX_GLOB_PATTERNS
    assert search["max_results"]["maximum"] == MAX_SEARCH_RESULTS
    assert run_command["timeout_ms"]["type"] == "integer"
    assert run_command["timeout_ms"]["maximum"] == MAX_COMMAND_TIMEOUT_MS


def test_execute_tool_search_text_supports_regex_and_glob(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "feature.py").write_text("issue-42\n", encoding="utf-8")
    (tmp_path / "src" / "feature.txt").write_text("issue-43\n", encoding="utf-8")
    monkeypatch.setattr(search_module.shutil, "which", lambda _name: None)

    result = execute_tool(
        _config(tmp_path),
        "search_text",
        json.dumps(
            {
                "pattern": r"issue-\d+",
                "regex": True,
                "glob": ["*.py"],
                "max_results": 5,
            }
        ),
    )

    assert result.ok is True
    assert "src/feature.py:1:1" in result.output
    assert "feature.txt" not in result.output

@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, "paths must be a non-empty list of non-empty strings."),
        ({"paths": []}, "paths must be a non-empty list of non-empty strings."),
        ({"paths": "file.txt"}, "paths must be a non-empty list of non-empty strings."),
        ({"paths": [""]}, "paths must be a non-empty list of non-empty strings."),
        ({"paths": [1]}, "paths must be a non-empty list of non-empty strings."),
        (
            {"paths": [f"file-{index}.txt" for index in range(MAX_READ_FILES + 1)]},
            f"paths must contain at most {MAX_READ_FILES} items.",
        ),
    ],
)
def test_read_many_files_rejects_invalid_path_arrays(
    tmp_path: Path,
    arguments: dict[str, object],
    expected: str,
) -> None:
    result = execute_tool(
        _config(tmp_path),
        "read_many_files",
        json.dumps(arguments),
    )

    assert result.ok is False
    assert result.output == expected


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected"),
    [
        (
            "read_file",
            {"path": "file.txt", "max_bytes": "100"},
            "max_bytes must be a positive integer.",
        ),
        (
            "read_many_files",
            {"paths": ["file.txt"], "max_files": 0},
            "max_files must be a positive integer.",
        ),
        (
            "read_many_files",
            {"paths": ["file.txt"], "max_bytes_per_file": 1.5},
            "max_bytes_per_file must be a positive integer.",
        ),
        (
            "read_many_files",
            {"paths": ["file.txt"], "max_total_bytes": -1},
            "max_total_bytes must be a positive integer.",
        ),
        (
            "search_text",
            {"pattern": "needle", "max_results": True},
            "max_results must be a positive integer.",
        ),
        (
            "run_command",
            {"command": "echo ok", "timeout_ms": "1000"},
            "timeout_ms must be a positive integer.",
        ),
    ],
)
def test_numeric_arguments_reject_coercion_and_non_positive_values(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, object],
    expected: str,
) -> None:
    result = execute_tool(_config(tmp_path), tool_name, json.dumps(arguments))

    assert result.ok is False
    assert result.output == expected


@pytest.mark.parametrize(
    ("tool_name", "arguments", "label", "maximum"),
    [
        (
            "read_file",
            {"path": "file.txt", "max_bytes": MAX_READ_BYTES_PER_FILE + 1},
            "max_bytes",
            MAX_READ_BYTES_PER_FILE,
        ),
        (
            "read_many_files",
            {"paths": ["file.txt"], "max_files": MAX_READ_FILES + 1},
            "max_files",
            MAX_READ_FILES,
        ),
        (
            "read_many_files",
            {
                "paths": ["file.txt"],
                "max_bytes_per_file": MAX_READ_BYTES_PER_FILE + 1,
            },
            "max_bytes_per_file",
            MAX_READ_BYTES_PER_FILE,
        ),
        (
            "read_many_files",
            {
                "paths": ["file.txt"],
                "max_total_bytes": MAX_READ_TOTAL_BYTES + 1,
            },
            "max_total_bytes",
            MAX_READ_TOTAL_BYTES,
        ),
        (
            "search_text",
            {"pattern": "needle", "max_results": MAX_SEARCH_RESULTS + 1},
            "max_results",
            MAX_SEARCH_RESULTS,
        ),
        (
            "run_command",
            {"command": "echo ok", "timeout_ms": MAX_COMMAND_TIMEOUT_MS + 1},
            "timeout_ms",
            MAX_COMMAND_TIMEOUT_MS,
        ),
    ],
)
def test_numeric_arguments_enforce_hard_maximums(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, object],
    label: str,
    maximum: int,
) -> None:
    result = execute_tool(_config(tmp_path), tool_name, json.dumps(arguments))

    assert result.ok is False
    assert result.output == f"{label} must be at most {maximum}."


@pytest.mark.parametrize("name", ["regex", "case_sensitive"])
def test_search_text_requires_actual_booleans(tmp_path: Path, name: str) -> None:
    result = execute_tool(
        _config(tmp_path),
        "search_text",
        json.dumps({"pattern": "needle", name: "false"}),
    )

    assert result.ok is False
    assert result.output == f"{name} must be a boolean."


@pytest.mark.parametrize("value", ["*.py", [1], [""]])
def test_search_text_rejects_invalid_glob_arrays(
    tmp_path: Path,
    value: object,
) -> None:
    result = execute_tool(
        _config(tmp_path),
        "search_text",
        json.dumps({"pattern": "needle", "glob": value}),
    )

    assert result.ok is False
    assert result.output == "glob must be a list of non-empty strings."


def test_search_text_rejects_too_many_glob_patterns(tmp_path: Path) -> None:
    result = execute_tool(
        _config(tmp_path),
        "search_text",
        json.dumps(
            {
                "pattern": "needle",
                "glob": [f"pattern-{index}" for index in range(MAX_GLOB_PATTERNS + 1)],
            }
        ),
    )

    assert result.ok is False
    assert result.output == f"glob must contain at most {MAX_GLOB_PATTERNS} items."


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("read_file", {"path": 1}),
        ("list_files", {"path": 1}),
        ("search_text", {"pattern": "needle", "path": 1}),
    ],
)
def test_path_arguments_reject_non_strings(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    result = execute_tool(_config(tmp_path), tool_name, json.dumps(arguments))

    assert result.ok is False
    assert "string argument: path" in result.output


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("read_file", {"path": "../outside.txt"}),
        ("list_files", {"path": ".."}),
        ("search_text", {"pattern": "needle", "path": ".."}),
    ],
)
def test_path_tools_reject_workspace_escape(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    result = execute_tool(_config(tmp_path), tool_name, json.dumps(arguments))

    assert result.ok is False
    assert "escapes workspace" in result.output


def test_invalid_tool_call_does_not_prevent_later_tool_calls(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("needle\n", encoding="utf-8")

    invalid = execute_tool(
        _config(tmp_path),
        "search_text",
        json.dumps({"pattern": "needle", "regex": "false"}),
    )
    valid = execute_tool(
        _config(tmp_path),
        "search_text",
        json.dumps({"pattern": "needle"}),
    )

    assert invalid.ok is False
    assert valid.ok is True
    assert "file.txt:1:1" in valid.output


import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.process_fakes import patch_tools_runner

import coding_agent.search as search_module
import coding_agent.tools as tools_module
from coding_agent.security.models import (
    MAX_COMMAND_ARGUMENTS,
    SECURITY_POLICY_VERSION,
    SandboxCapability,
)
from coding_agent.approvals import ApprovalRequest, create_approval_decision
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


def _config(
    tmp_path: Path,
    *,
    auto_approve_edits: bool = False,
    sandbox_mode: str = "none",
) -> AgentConfig:
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
        sandbox_mode=sandbox_mode,  # type: ignore[arg-type]
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
    run_command_schema = definitions["run_command"]["parameters"]
    assert "command" not in run_command
    assert run_command_schema["required"] == ["argv"]
    assert run_command["argv"] == {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_COMMAND_ARGUMENTS,
        "items": {"type": "string", "minLength": 1},
        "description": "Command executable and arguments. Each item is passed directly without a shell.",
    }
    assert run_command["cwd"] == {
        "type": "string",
        "minLength": 1,
        "description": "Optional workspace-relative working directory. Defaults to .",
    }
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
            {"argv": ["echo", "ok"], "timeout_ms": "1000"},
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
            {"argv": ["echo", "ok"], "timeout_ms": MAX_COMMAND_TIMEOUT_MS + 1},
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



def test_path_tools_reject_symlinked_directories(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle\n", encoding="utf-8")
    linked_directory = tmp_path / "linked"
    try:
        linked_directory.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable on this platform: {exc}")

    listed = execute_tool(
        _config(tmp_path),
        "list_files",
        json.dumps({"path": "linked"}),
    )
    searched = execute_tool(
        _config(tmp_path),
        "search_text",
        json.dumps({"pattern": "needle", "path": "linked"}),
    )

    assert listed.ok is False
    assert searched.ok is False
    assert "symlink or reparse" in listed.output
    assert "symlink or reparse" in searched.output


def test_apply_patch_revalidates_path_after_approval(tmp_path: Path) -> None:
    source_directory = tmp_path / "src"
    source_directory.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-patch-outside"
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

    def approve_then_swap(request: ApprovalRequest):
        source_directory.rmdir()
        try:
            source_directory.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"Symlink creation is unavailable on this platform: {exc}")
        return create_approval_decision(
            request,
            approved=True,
            source="interactive",
        )

    result = execute_tool(
        _config(tmp_path),
        "apply_patch",
        json.dumps({"patch": patch}),
        approval_handler=approve_then_swap,
    )

    assert result.ok is False
    assert "symlink or reparse" in result.output
    assert not (outside / "created.txt").exists()


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, "argv must be a non-empty list of non-empty strings."),
        ({"argv": []}, "argv must be a non-empty list of non-empty strings."),
        ({"argv": "echo ok"}, "argv must be a non-empty list of non-empty strings."),
        ({"argv": ["echo", ""]}, "argv must be a non-empty list of non-empty strings."),
        ({"argv": ["echo", 1]}, "argv must be a non-empty list of non-empty strings."),
        (
            {"argv": ["echo"] * (MAX_COMMAND_ARGUMENTS + 1)},
            f"argv must contain at most {MAX_COMMAND_ARGUMENTS} items.",
        ),
    ],
)
def test_run_command_rejects_invalid_argv(
    tmp_path: Path,
    arguments: dict[str, object],
    expected: str,
) -> None:
    result = execute_tool(_config(tmp_path), "run_command", json.dumps(arguments))

    assert result.ok is False
    assert result.output == expected


def test_run_command_rejects_legacy_shell_command_string(tmp_path: Path) -> None:
    result = execute_tool(
        _config(tmp_path),
        "run_command",
        json.dumps({"command": "echo legacy"}),
    )

    assert result.ok is False
    assert result.output == (
        'run_command no longer accepts "command"; provide a non-empty "argv" array.'
    )


def test_run_command_rejects_unknown_arguments_before_approval(tmp_path: Path) -> None:
    result = execute_tool(
        _config(tmp_path),
        "run_command",
        json.dumps({"argv": ["echo", "ok"], "unexpected": True}),
    )

    assert result.ok is False
    assert result.output == "Unexpected argument(s): unexpected"


def test_run_command_passes_original_argv_without_a_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    argv = [
        "echo",
        "argument with spaces",
        'quoted "text"',
        "&&",
        "|",
        ">",
        "$(not-a-subshell)",
    ]

    def fake_run(received_argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured["argv"] = received_argv
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="safe\n", stderr="")

    patch_tools_runner(monkeypatch, fake_run)
    requests: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest):
        requests.append(request)
        return create_approval_decision(request, approved=True, source="interactive")

    result = execute_tool(
        _config(tmp_path),
        "run_command",
        json.dumps({"argv": argv, "timeout_ms": 1_234}),
        approval_handler=approve,
    )

    assert result.ok is True
    assert captured["argv"] == argv
    assert captured["shell"] is False
    assert captured["cwd"] == str(tmp_path.resolve())
    assert captured["timeout"] == 1.234
    assert len(requests) == 1
    assert requests[0].details["argv"] == tuple(argv)
    assert "command" not in requests[0].details
    assert result.data is not None
    assert result.data["type"] == "secure_command_result"
    assert result.data["argv"] == argv
    assert result.data["cwd"] == "."
    assert result.data["backend"] == "host"
    assert result.data["sandboxed"] is False
    assert result.data["shell"] is False
    assert result.data["timeout_ms"] == 1_234
    assert result.data["exit_code"] == 0
    assert result.data["timed_out"] is False
    assert isinstance(result.data["duration_ms"], int)


@pytest.mark.parametrize(
    ("argv", "status", "disposition", "rule_id"),
    [
        (
            ["git", "reset", "--hard", "HEAD"],
            "denied",
            "deny",
            "deny.destructive_git",
        ),
        (
            ["python", "-c", "print('unsafe')"],
            "sandbox_unavailable",
            "sandbox_required",
            "sandbox.inline_interpreter",
        ),
    ],
)
def test_run_command_policy_blocks_before_approval_and_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    status: str,
    disposition: str,
    rule_id: str,
) -> None:
    approval_calls = 0
    subprocess_calls = 0

    def forbidden_approval(_request: ApprovalRequest):
        nonlocal approval_calls
        approval_calls += 1
        raise AssertionError("blocked commands must not request approval")

    def forbidden_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal subprocess_calls
        subprocess_calls += 1
        raise AssertionError("blocked commands must not start subprocesses")

    patch_tools_runner(monkeypatch, forbidden_run)

    result = execute_tool(
        _config(tmp_path),
        "run_command",
        json.dumps({"argv": argv}),
        approval_handler=forbidden_approval,
    )

    assert result.ok is False
    assert approval_calls == 0
    assert subprocess_calls == 0
    assert result.data is not None
    assert result.data["type"] == "secure_command_result"
    assert result.data["status"] == status
    assert result.data["exit_code"] is None
    assert result.data["policy_version"] == SECURITY_POLICY_VERSION
    assert result.data["disposition"] == disposition
    assert result.data["rule_id"] == rule_id
    assert result.data["policy"] == {
        "schema_version": 1,
        "policy_version": SECURITY_POLICY_VERSION,
        "disposition": disposition,
        "rule_id": rule_id,
        "reasons": result.data["reasons"],
        "normalized_executable": result.data["normalized_executable"],
        "requires_approval": False,
        "requires_sandbox": disposition == "sandbox_required",
    }
    assert f"[{rule_id}]" in result.output


def test_auto_sandbox_fails_closed_when_docker_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_calls = 0
    approval_calls = 0

    class UnavailableDockerBackend:
        def __init__(self, *, image_reference: str) -> None:
            self.image_reference = image_reference

        def probe_capability(self, _workspace: str) -> SandboxCapability:
            return SandboxCapability(
                backend="docker",
                available=False,
                reason="Docker daemon is unavailable.",
                image_reference=self.image_reference,
                image_digest=None,
            )

    def forbidden_host_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal host_calls
        host_calls += 1
        raise AssertionError("sandbox-required commands must not fall back to host")

    def forbidden_approval(_request: ApprovalRequest):
        nonlocal approval_calls
        approval_calls += 1
        raise AssertionError("sandbox-required commands must not request approval")

    monkeypatch.setattr(tools_module, "DockerSandboxBackend", UnavailableDockerBackend)
    patch_tools_runner(monkeypatch, forbidden_host_run)

    result = execute_tool(
        _config(tmp_path, sandbox_mode="auto"),
        "run_command",
        json.dumps({"argv": ["python", "-c", "print('unsafe')"]}),
        approval_handler=forbidden_approval,
    )

    assert result.ok is False
    assert host_calls == 0
    assert approval_calls == 0
    assert result.data is not None
    assert result.data["type"] == "secure_command_result"
    assert result.data["status"] == "sandbox_unavailable"
    assert result.data["backend"] == "docker"
    assert result.data["sandboxed"] is False
    assert "Docker daemon is unavailable" in result.output


def test_run_command_uses_workspace_relative_cwd_and_rejects_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    captured_cwds: list[str] = []

    def fake_run(_argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured_cwds.append(str(kwargs["cwd"]))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    patch_tools_runner(monkeypatch, fake_run)

    def approve(request: ApprovalRequest):
        return create_approval_decision(request, approved=True, source="interactive")

    result = execute_tool(
        _config(tmp_path),
        "run_command",
        json.dumps({"argv": ["echo", "ok"], "cwd": "nested"}),
        approval_handler=approve,
    )
    escaped = execute_tool(
        _config(tmp_path),
        "run_command",
        json.dumps({"argv": ["echo", "ok"], "cwd": ".."}),
    )

    assert result.ok is True
    assert captured_cwds == [str(nested.resolve())]
    assert escaped.ok is False
    assert "escapes workspace" in escaped.output


def test_run_command_revalidates_cwd_after_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_directory = tmp_path / "command-cwd"
    command_directory.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-command-outside"
    outside.mkdir()

    def approve_then_swap(request: ApprovalRequest):
        command_directory.rmdir()
        try:
            command_directory.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"Symlink creation is unavailable on this platform: {exc}")
        return create_approval_decision(request, approved=True, source="interactive")

    def forbidden_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise AssertionError("unsafe cwd must be rejected before subprocess startup")

    patch_tools_runner(monkeypatch, forbidden_run)
    result = execute_tool(
        _config(tmp_path),
        "run_command",
        json.dumps({"argv": ["echo", "ok"], "cwd": "command-cwd"}),
        approval_handler=approve_then_swap,
    )

    assert result.ok is False
    assert "symlink or reparse" in result.output



def test_internal_git_tools_use_the_shared_argv_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        assert kwargs["shell"] is False
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    patch_tools_runner(monkeypatch, fake_run)

    status = execute_tool(_config(tmp_path), "git_status", "{}")
    diff = execute_tool(_config(tmp_path), "git_diff", "{}")

    assert status.ok is True
    assert diff.ok is True
    assert calls == [
        ["git", "status", "--short"],
        ["git", "diff", "--stat"],
        ["git", "diff"],
    ]

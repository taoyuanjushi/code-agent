"""Tests for unified verification discovery and safe argv execution."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.process_fakes import patch_verification_runner

import coding_agent.verification as verification
from coding_agent.verification import (
    VerificationDiscoveryResult,
    create_verification_command,
    discover_verification_commands,
    run_verification_command,
)


def _discovery(
    workspace: Path,
    *,
    available: bool = True,
) -> VerificationDiscoveryResult:
    reason = None if available else "Python module 'pytest' is not installed."
    command = create_verification_command(
        workspace=workspace,
        command_id="python:pytest",
        kind="test",
        argv=(sys.executable, "-m", "pytest", "-q"),
        source="tests/",
        available=available,
        unavailable_reason=reason,
    )
    return VerificationDiscoveryResult(
        workspace=str(workspace.resolve()),
        commands=(command,),
    )


def test_unified_discovery_merges_python_and_node_commands_stably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        """
[build-system]
requires = ["setuptools"]

[tool.ruff]
""",
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        """{
  "packageManager": "npm@10",
  "scripts": {"test": "node --test", "lint": "eslint .", "build": "tsc"}
}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(verification, "_python_module_available", lambda _name: True)
    monkeypatch.setattr(verification, "_executable_available", lambda _name: True)

    result = discover_verification_commands(tmp_path)

    assert [command.id for command in result.commands] == [
        "node:test",
        "python:pytest",
        "node:lint",
        "python:ruff",
        "node:build",
        "python:build",
    ]
    assert len({command.id for command in result.commands}) == len(result.commands)
    assert all(command.cwd == str(tmp_path.resolve()) for command in result.commands)
    assert result.warnings == ()
    assert result.errors == ()


def test_unified_discovery_keeps_partial_results_warnings_and_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[invalid", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "node --test"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(verification, "_executable_available", lambda _name: True)

    result = discover_verification_commands(tmp_path)

    assert [command.id for command in result.commands] == ["node:test"]
    assert result.warnings == (
        "No packageManager field or lockfile found; defaulting to npm.",
    )
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Failed to parse pyproject.toml:")



def test_runner_uses_unified_discovery_when_result_is_not_supplied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = _discovery(tmp_path)
    discovered_workspaces: list[Path] = []

    def fake_discover(workspace: str | Path) -> VerificationDiscoveryResult:
        discovered_workspaces.append(Path(workspace))
        return discovery

    monkeypatch.setattr(verification, "discover_verification_commands", fake_discover)
    patch_verification_runner(
        monkeypatch,
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="ok",
            stderr="",
        ),
    )

    result = run_verification_command(
        tmp_path,
        command_id="python:pytest",
    )

    assert result.status == "passed"
    assert discovered_workspaces == [tmp_path.resolve()]


def test_runner_rejects_discovery_from_another_workspace(
    tmp_path: Path,
) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()

    with pytest.raises(ValueError, match="workspace does not match"):
        run_verification_command(
            tmp_path,
            command_id="python:pytest",
            discovery=_discovery(other_workspace),
        )

def test_runner_executes_only_discovered_argv_without_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = _discovery(tmp_path)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_run(argv: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="2 passed\n",
            stderr="warning\n",
        )

    patch_verification_runner(monkeypatch, fake_run)

    result = run_verification_command(
        tmp_path,
        command_id="python:pytest",
        discovery=discovery,
    )

    assert result.status == "passed"
    assert result.exit_code == 0
    assert result.argv == discovery.commands[0].argv
    assert result.output == "stdout: 2 passed\nstderr: warning"
    assert calls == [
        (
            discovery.commands[0].argv,
            {
                "cwd": discovery.commands[0].cwd,
                "shell": False,
                "stdin": subprocess.DEVNULL,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "capture_output": True,
                "timeout": 30.0,
            },
        )
    ]


def test_runner_reports_failed_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_verification_runner(
        monkeypatch,
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2,
            stdout="",
            stderr="collection failed",
        ),
    )

    result = run_verification_command(
        tmp_path,
        command_id="python:pytest",
        discovery=_discovery(tmp_path),
        attempt=2,
    )

    assert result.status == "failed"
    assert result.exit_code == 2
    assert result.attempt == 2
    assert result.output == "stderr: collection failed"


def test_runner_prioritizes_failure_context_and_stderr_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = "\n".join(
        [
            *(f"progress {index}" for index in range(30)),
            "ERROR src/refund.py:42 expected 20 but got 10",
            *(f"cleanup {index}" for index in range(30)),
        ]
    )
    patch_verification_runner(
        monkeypatch,
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout=stdout,
            stderr="warning\nfinal stderr diagnostic\n",
        ),
    )

    result = run_verification_command(
        tmp_path,
        command_id="python:pytest",
        discovery=_discovery(tmp_path),
        max_output_lines=6,
        max_output_bytes=512,
    )

    assert result.status == "failed"
    assert result.exit_code == 1
    assert "stdout: ERROR src/refund.py:42 expected 20 but got 10" in result.output
    assert "stderr: final stderr diagnostic" in result.output
    assert result.truncated is True
    assert result.omitted_lines > 0


def test_runner_keeps_passed_output_brief(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_verification_runner(
        monkeypatch,
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="\n".join(f"passed detail {index}" for index in range(100)),
            stderr="",
        ),
    )

    result = run_verification_command(
        tmp_path,
        command_id="python:pytest",
        discovery=_discovery(tmp_path),
        max_output_lines=200,
        max_output_bytes=32 * 1024,
    )

    assert result.status == "passed"
    assert len(result.output.splitlines()) <= 20
    assert len(result.output.encode("utf-8")) <= 4 * 1024
    assert "stdout: passed detail 0" in result.output
    assert "stdout: passed detail 99" in result.output
    assert result.truncated is True


def test_runner_rejects_unknown_command_id_without_starting_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess must not run")

    patch_verification_runner(monkeypatch, fail_run)

    with pytest.raises(ValueError, match="Unknown verification command id"):
        run_verification_command(
            tmp_path,
            command_id="python:arbitrary",
            discovery=_discovery(tmp_path),
        )


def test_runner_does_not_start_unavailable_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess must not run")

    patch_verification_runner(monkeypatch, fail_run)

    result = run_verification_command(
        tmp_path,
        command_id="python:pytest",
        discovery=_discovery(tmp_path, available=False),
    )

    assert result.status == "not_found"
    assert result.exit_code is None
    assert result.output == "Python module 'pytest' is not installed."


def test_runner_handles_runtime_not_found_and_startup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = _discovery(tmp_path)

    patch_verification_runner(
        monkeypatch,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            FileNotFoundError("missing runtime")
        ),
    )
    missing = run_verification_command(
        tmp_path,
        command_id="python:pytest",
        discovery=discovery,
    )

    patch_verification_runner(
        monkeypatch,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("cannot start")
        ),
    )
    errored = run_verification_command(
        tmp_path,
        command_id="python:pytest",
        discovery=discovery,
    )

    assert missing.status == "not_found"
    assert "missing runtime" in missing.output
    assert errored.status == "error"
    assert "cannot start" in errored.output


def test_runner_handles_timeout_and_limits_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def time_out(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(
            cmd=(sys.executable, "-m", "pytest"),
            timeout=0.01,
            output="first\nsecond\nthird\n",
            stderr="error tail",
        )

    patch_verification_runner(monkeypatch, time_out)

    result = run_verification_command(
        tmp_path,
        command_id="python:pytest",
        discovery=_discovery(tmp_path),
        timeout_ms=10,
        max_output_lines=2,
        max_output_bytes=30,
    )

    assert result.status == "timed_out"
    assert result.exit_code is None
    assert result.truncated is True
    assert result.omitted_lines > 0 or result.omitted_bytes > 0
    assert len(result.output.splitlines()) <= 2
    assert len(result.output.encode("utf-8")) <= 30


def test_runner_declined_approval_does_not_start_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess must not run")

    patch_verification_runner(monkeypatch, fail_run)

    result = run_verification_command(
        tmp_path,
        command_id="python:pytest",
        discovery=_discovery(tmp_path),
        approval_callback=lambda _command: False,
    )

    assert result.status == "error"
    assert result.output == "User declined verification command execution."


def test_runner_rechecks_cwd_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = _discovery(tmp_path)
    monkeypatch.setattr(
        verification,
        "resolve_inside_workspace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("Path escapes workspace")
        ),
    )

    result = run_verification_command(
        tmp_path,
        command_id="python:pytest",
        discovery=discovery,
    )

    assert result.status == "error"
    assert "Path escapes workspace" in result.output


@pytest.mark.parametrize(
    ("argument", "value", "message"),
    [
        ("timeout_ms", 0, "timeout_ms must be a positive integer"),
        ("max_output_bytes", 0, "max_output_bytes must be a positive integer"),
        ("max_output_lines", 0, "max_output_lines must be a positive integer"),
        ("attempt", 0, "attempt must be a positive integer"),
    ],
)
def test_runner_validates_execution_limits(
    tmp_path: Path,
    argument: str,
    value: int,
    message: str,
) -> None:
    kwargs = {argument: value}
    with pytest.raises(ValueError, match=message):
        run_verification_command(
            tmp_path,
            command_id="python:pytest",
            discovery=_discovery(tmp_path),
            **kwargs,
        )


def test_runner_preserves_unicode_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_verification_runner(
        monkeypatch,
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="???????????? ?\n",
            stderr="src/refund.py:8: ????? 20??? 10\n",
        ),
    )

    result = run_verification_command(
        tmp_path,
        command_id="python:pytest",
        discovery=_discovery(tmp_path),
    )

    assert result.status == "failed"
    assert "???????" in result.output
    assert "????? 20??? 10" in result.output
    result.output.encode("utf-8").decode("utf-8")


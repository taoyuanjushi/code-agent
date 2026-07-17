"""Tests for structured verification tool integration."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.process_fakes import patch_verification_runner

import coding_agent.verification as verification
import coding_agent.tools as tools_module
from coding_agent.security.models import SandboxCapability, SecureExecutionResult
from coding_agent.security.sandbox import SandboxExecutionOutcome
from coding_agent.tools import (
    TOOL_DEFINITIONS,
    VerificationToolState,
    execute_tool,
)
from coding_agent.types import AgentConfig
from coding_agent.verification import (
    MAX_VERIFICATION_OUTPUT_BYTES,
    MAX_VERIFICATION_OUTPUT_LINES,
    MAX_VERIFICATION_TIMEOUT_MS,
    VerificationDiscoveryResult,
    VerificationResult,
    VerificationStatus,
    create_verification_command,
)


def _config(
    tmp_path: Path,
    *,
    max_fix_attempts: int = 3,
    sandbox_mode: str = "none",
    sandbox_image_digest: str | None = None,
) -> AgentConfig:
    return AgentConfig(
        workspace=str(tmp_path),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=12,
        permission_mode="workspace-write",
        auto_approve_commands=True,
        auto_approve_edits=True,
        context_max_files=6,
        context_max_bytes_per_file=8_000,
        max_fix_attempts=max_fix_attempts,
        sandbox_mode=sandbox_mode,  # type: ignore[arg-type]
        sandbox_image_digest=sandbox_image_digest,
    )


def _python_project(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n[tool.ruff]\n",
        encoding="utf-8",
    )


def test_verification_state_rejects_unbounded_repair_limits() -> None:
    with pytest.raises(ValueError, match="between 0 and 10"):
        VerificationToolState(task="fix", max_fix_attempts=11)


def test_verification_tool_schemas_are_restricted() -> None:
    definitions = {item["name"]: item for item in TOOL_DEFINITIONS}
    discovery = definitions["discover_verification_commands"]["parameters"]
    runner = definitions["run_verification"]["parameters"]

    assert discovery["additionalProperties"] is False
    assert set(discovery["properties"]) == {"task"}
    assert runner["additionalProperties"] is False
    assert set(runner["properties"]) == {
        "command_id",
        "timeout_ms",
        "max_output_bytes",
        "max_output_lines",
    }
    assert runner["required"] == ["command_id"]
    assert runner["properties"]["timeout_ms"]["maximum"] == MAX_VERIFICATION_TIMEOUT_MS
    assert runner["properties"]["max_output_bytes"]["maximum"] == MAX_VERIFICATION_OUTPUT_BYTES
    assert runner["properties"]["max_output_lines"]["maximum"] == MAX_VERIFICATION_OUTPUT_LINES
    assert "argv" not in runner["properties"]


def test_discover_verification_tool_returns_ranked_structured_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _python_project(tmp_path)
    monkeypatch.setattr(verification, "_python_module_available", lambda _name: True)
    state = VerificationToolState(task="Fix lint failures", max_fix_attempts=3)

    result = execute_tool(
        _config(tmp_path),
        "discover_verification_commands",
        json.dumps({}),
        state=state,
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["type"] == "verification_discovery"
    commands = result.data["commands"]
    assert isinstance(commands, list)
    assert [command["id"] for command in commands] == [
        "python:ruff",
        "python:pytest",
    ]
    assert commands[0]["reason"] == "task mentions lint"
    assert state.discovery is not None
    assert "python:ruff" in result.output


def test_run_verification_tool_records_failed_structured_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _python_project(tmp_path)
    monkeypatch.setattr(verification, "_python_module_available", lambda _name: True)
    patch_verification_runner(
        monkeypatch,
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="progress\nERROR src/refund.py:17 expected 20\n",
            stderr="final failure diagnostic\n",
        ),
    )
    state = VerificationToolState(task="Fix failing tests", max_fix_attempts=3)
    execute_tool(
        _config(tmp_path),
        "discover_verification_commands",
        "{}",
        state=state,
    )

    result = execute_tool(
        _config(tmp_path),
        "run_verification",
        json.dumps({"command_id": "python:pytest"}),
        state=state,
    )

    assert result.ok is False
    assert result.data is not None
    assert result.data["type"] == "secure_command_result"
    assert result.data["status"] == "failed"
    assert result.data["exit_code"] == 1
    assert result.data["attempt"] == 1
    assert result.data["repair_limit_reached"] is False
    assert "stdout: ERROR src/refund.py:17 expected 20" in result.output
    assert state.unresolved_failure_command_id == "python:pytest"
    assert [item.status for item in state.verification_history] == ["failed"]


def test_run_verification_rejects_injected_argv_and_unknown_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _python_project(tmp_path)
    monkeypatch.setattr(verification, "_python_module_available", lambda _name: True)

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess must not run")

    patch_verification_runner(monkeypatch, fail_run)
    state = VerificationToolState(task="test", max_fix_attempts=3)

    injected = execute_tool(
        _config(tmp_path),
        "run_verification",
        json.dumps(
            {
                "command_id": "python:pytest",
                "argv": [sys.executable, "-c", "print('unsafe')"],
            }
        ),
        state=state,
    )
    unknown = execute_tool(
        _config(tmp_path),
        "run_verification",
        json.dumps({"command_id": "python:arbitrary"}),
        state=state,
    )

    assert injected.ok is False
    assert injected.output == "Unexpected argument(s): argv"
    assert unknown.ok is False
    assert "Unknown verification command id" in unknown.output


def test_inline_verification_is_blocked_before_approval_and_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = create_verification_command(
        workspace=tmp_path,
        command_id="python:inline",
        kind="test",
        argv=(sys.executable, "-c", "print('unsafe')"),
        source="fixture",
        available=True,
    )
    state = VerificationToolState(task="test", max_fix_attempts=3)
    state.discovery = VerificationDiscoveryResult(
        workspace=str(tmp_path.resolve()),
        commands=(command,),
    )
    approval_calls = 0
    subprocess_calls = 0

    def forbidden_approval(_request: object):
        nonlocal approval_calls
        approval_calls += 1
        raise AssertionError("sandbox-required verification must not request approval")

    def forbidden_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal subprocess_calls
        subprocess_calls += 1
        raise AssertionError("sandbox-required verification must not start subprocesses")

    patch_verification_runner(monkeypatch, forbidden_run)

    result = execute_tool(
        _config(tmp_path),
        "run_verification",
        json.dumps({"command_id": "python:inline"}),
        state=state,
        approval_handler=forbidden_approval,
    )

    assert result.ok is False
    assert approval_calls == 0
    assert subprocess_calls == 0
    assert result.data is not None
    assert result.data["type"] == "secure_command_result"
    assert result.data["status"] == "sandbox_unavailable"
    assert result.data["exit_code"] is None
    assert result.data["disposition"] == "sandbox_required"
    assert result.data["rule_id"] == "sandbox.inline_interpreter"
    assert result.data["requires_approval"] is False
    assert result.data["requires_sandbox"] is True
    assert result.data["policy"]["rule_id"] == "sandbox.inline_interpreter"
    assert "[sandbox.inline_interpreter]" in result.output


def test_sandbox_required_verification_routes_to_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "sha256:" + "a" * 64
    command = create_verification_command(
        workspace=tmp_path,
        command_id="python:inline",
        kind="test",
        argv=(sys.executable, "-c", "print('sandboxed')"),
        source="fixture",
        available=True,
    )


def _docker_outcome(config, command_spec, decision, digest: str):
    capability = SandboxCapability(
        backend="docker",
        available=True,
        reason=None,
        image_reference=config.sandbox_image,
        image_digest=digest,
    )
    return SandboxExecutionOutcome(
        result=SecureExecutionResult(
            command=command_spec,
            decision=decision,
            status="passed",
            backend="docker",
            sandboxed=True,
            image_digest=digest,
            exit_code=0,
            timed_out=False,
            duration_ms=12,
            output="sandboxed\n",
            output_truncated=False,
            omitted_lines=0,
            omitted_bytes=0,
            error_reason=None,
        ),
        capability=capability,
        snapshot_cleanup_succeeded=True,
    )
    state = VerificationToolState(task="test", max_fix_attempts=3)
    state.discovery = VerificationDiscoveryResult(
        workspace=str(tmp_path.resolve()),
        commands=(command,),
    )
    executions = []

    def fake_execute(
        config,
        command_spec,
        decision,
        **kwargs,
    ) -> SandboxExecutionOutcome:
        executions.append((config, command_spec, decision, kwargs))
        return _docker_outcome(config, command_spec, decision, digest)

    monkeypatch.setattr(tools_module, "_execute_docker_command", fake_execute)
    patch_verification_runner(
        monkeypatch,
        lambda *_args, **_kwargs: pytest.fail("host execution must not run"),
    )

    result = execute_tool(
        _config(
            tmp_path,
            sandbox_mode="docker",
            sandbox_image_digest=digest,
        ),
        "run_verification",
        json.dumps({"command_id": "python:inline"}),
        state=state,
        session_id="session-1",
        call_id="call-1",
    )

    assert len(executions) == 1
    assert result.ok is True
    assert result.data is not None
    assert result.data["type"] == "secure_command_result"
    assert result.data["status"] == "passed"
    assert result.data["verification_status"] == "passed"
    assert result.data["backend"] == "docker"
    assert result.data["sandboxed"] is True
    assert result.data["image_digest"] == digest
    assert result.data["network_mode"] == "none"


def test_explicit_docker_routes_allow_host_verification_to_stricter_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "sha256:" + "b" * 64
    _python_project(tmp_path)
    monkeypatch.setattr(verification, "_python_module_available", lambda _name: True)
    state = VerificationToolState(task="test", max_fix_attempts=3)
    execute_tool(
        _config(tmp_path),
        "discover_verification_commands",
        "{}",
        state=state,
    )
    executions = []

    def fake_execute(config, command_spec, decision, **kwargs):
        executions.append((decision, kwargs))
        return _docker_outcome(config, command_spec, decision, digest)

    monkeypatch.setattr(tools_module, "_execute_docker_command", fake_execute)
    patch_verification_runner(
        monkeypatch,
        lambda *_args, **_kwargs: pytest.fail("host execution must not run"),
    )

    result = execute_tool(
        _config(
            tmp_path,
            sandbox_mode="docker",
            sandbox_image_digest=digest,
        ),
        "run_verification",
        json.dumps({"command_id": "python:pytest"}),
        state=state,
    )

    assert executions[0][0].disposition == "allow_host"
    assert result.ok is True
    assert result.data is not None
    assert result.data["backend"] == "docker"
    assert result.data["sandboxed"] is True


def test_successful_verification_is_not_repeated_without_an_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _python_project(tmp_path)
    monkeypatch.setattr(verification, "_python_module_available", lambda _name: True)
    calls = 0

    def pass_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return SimpleNamespace(returncode=0, stdout="1 passed\n", stderr="")

    patch_verification_runner(monkeypatch, pass_run)
    state = VerificationToolState(task="test", max_fix_attempts=3)

    first = execute_tool(
        _config(tmp_path),
        "run_verification",
        json.dumps({"command_id": "python:pytest"}),
        state=state,
    )
    repeated = execute_tool(
        _config(tmp_path),
        "run_verification",
        json.dumps({"command_id": "python:pytest"}),
        state=state,
    )

    assert first.ok is True
    assert repeated.ok is False
    assert repeated.data == {
        "type": "verification_skipped",
        "command_id": "python:pytest",
        "reason": "already passed after the latest edit",
    }
    assert calls == 1


def test_repair_limit_blocks_additional_patch(tmp_path: Path) -> None:
    target = tmp_path / "value.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    state = VerificationToolState(task="fix", max_fix_attempts=1)
    state.unresolved_failure_command_id = "python:pytest"
    state.repair_attempts = 1
    patch = "\n".join(
        [
            "--- a/value.py",
            "+++ b/value.py",
            "@@ -1 +1 @@",
            "-VALUE = 1",
            "+VALUE = 2",
            "",
        ]
    )

    result = execute_tool(
        _config(tmp_path),
        "apply_patch",
        json.dumps({"patch": patch}),
        state=state,
    )

    assert result.ok is False
    assert result.data == {
        "type": "repair_limit_reached",
        "failed_command_id": "python:pytest",
        "repair_attempts": 1,
        "max_fix_attempts": 1,
    }
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


def _result(
    tmp_path: Path,
    *,
    command_id: str,
    status: VerificationStatus,
    exit_code: int | None,
    attempt: int = 1,
) -> VerificationResult:
    return VerificationResult(
        command_id=command_id,
        kind="test",
        status=status,
        argv=("python", "-m", "pytest"),
        cwd=str(tmp_path.resolve()),
        exit_code=exit_code,
        duration_ms=10,
        output=status,
        truncated=False,
        omitted_lines=0,
        omitted_bytes=0,
        attempt=attempt,
    )


@pytest.mark.parametrize("status", ["timed_out", "not_found", "error"])
def test_only_test_failures_activate_the_repair_path(
    tmp_path: Path,
    status: str,
) -> None:
    state = VerificationToolState(task="fix", max_fix_attempts=2)

    state.record_verification(
        _result(
            tmp_path,
            command_id="python:pytest",
            status=status,
            exit_code=None,
        )
    )
    state.record_patch_applied()

    assert state.unresolved_failure_command_id is None
    assert state.repair_attempts == 0
    assert state.repair_limit_reached is False


def test_switching_failed_commands_does_not_reset_the_repair_budget(
    tmp_path: Path,
) -> None:
    state = VerificationToolState(task="fix", max_fix_attempts=1)
    state.record_verification(
        _result(
            tmp_path,
            command_id="python:pytest",
            status="failed",
            exit_code=1,
        )
    )
    state.record_patch_applied()
    assert state.repair_attempts == 1

    state.record_verification(
        _result(
            tmp_path,
            command_id="python:ruff",
            status="failed",
            exit_code=1,
        )
    )

    assert state.unresolved_failure_command_id == "python:ruff"
    assert state.repair_attempts == 1
    assert state.repair_limit_reached is True


def test_execution_failure_clears_the_automatic_repair_path(
    tmp_path: Path,
) -> None:
    state = VerificationToolState(task="fix", max_fix_attempts=2)
    state.record_verification(
        _result(
            tmp_path,
            command_id="python:pytest",
            status="failed",
            exit_code=1,
        )
    )
    state.record_patch_applied()

    state.record_verification(
        _result(
            tmp_path,
            command_id="python:pytest",
            status="timed_out",
            exit_code=None,
            attempt=2,
        )
    )

    assert state.unresolved_failure_command_id is None
    assert state.repair_attempts == 0
    assert state.repair_limit_reached is False


def test_repair_loop_counts_patches_and_stops_after_the_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _python_project(tmp_path)
    target = tmp_path / "value.py"
    target.write_text("VALUE = 0\n", encoding="utf-8")
    monkeypatch.setattr(verification, "_python_module_available", lambda _name: True)
    patch_verification_runner(
        monkeypatch,
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="tests/test_value.py:4: AssertionError\n",
            stderr="1 failed\n",
        ),
    )
    config = _config(tmp_path, max_fix_attempts=1)
    state = VerificationToolState(task="fix value", max_fix_attempts=1)

    initial_patch = execute_tool(
        config,
        "apply_patch",
        json.dumps({"patch": _value_patch(0, 1)}),
        state=state,
    )
    first_failure = execute_tool(
        config,
        "run_verification",
        json.dumps({"command_id": "python:pytest"}),
        state=state,
    )
    repair_patch = execute_tool(
        config,
        "apply_patch",
        json.dumps({"patch": _value_patch(1, 2)}),
        state=state,
    )
    second_failure = execute_tool(
        config,
        "run_verification",
        json.dumps({"command_id": "python:pytest"}),
        state=state,
    )
    blocked_patch = execute_tool(
        config,
        "apply_patch",
        json.dumps({"patch": _value_patch(2, 3)}),
        state=state,
    )

    assert initial_patch.ok is True
    assert initial_patch.data is not None
    assert initial_patch.data["type"] == "patch_applied"
    assert initial_patch.data["changed_paths"] == ["value.py"]
    assert initial_patch.data["edit_generation"] == 1
    assert initial_patch.data["failed_command_id"] is None
    assert initial_patch.data["repair_attempts"] == 0
    assert initial_patch.data["max_fix_attempts"] == 1
    assert initial_patch.data["repair_limit_reached"] is False
    assert initial_patch.data["file_changes"][0]["before_sha256"]
    assert initial_patch.data["file_changes"][0]["after_sha256"]
    assert first_failure.data is not None
    assert first_failure.data["active_failure_command_id"] == "python:pytest"
    assert first_failure.data["repair_attempts"] == 0
    assert repair_patch.ok is True
    assert repair_patch.data is not None
    assert repair_patch.data["repair_attempts"] == 1
    assert second_failure.data is not None
    assert second_failure.data["repair_limit_reached"] is True
    assert blocked_patch.ok is False
    assert blocked_patch.data == {
        "type": "repair_limit_reached",
        "failed_command_id": "python:pytest",
        "repair_attempts": 1,
        "max_fix_attempts": 1,
    }
    assert [result.status for result in state.verification_history] == [
        "failed",
        "failed",
    ]
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"


def _value_patch(old: int, new: int) -> str:
    return "\n".join(
        [
            "--- a/value.py",
            "+++ b/value.py",
            "@@ -1 +1 @@",
            f"-VALUE = {old}",
            f"+VALUE = {new}",
            "",
        ]
    )


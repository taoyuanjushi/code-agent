from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from coding_agent.verification import (
    VERIFICATION_KINDS,
    VERIFICATION_STATUSES,
    VerificationCommand,
    VerificationDiscoveryResult,
    VerificationResult,
    classify_verification_status,
    create_verification_command,
)


def _command(
    workspace: Path,
    *,
    command_id: str = "python:pytest",
    available: bool = True,
) -> VerificationCommand:
    return create_verification_command(
        workspace=workspace,
        command_id=command_id,
        kind="test",
        argv=("python", "-m", "pytest", "-q"),
        source="pyproject.toml",
        available=available,
        unavailable_reason=None if available else "pytest is not installed",
    )


def _result(
    tmp_path: Path,
    *,
    status: str,
    exit_code: int | None,
) -> VerificationResult:
    return VerificationResult(
        command_id="python:pytest",
        kind="test",
        status=status,  # type: ignore[arg-type]
        argv=("python", "-m", "pytest", "-q"),
        cwd=str(tmp_path.resolve()),
        exit_code=exit_code,
        duration_ms=125,
        output="verification summary",
        truncated=False,
        omitted_lines=0,
        omitted_bytes=0,
        attempt=1,
    )


def test_create_verification_command_normalizes_workspace_cwd(tmp_path: Path) -> None:
    target = tmp_path / "packages" / "api"
    target.mkdir(parents=True)

    command = create_verification_command(
        workspace=tmp_path,
        command_id="python:pyproject.toml:pytest",
        kind="test",
        argv=("python", "-m", "pytest", "-q"),
        cwd="packages/api",
        source="packages/api/pyproject.toml",
        available=True,
    )

    assert command.cwd == str(target.resolve())
    assert command.argv == ("python", "-m", "pytest", "-q")
    assert command.source == "packages/api/pyproject.toml"
    assert command.unavailable_reason is None


def test_create_verification_command_rejects_workspace_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes workspace"):
        create_verification_command(
            workspace=tmp_path,
            command_id="python:pytest",
            kind="test",
            argv=("python", "-m", "pytest"),
            cwd="..",
            source="pyproject.toml",
            available=True,
        )


def test_verification_command_requires_tuple_argv_and_absolute_cwd(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match="argv must be a tuple"):
        VerificationCommand(
            id="python:pytest",
            kind="test",
            argv=["python", "-m", "pytest"],  # type: ignore[arg-type]
            cwd=str(tmp_path.resolve()),
            source="pyproject.toml",
            available=True,
        )

    with pytest.raises(ValueError, match="cwd must be an absolute path"):
        VerificationCommand(
            id="python:pytest",
            kind="test",
            argv=("python", "-m", "pytest"),
            cwd=".",
            source="pyproject.toml",
            available=True,
        )


def test_verification_command_requires_stable_id_and_unavailable_reason(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="stable namespace"):
        VerificationCommand(
            id="pytest",
            kind="test",
            argv=("python", "-m", "pytest"),
            cwd=str(tmp_path.resolve()),
            source="pyproject.toml",
            available=True,
        )

    with pytest.raises(ValueError, match="unavailable_reason"):
        VerificationCommand(
            id="python:pytest",
            kind="test",
            argv=("python", "-m", "pytest"),
            cwd=str(tmp_path.resolve()),
            source="pyproject.toml",
            available=False,
        )

    unavailable = _command(tmp_path, available=False)
    assert unavailable.available is False
    assert unavailable.unavailable_reason == "pytest is not installed"


def test_discovery_result_is_immutable_and_rejects_duplicate_ids(
    tmp_path: Path,
) -> None:
    command = _command(tmp_path)
    discovery = VerificationDiscoveryResult(
        workspace=str(tmp_path.resolve()),
        commands=(command,),
        warnings=("root configuration only",),
    )

    assert discovery.commands == (command,)
    assert discovery.errors == ()
    with pytest.raises(FrozenInstanceError):
        discovery.commands = ()  # type: ignore[misc]

    with pytest.raises(ValueError, match="Duplicate verification command id"):
        VerificationDiscoveryResult(
            workspace=str(tmp_path.resolve()),
            commands=(command, command),
        )


def test_discovery_result_rejects_command_cwd_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    command = VerificationCommand(
        id="python:pytest",
        kind="test",
        argv=("python", "-m", "pytest"),
        cwd=str(outside.resolve()),
        source="pyproject.toml",
        available=True,
    )

    with pytest.raises(ValueError, match="escapes workspace"):
        VerificationDiscoveryResult(
            workspace=str(workspace.resolve()),
            commands=(command,),
        )


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"exit_code": 0}, "passed"),
        ({"exit_code": 1}, "failed"),
        ({"timed_out": True}, "timed_out"),
        ({"not_found": True}, "not_found"),
        ({"execution_error": True}, "error"),
    ],
)
def test_classify_verification_status_covers_all_terminal_states(
    arguments: dict[str, object],
    expected: str,
) -> None:
    assert classify_verification_status(**arguments) == expected  # type: ignore[arg-type]


def test_classify_verification_status_rejects_conflicting_signals() -> None:
    with pytest.raises(ValueError, match="Only one exceptional"):
        classify_verification_status(timed_out=True, not_found=True)

    with pytest.raises(ValueError, match="cannot include an exit code"):
        classify_verification_status(exit_code=1, timed_out=True)

    with pytest.raises(ValueError, match="exit_code is required"):
        classify_verification_status()


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [
        ("passed", 0),
        ("failed", 1),
        ("timed_out", None),
        ("not_found", None),
        ("error", None),
    ],
)
def test_verification_result_accepts_all_terminal_states(
    tmp_path: Path,
    status: str,
    exit_code: int | None,
) -> None:
    result = _result(tmp_path, status=status, exit_code=exit_code)

    assert result.status == status
    assert result.exit_code == exit_code
    assert result.output == "verification summary"


def test_verification_result_rejects_inconsistent_status_and_truncation(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="passed results"):
        _result(tmp_path, status="passed", exit_code=1)

    with pytest.raises(ValueError, match="non-zero exit_code"):
        _result(tmp_path, status="failed", exit_code=0)

    with pytest.raises(ValueError, match="must not include an exit_code"):
        _result(tmp_path, status="timed_out", exit_code=1)

    with pytest.raises(ValueError, match="require truncated=True"):
        VerificationResult(
            command_id="python:pytest",
            kind="test",
            status="failed",
            argv=("python", "-m", "pytest"),
            cwd=str(tmp_path.resolve()),
            exit_code=1,
            duration_ms=1,
            output="summary",
            truncated=False,
            omitted_lines=10,
            omitted_bytes=100,
            attempt=1,
        )


def test_verification_result_contains_summary_fields_but_no_raw_logs() -> None:
    field_names = {field.name for field in fields(VerificationResult)}

    assert {
        "output",
        "truncated",
        "omitted_lines",
        "omitted_bytes",
    } <= field_names
    assert "stdout" not in field_names
    assert "stderr" not in field_names
    assert "raw_output" not in field_names
    assert VERIFICATION_KINDS == {"test", "lint", "typecheck", "build"}
    assert VERIFICATION_STATUSES == {
        "passed",
        "failed",
        "timed_out",
        "not_found",
        "error",
    }

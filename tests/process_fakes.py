from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

import coding_agent.tools as tools_module
import coding_agent.verification as verification_module
from coding_agent.security.process_runner import HostProcessResult


def patch_verification_runner(monkeypatch, legacy_run: Callable[..., object]) -> None:
    def fake_host_process(
        workspace,
        command,
        _decision,
        *,
        approval_granted: bool = False,
        environment=None,
    ) -> HostProcessResult:
        del approval_granted, environment
        cwd = str((Path(workspace).resolve() / command.cwd).resolve())
        try:
            completed = legacy_run(
                command.argv,
                cwd=cwd,
                shell=False,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=command.limits.timeout_ms / 1000,
            )
        except subprocess.TimeoutExpired as exc:
            return _result(
                command,
                cwd=cwd,
                status="timed_out",
                stdout=_text(exc.stdout if exc.stdout is not None else exc.output),
                stderr=_text(exc.stderr),
                exit_code=None,
            )
        except FileNotFoundError as exc:
            return _result(
                command,
                cwd=cwd,
                status="not_found",
                error_reason=str(exc),
                exit_code=None,
            )
        except (OSError, ValueError) as exc:
            return _result(
                command,
                cwd=cwd,
                status="error",
                error_reason=str(exc),
                exit_code=None,
            )
        return_code = completed.returncode
        return _result(
            command,
            cwd=cwd,
            status="passed" if return_code == 0 else "failed",
            stdout=_text(completed.stdout),
            stderr=_text(completed.stderr),
            exit_code=return_code,
        )

    monkeypatch.setattr(verification_module, "run_host_process", fake_host_process)


def patch_tools_runner(monkeypatch, legacy_run: Callable[..., object]) -> None:
    def fake_host_process(
        workspace,
        command,
        _decision,
        *,
        approval_granted: bool = False,
        environment=None,
    ) -> HostProcessResult:
        del approval_granted, environment
        cwd = str((Path(workspace).resolve() / command.cwd).resolve())
        try:
            completed = legacy_run(
                list(command.argv),
                cwd=cwd,
                shell=False,
                text=True,
                capture_output=True,
                timeout=command.limits.timeout_ms / 1000,
            )
        except subprocess.TimeoutExpired as exc:
            return _result(
                command,
                cwd=cwd,
                status="timed_out",
                stdout=_text(exc.stdout if exc.stdout is not None else exc.output),
                stderr=_text(exc.stderr),
                exit_code=None,
            )
        except FileNotFoundError as exc:
            return _result(
                command,
                cwd=cwd,
                status="not_found",
                error_reason=str(exc),
                exit_code=None,
            )
        except (OSError, ValueError) as exc:
            return _result(
                command,
                cwd=cwd,
                status="error",
                error_reason=str(exc),
                exit_code=None,
            )
        return_code = completed.returncode
        return _result(
            command,
            cwd=cwd,
            status="passed" if return_code == 0 else "failed",
            stdout=_text(completed.stdout),
            stderr=_text(completed.stderr),
            exit_code=return_code,
        )

    monkeypatch.setattr(tools_module, "run_host_process", fake_host_process)


def _result(
    command,
    *,
    cwd: str,
    status: str,
    exit_code: int | None,
    stdout: str = "",
    stderr: str = "",
    error_reason: str | None = None,
) -> HostProcessResult:
    return HostProcessResult(
        status=status,
        argv=command.argv,
        cwd=cwd,
        actual_executable=command.argv[0],
        allowed_environment_keys=(),
        timeout_ms=command.limits.timeout_ms,
        duration_ms=1,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        output_truncated=False,
        omitted_lines=0,
        omitted_bytes=0,
        process_tree_terminated=True if status == "timed_out" else None,
        error_reason=error_reason,
    )


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)

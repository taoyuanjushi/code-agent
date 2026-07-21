from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

from .models import (
    SECURITY_SCHEMA_VERSION,
    CommandPolicyDecision,
    CommandSpec,
)

HostProcessStatus = Literal[
    "passed",
    "failed",
    "timed_out",
    "not_found",
    "error",
]

DEFAULT_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
    }
)
FORBIDDEN_ENV_KEYS = frozenset({"OPENAI_API_KEY"})
FORBIDDEN_ENV_SUFFIXES = ("_TOKEN", "_SECRET", "_PASSWORD")
_READ_CHUNK_BYTES = 8 * 1024
_TERMINATION_GRACE_SECONDS = 0.5
_TERMINATION_WAIT_SECONDS = 5.0


class HostProcessAuthorizationError(ValueError):
    """Raised when a process plan is not authorized for host execution."""


class ProcessLike(Protocol):
    pid: int
    returncode: int | None
    stdout: BinaryIO | None
    stderr: BinaryIO | None

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[..., ProcessLike]
TreeTerminator = Callable[
    [ProcessLike, Path, Mapping[str, str]],
    tuple[bool, str | None],
]


@dataclass(frozen=True)
class HostProcessResult:
    """Bounded, auditable result from one host process execution."""

    status: HostProcessStatus
    argv: tuple[str, ...]
    cwd: str
    actual_executable: str | None
    allowed_environment_keys: tuple[str, ...]
    timeout_ms: int
    duration_ms: int
    exit_code: int | None
    stdout: str
    stderr: str
    output_truncated: bool
    omitted_lines: int
    omitted_bytes: int
    process_tree_terminated: bool | None = None
    cleanup_error: str | None = None
    error_reason: str | None = None

    @property
    def timed_out(self) -> bool:
        return self.status == "timed_out"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SECURITY_SCHEMA_VERSION,
            "status": self.status,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "actual_executable": self.actual_executable,
            "allowed_environment_keys": list(self.allowed_environment_keys),
            "timeout_ms": self.timeout_ms,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "shell": False,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "output_truncated": self.output_truncated,
            "omitted_lines": self.omitted_lines,
            "omitted_bytes": self.omitted_bytes,
            "process_tree_terminated": self.process_tree_terminated,
            "cleanup_error": self.cleanup_error,
            "error_reason": self.error_reason,
        }


@dataclass
class _StreamCapture:
    storage_limit: int
    data: bytearray
    total_bytes: int = 0
    line_breaks: int = 0
    last_byte: int | None = None
    previous_was_cr: bool = False

    @classmethod
    def create(cls, storage_limit: int) -> _StreamCapture:
        return cls(storage_limit=storage_limit, data=bytearray())

    def feed(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        if len(self.data) < self.storage_limit:
            remaining = self.storage_limit - len(self.data)
            self.data.extend(chunk[:remaining])

        for byte in chunk:
            if self.previous_was_cr:
                if byte == 10:
                    self.previous_was_cr = False
                    self.last_byte = byte
                    continue
                self.previous_was_cr = False
            if byte == 13:
                self.line_breaks += 1
                self.previous_was_cr = True
            elif byte == 10:
                self.line_breaks += 1
            self.last_byte = byte

    @property
    def total_lines(self) -> int:
        if self.total_bytes == 0:
            return 0
        if self.last_byte in {10, 13}:
            return self.line_breaks
        return self.line_breaks + 1

    @property
    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


class HostProcessRunner:
    """Execute an already-authorized command with bounded host resources."""

    def __init__(
        self,
        *,
        popen_factory: ProcessFactory | None = None,
        tree_terminator: TreeTerminator | None = None,
        clock: Callable[[], float] | None = None,
        platform_name: str | None = None,
    ) -> None:
        self._popen_factory = popen_factory
        self._tree_terminator = tree_terminator
        self._clock = clock
        self._platform_name = platform_name

    def run(
        self,
        workspace: str | Path,
        command: CommandSpec,
        decision: CommandPolicyDecision,
        *,
        approval_granted: bool = False,
        environment: Mapping[str, str] | None = None,
    ) -> HostProcessResult:
        _validate_host_authorization(decision, approval_granted=approval_granted)
        if not isinstance(command, CommandSpec):
            raise TypeError("command must be a CommandSpec instance.")

        clock = self._clock or time.monotonic
        started = clock()
        child_environment = build_child_environment(environment)
        allowed_keys = tuple(sorted(child_environment))
        timeout_ms = command.limits.timeout_ms

        try:
            from ..path_safety import resolve_workspace_path

            workspace_root = Path(workspace).resolve(strict=True)
            resolved_cwd = resolve_workspace_path(
                workspace_root,
                command.cwd,
                operation="execute",
                allow_missing=False,
            )
        except (OSError, ValueError) as exc:
            return _error_result(
                command,
                cwd=str(Path(workspace).resolve()),
                actual_executable=None,
                allowed_environment_keys=allowed_keys,
                started=started,
                clock=clock,
                error_reason=str(exc) or "Command working directory is invalid.",
            )

        actual_executable = resolve_actual_executable(
            command.argv[0],
            cwd=resolved_cwd,
            environment=child_environment,
        )
        stdout_capture = _StreamCapture.create(command.limits.max_output_bytes)
        stderr_capture = _StreamCapture.create(command.limits.max_output_bytes)
        reader_errors: list[str] = []
        platform_name = self._platform_name or os.name
        popen_factory = self._popen_factory or subprocess.Popen
        popen_kwargs: dict[str, object] = {
            "cwd": str(resolved_cwd),
            "shell": False,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": child_environment,
        }
        if platform_name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0,
            )
        else:
            popen_kwargs["start_new_session"] = True

        try:
            process = popen_factory(list(command.argv), **popen_kwargs)
        except FileNotFoundError as exc:
            return _error_result(
                command,
                status="not_found",
                cwd=str(resolved_cwd),
                actual_executable=actual_executable,
                allowed_environment_keys=allowed_keys,
                started=started,
                clock=clock,
                error_reason=str(exc) or f"Runtime not found: {command.argv[0]}",
            )
        except (OSError, ValueError) as exc:
            return _error_result(
                command,
                cwd=str(resolved_cwd),
                actual_executable=actual_executable,
                allowed_environment_keys=allowed_keys,
                started=started,
                clock=clock,
                error_reason=str(exc) or "Failed to start command.",
            )

        readers = [
            _start_reader(process.stdout, stdout_capture, reader_errors, "stdout"),
            _start_reader(process.stderr, stderr_capture, reader_errors, "stderr"),
        ]
        timed_out = False
        process_tree_terminated: bool | None = None
        cleanup_error: str | None = None
        return_code: int | None = None
        try:
            return_code = process.wait(timeout=timeout_ms / 1000)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminator = self._tree_terminator or _default_tree_terminator(platform_name)
            process_tree_terminated, cleanup_error = terminator(
                process,
                resolved_cwd,
                child_environment,
            )
            try:
                process.wait(timeout=_TERMINATION_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=_TERMINATION_WAIT_SECONDS)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    process_tree_terminated = False
                    cleanup_error = _combine_errors(cleanup_error, str(exc))
        except (OSError, ValueError) as exc:
            try:
                process.kill()
            except OSError:
                pass
            _join_readers(readers, process)
            return _error_result(
                command,
                cwd=str(resolved_cwd),
                actual_executable=actual_executable,
                allowed_environment_keys=allowed_keys,
                started=started,
                clock=clock,
                error_reason=str(exc) or "Failed while waiting for command.",
            )
        except BaseException as exc:
            cleanup_error = _cleanup_interrupted_process(
                process,
                readers,
                resolved_cwd,
                child_environment,
                self._tree_terminator
                or _default_tree_terminator(platform_name),
            )
            if cleanup_error:
                exc.add_note(
                    "Additionally, interrupted process cleanup failed: "
                    + cleanup_error
                )
            raise

        _join_readers(readers, process)
        stdout, stderr, truncated, omitted_lines, omitted_bytes = _bounded_output(
            stdout_capture,
            stderr_capture,
            max_bytes=command.limits.max_output_bytes,
            max_lines=command.limits.max_output_lines,
        )
        if reader_errors:
            return HostProcessResult(
                status="error",
                argv=command.argv,
                cwd=str(resolved_cwd),
                actual_executable=actual_executable,
                allowed_environment_keys=allowed_keys,
                timeout_ms=timeout_ms,
                duration_ms=_duration_ms(started, clock),
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                output_truncated=truncated,
                omitted_lines=omitted_lines,
                omitted_bytes=omitted_bytes,
                process_tree_terminated=process_tree_terminated,
                cleanup_error=cleanup_error,
                error_reason="; ".join(reader_errors),
            )

        if timed_out:
            return HostProcessResult(
                status="timed_out",
                argv=command.argv,
                cwd=str(resolved_cwd),
                actual_executable=actual_executable,
                allowed_environment_keys=allowed_keys,
                timeout_ms=timeout_ms,
                duration_ms=_duration_ms(started, clock),
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                output_truncated=truncated,
                omitted_lines=omitted_lines,
                omitted_bytes=omitted_bytes,
                process_tree_terminated=process_tree_terminated,
                cleanup_error=cleanup_error,
            )

        if return_code is None:
            return_code = process.returncode
        if return_code is None:
            return HostProcessResult(
                status="error",
                argv=command.argv,
                cwd=str(resolved_cwd),
                actual_executable=actual_executable,
                allowed_environment_keys=allowed_keys,
                timeout_ms=timeout_ms,
                duration_ms=_duration_ms(started, clock),
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                output_truncated=truncated,
                omitted_lines=omitted_lines,
                omitted_bytes=omitted_bytes,
                error_reason="Command completed without an exit code.",
            )

        return HostProcessResult(
            status="passed" if return_code == 0 else "failed",
            argv=command.argv,
            cwd=str(resolved_cwd),
            actual_executable=actual_executable,
            allowed_environment_keys=allowed_keys,
            timeout_ms=timeout_ms,
            duration_ms=_duration_ms(started, clock),
            exit_code=return_code,
            stdout=stdout,
            stderr=stderr,
            output_truncated=truncated,
            omitted_lines=omitted_lines,
            omitted_bytes=omitted_bytes,
        )


def run_host_process(
    workspace: str | Path,
    command: CommandSpec,
    decision: CommandPolicyDecision,
    *,
    approval_granted: bool = False,
    environment: Mapping[str, str] | None = None,
) -> HostProcessResult:
    """Execute a command through the shared default host runner."""

    return HostProcessRunner().run(
        workspace,
        command,
        decision,
        approval_granted=approval_granted,
        environment=environment,
    )


def build_child_environment(
    environment: Mapping[str, str] | None = None,
    *,
    allowlist: frozenset[str] = DEFAULT_ENV_ALLOWLIST,
) -> dict[str, str]:
    """Build a new child environment from an explicit, secret-safe allowlist."""

    source = os.environ if environment is None else environment
    allowed = {key.upper() for key in allowlist}
    child: dict[str, str] = {}
    for key, value in source.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        normalized = key.upper()
        if normalized not in allowed or _is_forbidden_environment_key(normalized):
            continue
        if "\x00" in key or "\x00" in value or "=" in key:
            continue
        child[normalized] = value
    return child


def resolve_actual_executable(
    executable: str,
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> str | None:
    """Resolve the executable path used for audit metadata without changing argv."""

    candidate = Path(executable)
    if candidate.is_absolute():
        return str(candidate.resolve(strict=False))
    if candidate.parent != Path("."):
        return str((cwd / candidate).resolve(strict=False))
    resolved = shutil.which(
        executable,
        path=environment.get("PATH", os.defpath),
    )
    if resolved is None:
        return None
    return str(Path(resolved).resolve(strict=False))


def _validate_host_authorization(
    decision: CommandPolicyDecision,
    *,
    approval_granted: bool,
) -> None:
    if not isinstance(decision, CommandPolicyDecision):
        raise TypeError("decision must be a CommandPolicyDecision instance.")
    if decision.disposition == "allow_host":
        return
    if decision.disposition == "approval_required" and approval_granted is True:
        return
    if decision.disposition == "approval_required":
        raise HostProcessAuthorizationError(
            "Host command requires a completed approval before process creation."
        )
    raise HostProcessAuthorizationError(
        f"Command disposition {decision.disposition!r} is not authorized for host execution."
    )


def _is_forbidden_environment_key(key: str) -> bool:
    return key in FORBIDDEN_ENV_KEYS or key.endswith(FORBIDDEN_ENV_SUFFIXES)


def _start_reader(
    pipe: BinaryIO | None,
    capture: _StreamCapture,
    errors: list[str],
    label: str,
) -> threading.Thread | None:
    if pipe is None:
        errors.append(f"Process {label} pipe was not created.")
        return None

    def drain() -> None:
        try:
            while True:
                chunk = pipe.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                capture.feed(chunk)
        except (OSError, ValueError) as exc:
            errors.append(f"Failed to read process {label}: {exc}")
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    thread = threading.Thread(target=drain, name=f"host-process-{label}", daemon=True)
    thread.start()
    return thread


def _cleanup_interrupted_process(
    process: ProcessLike,
    readers: list[threading.Thread | None],
    cwd: Path,
    environment: Mapping[str, str],
    terminator: TreeTerminator,
) -> str | None:
    cleanup_error: str | None = None
    try:
        terminated, termination_error = terminator(process, cwd, environment)
        cleanup_error = termination_error
        if not terminated and cleanup_error is None:
            cleanup_error = "process tree termination was not confirmed"
    except BaseException as exc:
        cleanup_error = f"{type(exc).__name__}: {exc}"

    try:
        process.wait(timeout=_TERMINATION_WAIT_SECONDS)
    except BaseException as exc:
        cleanup_error = _combine_errors(cleanup_error, str(exc))
        try:
            process.kill()
            process.wait(timeout=_TERMINATION_WAIT_SECONDS)
        except BaseException as kill_exc:
            cleanup_error = _combine_errors(cleanup_error, str(kill_exc))
    try:
        _join_readers(readers, process)
    except BaseException as exc:
        cleanup_error = _combine_errors(cleanup_error, str(exc))
    return cleanup_error


def _join_readers(
    readers: list[threading.Thread | None],
    process: ProcessLike,
) -> None:
    for reader in readers:
        if reader is not None:
            reader.join(timeout=_TERMINATION_WAIT_SECONDS)
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def _bounded_output(
    stdout_capture: _StreamCapture,
    stderr_capture: _StreamCapture,
    *,
    max_bytes: int,
    max_lines: int,
) -> tuple[str, str, bool, int, int]:
    total_bytes = stdout_capture.total_bytes + stderr_capture.total_bytes
    total_lines = stdout_capture.total_lines + stderr_capture.total_lines
    stdout = stdout_capture.text
    stderr = stderr_capture.text

    storage_complete = (
        stdout_capture.total_bytes <= len(stdout_capture.data)
        and stderr_capture.total_bytes <= len(stderr_capture.data)
    )
    if storage_complete and total_bytes <= max_bytes and total_lines <= max_lines:
        return stdout, stderr, False, 0, 0

    if stdout and stderr:
        stderr_byte_reserve = max(1, max_bytes // 3)
        stderr_line_reserve = max(1, max_lines // 3)
    else:
        stderr_byte_reserve = max_bytes if stderr else 0
        stderr_line_reserve = max_lines if stderr else 0

    limited_stderr = _limit_text(
        stderr,
        max_bytes=stderr_byte_reserve,
        max_lines=stderr_line_reserve,
    )
    limited_stdout = _limit_text(
        stdout,
        max_bytes=max(0, max_bytes - _encoded_length(limited_stderr)),
        max_lines=max(0, max_lines - _count_text_lines(limited_stderr)),
    )
    limited_stderr = _limit_text(
        stderr,
        max_bytes=max(0, max_bytes - _encoded_length(limited_stdout)),
        max_lines=max(0, max_lines - _count_text_lines(limited_stdout)),
    )
    limited_stdout = _limit_text(
        stdout,
        max_bytes=max(0, max_bytes - _encoded_length(limited_stderr)),
        max_lines=max(0, max_lines - _count_text_lines(limited_stderr)),
    )

    returned_bytes = _encoded_length(limited_stdout) + _encoded_length(limited_stderr)
    returned_lines = _count_text_lines(limited_stdout) + _count_text_lines(limited_stderr)
    omitted_bytes = max(0, total_bytes - returned_bytes)
    omitted_lines = max(0, total_lines - returned_lines)
    return (
        limited_stdout,
        limited_stderr,
        bool(omitted_bytes or omitted_lines),
        omitted_lines,
        omitted_bytes,
    )


def _limit_text(value: str, *, max_bytes: int, max_lines: int) -> str:
    if not value or max_bytes <= 0 or max_lines <= 0:
        return ""
    lines = value.splitlines(keepends=True)
    if not lines:
        lines = [value]
    selected = "".join(lines[:max_lines])
    encoded = selected.encode("utf-8")
    if len(encoded) <= max_bytes:
        return selected
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _count_text_lines(value: str) -> int:
    if not value:
        return 0
    return len(value.splitlines()) or 1


def _encoded_length(value: str) -> int:
    return len(value.encode("utf-8"))


def _default_tree_terminator(platform_name: str) -> TreeTerminator:
    return _terminate_windows_process_tree if platform_name == "nt" else _terminate_posix_process_group


def _terminate_posix_process_group(
    process: ProcessLike,
    _cwd: Path,
    _environment: Mapping[str, str],
) -> tuple[bool, str | None]:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, None
    except OSError as exc:
        return False, str(exc)

    try:
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
        return True, None
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(process.pid, signal.SIGKILL)
        return True, None
    except ProcessLookupError:
        return True, None
    except OSError as exc:
        return False, str(exc)


def _terminate_windows_process_tree(
    process: ProcessLike,
    cwd: Path,
    environment: Mapping[str, str],
) -> tuple[bool, str | None]:
    system_root = environment.get("SYSTEMROOT") or environment.get("WINDIR")
    taskkill = (
        str(Path(system_root) / "System32" / "taskkill.exe")
        if system_root
        else shutil.which(
            "taskkill",
            path=environment.get("PATH", os.defpath),
        )
        or "taskkill.exe"
    )
    try:
        cleanup = subprocess.Popen(
            [taskkill, "/PID", str(process.pid), "/T", "/F"],
            cwd=str(cwd),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(environment),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return_code = cleanup.wait(timeout=_TERMINATION_WAIT_SECONDS)
        if return_code == 0:
            return True, None
        return False, f"taskkill exited with code {return_code}."
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        try:
            process.kill()
        except OSError as kill_exc:
            return False, _combine_errors(str(exc), str(kill_exc))
        return False, str(exc)


def _error_result(
    command: CommandSpec,
    *,
    cwd: str,
    actual_executable: str | None,
    allowed_environment_keys: tuple[str, ...],
    started: float,
    clock: Callable[[], float],
    error_reason: str,
    status: HostProcessStatus = "error",
) -> HostProcessResult:
    return HostProcessResult(
        status=status,
        argv=command.argv,
        cwd=cwd,
        actual_executable=actual_executable,
        allowed_environment_keys=allowed_environment_keys,
        timeout_ms=command.limits.timeout_ms,
        duration_ms=_duration_ms(started, clock),
        exit_code=None,
        stdout="",
        stderr="",
        output_truncated=False,
        omitted_lines=0,
        omitted_bytes=0,
        error_reason=error_reason,
    )


def _duration_ms(started: float, clock: Callable[[], float]) -> int:
    return max(0, round((clock() - started) * 1000))


def _combine_errors(first: str | None, second: str | None) -> str | None:
    messages = [message for message in (first, second) if message]
    return "; ".join(messages) or None

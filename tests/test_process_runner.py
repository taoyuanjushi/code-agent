from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import coding_agent.security.process_runner as process_runner_module
from coding_agent.security.models import (
    CommandPolicyDecision,
    CommandSpec,
    ExecutionLimits,
)
from coding_agent.security.process_runner import (
    DEFAULT_ENV_ALLOWLIST,
    HostProcessAuthorizationError,
    HostProcessRunner,
    build_child_environment,
    resolve_actual_executable,
    _terminate_posix_process_group,
    _terminate_windows_process_tree,
)


def _command(
    *,
    argv: tuple[str, ...] | None = None,
    cwd: str = ".",
    timeout_ms: int = 30_000,
    max_output_bytes: int = 32 * 1024,
    max_output_lines: int = 200,
) -> CommandSpec:
    return CommandSpec(
        argv=argv or (sys.executable, "--version"),
        cwd=cwd,
        source="internal",
        purpose="process runner test",
        limits=ExecutionLimits(
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
            max_output_lines=max_output_lines,
        ),
    )


def _decision(disposition: str = "allow_host") -> CommandPolicyDecision:
    return CommandPolicyDecision(
        disposition=disposition,  # type: ignore[arg-type]
        rule_id=f"test.{disposition}",
        reasons=("test decision",),
        normalized_executable="python",
        requires_approval=disposition == "approval_required",
        requires_sandbox=disposition == "sandbox_required",
    )


def test_child_environment_uses_allowlist_and_drops_secrets() -> None:
    source = {
        "PATH": "safe-path",
        "PathExt": ".EXE;.CMD",
        "HOME": "home",
        "LANG": "en_US.UTF-8",
        "OPENAI_API_KEY": "sk-secret",
        "GITHUB_TOKEN": "token-secret",
        "CLIENT_SECRET": "client-secret",
        "DATABASE_PASSWORD": "password-secret",
        "UNRELATED": "not-allowed",
    }

    child = build_child_environment(source)

    assert child == {
        "PATH": "safe-path",
        "PATHEXT": ".EXE;.CMD",
        "HOME": "home",
        "LANG": "en_US.UTF-8",
    }
    assert set(child) <= DEFAULT_ENV_ALLOWLIST
    assert not set(source.values()) - {
        "safe-path",
        ".EXE;.CMD",
        "home",
        "en_US.UTF-8",
    } & set(child.values())


def test_real_child_process_does_not_inherit_disallowed_environment(
    tmp_path: Path,
) -> None:
    argv = (
        sys.executable,
        "-c",
        (
            "import json, os; "
            "print(json.dumps({key: os.environ.get(key) for key in "
            "['HOME', 'OPENAI_API_KEY', 'GITHUB_TOKEN', 'UNRELATED']}))"
        ),
    )

    result = HostProcessRunner().run(
        tmp_path,
        _command(argv=argv),
        _decision(),
        environment={
            "HOME": "safe-home",
            "OPENAI_API_KEY": "sk-secret",
            "GITHUB_TOKEN": "token-secret",
            "UNRELATED": "not-allowed",
        },
    )

    assert result.status == "passed"
    assert json.loads(result.stdout) == {
        "HOME": "safe-home",
        "OPENAI_API_KEY": None,
        "GITHUB_TOKEN": None,
        "UNRELATED": None,
    }
    assert result.allowed_environment_keys == ("HOME",)


def test_executable_audit_does_not_fall_back_to_host_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str | None] = {}

    def fake_which(executable: str, *, path: str | None = None) -> None:
        captured["executable"] = executable
        captured["path"] = path
        return None

    monkeypatch.setattr(
        "coding_agent.security.process_runner.shutil.which",
        fake_which,
    )

    resolved = resolve_actual_executable(
        "missing-runtime",
        cwd=tmp_path,
        environment={},
    )

    assert resolved is None
    assert captured == {
        "executable": "missing-runtime",
        "path": os.defpath,
    }


def test_runner_uses_popen_without_shell_and_records_metadata(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 1234
        returncode = 0
        stdout = io.BytesIO(b"safe output\n")
        stderr = io.BytesIO(b"warning\n")

        def wait(self, timeout: float | None = None) -> int:
            captured["wait_timeout"] = timeout
            return 0

        def kill(self) -> None:
            raise AssertionError("successful process must not be killed")

    def fake_popen(argv: list[str], **kwargs: object) -> FakeProcess:
        captured["argv"] = argv
        captured.update(kwargs)
        return FakeProcess()

    runner = HostProcessRunner(popen_factory=fake_popen, platform_name="posix")
    result = runner.run(
        tmp_path,
        _command(cwd="nested"),
        _decision(),
        environment={
            "PATH": os.environ.get("PATH", ""),
            "HOME": "safe-home",
            "OPENAI_API_KEY": "secret",
        },
    )

    assert result.status == "passed"
    assert result.stdout == "safe output\n"
    assert result.stderr == "warning\n"
    assert result.cwd == str(nested.resolve())
    assert result.actual_executable == str(Path(sys.executable).resolve())
    assert result.allowed_environment_keys == ("HOME", "PATH")
    assert result.to_dict()["shell"] is False
    assert result.to_dict()["allowed_environment_keys"] == ["HOME", "PATH"]
    assert captured["argv"] == [sys.executable, "--version"]
    assert captured["cwd"] == str(nested.resolve())
    assert captured["shell"] is False
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE
    assert captured["start_new_session"] is True
    assert captured["env"] == {"PATH": os.environ.get("PATH", ""), "HOME": "safe-home"}
    assert "OPENAI_API_KEY" not in result.to_dict()


def test_windows_runner_uses_process_group_flags_without_shell(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 1234
        returncode = 0
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            raise AssertionError("successful process must not be killed")

    def fake_popen(_argv: list[str], **kwargs: object) -> FakeProcess:
        captured.update(kwargs)
        return FakeProcess()

    result = HostProcessRunner(
        popen_factory=fake_popen,
        platform_name="nt",
    ).run(tmp_path, _command(), _decision())

    assert result.status == "passed"
    assert captured["shell"] is False
    assert captured["creationflags"] == getattr(
        subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        0,
    )
    assert "start_new_session" not in captured


def test_posix_tree_terminator_targets_the_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = []

    class Process:
        pid = 4321
        returncode = None
        stdout = None
        stderr = None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            raise AssertionError("process-group termination should be used")

    monkeypatch.setattr(
        process_runner_module.os,
        "killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
        raising=False,
    )

    assert _terminate_posix_process_group(Process(), tmp_path, {}) == (True, None)
    assert signals == [(4321, process_runner_module.signal.SIGTERM)]


def test_windows_tree_terminator_uses_taskkill_tree_and_force_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class CleanupProcess:
        def wait(self, timeout: float | None = None) -> int:
            captured["timeout"] = timeout
            return 0

    class Process:
        pid = 9876
        returncode = None
        stdout = None
        stderr = None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            raise AssertionError("taskkill success must not use fallback kill")

    def fake_popen(argv: list[str], **kwargs: object) -> CleanupProcess:
        captured["argv"] = argv
        captured.update(kwargs)
        return CleanupProcess()

    monkeypatch.setattr(process_runner_module.subprocess, "Popen", fake_popen)

    assert _terminate_windows_process_tree(
        Process(),
        tmp_path,
        {"SYSTEMROOT": r"C:\Windows"},
    ) == (True, None)
    assert captured["argv"] == [
        str(Path(r"C:\Windows") / "System32" / "taskkill.exe"),
        "/PID",
        "9876",
        "/T",
        "/F",
    ]
    assert captured["shell"] is False


@pytest.mark.parametrize("disposition", ["deny", "sandbox_required"])
def test_runner_rejects_non_host_policy_before_popen(
    tmp_path: Path,
    disposition: str,
) -> None:
    calls = 0

    def forbidden_popen(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("unauthorized command must not start")

    runner = HostProcessRunner(popen_factory=forbidden_popen)

    with pytest.raises(HostProcessAuthorizationError):
        runner.run(tmp_path, _command(), _decision(disposition))

    assert calls == 0


def test_runner_requires_completed_approval_before_popen(tmp_path: Path) -> None:
    calls = 0

    class FakeProcess:
        pid = 10
        returncode = 0
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            return None

    def fake_popen(*_args: object, **_kwargs: object) -> FakeProcess:
        nonlocal calls
        calls += 1
        return FakeProcess()

    runner = HostProcessRunner(popen_factory=fake_popen)
    decision = _decision("approval_required")

    with pytest.raises(HostProcessAuthorizationError):
        runner.run(tmp_path, _command(), decision)
    approved = runner.run(
        tmp_path,
        _command(),
        decision,
        approval_granted=True,
    )

    assert approved.status == "passed"
    assert calls == 1


def test_runner_bounds_combined_output_and_records_omissions(tmp_path: Path) -> None:
    argv = (
        sys.executable,
        "-c",
        "import sys; print('x' * 200); print('err-1', file=sys.stderr); print('err-2', file=sys.stderr)",
    )

    result = HostProcessRunner().run(
        tmp_path,
        _command(
            argv=argv,
            max_output_bytes=64,
            max_output_lines=2,
        ),
        _decision(),
    )

    assert result.status == "passed"
    assert len((result.stdout + result.stderr).encode("utf-8")) <= 64
    assert len(result.stdout.splitlines()) + len(result.stderr.splitlines()) <= 2
    assert result.output_truncated is True
    assert result.omitted_bytes > 0
    assert result.omitted_lines > 0


def test_runner_timeout_uses_tree_terminator_and_returns_partial_output(
    tmp_path: Path,
) -> None:
    wait_calls = 0
    terminated: list[int] = []

    class TimedOutProcess:
        pid = 4321
        returncode = None
        stdout = io.BytesIO(b"partial stdout\n")
        stderr = io.BytesIO(b"partial stderr\n")

        def wait(self, timeout: float | None = None) -> int:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                raise subprocess.TimeoutExpired(cmd="python", timeout=timeout or 0)
            self.returncode = -9
            return -9

        def kill(self) -> None:
            self.returncode = -9

    def terminate(process, _cwd, _environment):
        terminated.append(process.pid)
        return True, None

    runner = HostProcessRunner(
        popen_factory=lambda *_args, **_kwargs: TimedOutProcess(),
        tree_terminator=terminate,
    )
    result = runner.run(
        tmp_path,
        _command(timeout_ms=10),
        _decision(),
    )

    assert result.status == "timed_out"
    assert result.exit_code is None
    assert result.process_tree_terminated is True
    assert result.stdout == "partial stdout\n"
    assert result.stderr == "partial stderr\n"
    assert terminated == [4321]


def test_runner_reports_startup_failure_without_environment_values(tmp_path: Path) -> None:
    def missing(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("runtime missing")

    result = HostProcessRunner(popen_factory=missing).run(
        tmp_path,
        _command(argv=("missing-runtime", "--version")),
        _decision(),
        environment={"PATH": "safe", "OPENAI_API_KEY": "secret-value"},
    )

    assert result.status == "not_found"
    assert result.exit_code is None
    assert result.error_reason == "runtime missing"
    assert result.allowed_environment_keys == ("PATH",)
    assert "secret-value" not in repr(result.to_dict())

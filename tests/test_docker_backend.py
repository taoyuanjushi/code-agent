from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest

from coding_agent.security.docker_backend import (
    DEFAULT_DOCKER_TMPFS,
    DEFAULT_DOCKER_USER,
    DockerSandboxBackend,
    build_docker_container_name,
)
from coding_agent.security.models import (
    CommandPolicyDecision,
    CommandSpec,
    ExecutionLimits,
    SandboxCapability,
    SandboxExecutionPlan,
)
from coding_agent.security.process_runner import HostProcessResult
from coding_agent.security.sandbox import (
    SandboxAuthorizationError,
    SandboxBackend,
)

IMAGE_A = "sha256:" + "a" * 64
IMAGE_B = "sha256:" + "b" * 64
IMAGE_REFERENCE = "python:3.12-slim"


class ScriptedProcessRunner:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.commands: list[CommandSpec] = []
        self.decisions: list[CommandPolicyDecision] = []
        self.environments: list[Mapping[str, str] | None] = []

    def run(
        self,
        workspace,
        command,
        decision,
        *,
        approval_granted: bool = False,
        environment=None,
    ) -> HostProcessResult:
        assert approval_granted is False
        self.commands.append(command)
        self.decisions.append(decision)
        self.environments.append(environment)
        if not self.responses:
            raise AssertionError(f"Unexpected Docker CLI call: {command.argv}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(Path(workspace), command)
        if not isinstance(response, Mapping):
            raise TypeError("Scripted response must be a mapping, exception, or callable.")

        status = str(response.get("status", "passed"))
        exit_code = response.get("exit_code")
        if "exit_code" not in response:
            exit_code = 0 if status == "passed" else 1 if status == "failed" else None
        return HostProcessResult(
            status=status,  # type: ignore[arg-type]
            argv=command.argv,
            cwd=str(Path(workspace).resolve()),
            actual_executable=(
                None if status == "not_found" else str(command.argv[0])
            ),
            allowed_environment_keys=(),
            timeout_ms=command.limits.timeout_ms,
            duration_ms=int(response.get("duration_ms", 5)),
            exit_code=exit_code,  # type: ignore[arg-type]
            stdout=str(response.get("stdout", "")),
            stderr=str(response.get("stderr", "")),
            output_truncated=bool(response.get("output_truncated", False)),
            omitted_lines=int(response.get("omitted_lines", 0)),
            omitted_bytes=int(response.get("omitted_bytes", 0)),
            process_tree_terminated=(True if status == "timed_out" else None),
            error_reason=(
                str(response["error_reason"])
                if response.get("error_reason") is not None
                else None
            ),
        )


def _version_response(os_name: str = "linux") -> Mapping[str, object]:
    return {"status": "passed", "stdout": json.dumps({"Os": os_name})}


def _inspect_response(
    digest: str = IMAGE_A,
    os_name: str = "linux",
) -> Mapping[str, object]:
    return {
        "status": "passed",
        "stdout": json.dumps([{"Id": digest, "Os": os_name}]),
    }


def _capability(digest: str = IMAGE_A) -> SandboxCapability:
    return SandboxCapability(
        backend="docker",
        available=True,
        reason=None,
        image_reference=IMAGE_REFERENCE,
        image_digest=digest,
    )


def _decision(disposition: str = "sandbox_required") -> CommandPolicyDecision:
    return CommandPolicyDecision(
        disposition=disposition,  # type: ignore[arg-type]
        rule_id=f"{disposition}.test",
        reasons=("Docker backend test decision.",),
        normalized_executable="python",
        requires_approval=disposition == "approval_required",
        requires_sandbox=disposition == "sandbox_required",
    )


def _plan(
    *,
    digest: str = IMAGE_A,
    cwd: str = ".",
    disposition: str = "sandbox_required",
) -> SandboxExecutionPlan:
    command = CommandSpec(
        argv=("python", "-m", "pytest", "-q"),
        cwd=cwd,
        source="verification",
        purpose="Run repository tests in Docker",
        limits=ExecutionLimits(
            timeout_ms=1_234,
            max_output_bytes=4_096,
            max_output_lines=40,
            memory_mb=384,
            pids_limit=32,
            cpus=1.5,
        ),
    )
    capability = _capability(digest)
    return SandboxExecutionPlan(
        command=command,
        decision=_decision(disposition),
        capability=capability,
        backend="docker",
        sandboxed=True,
        network_mode="none",
        image_digest=digest,
    )


def test_backend_satisfies_sandbox_protocol() -> None:
    assert isinstance(DockerSandboxBackend(), SandboxBackend)


def test_probe_uses_version_and_local_image_inspect_without_pull(
    tmp_path: Path,
) -> None:
    runner = ScriptedProcessRunner([_version_response(), _inspect_response()])
    backend = DockerSandboxBackend(process_runner=runner)

    capability = backend.probe_capability(tmp_path)

    assert capability == _capability()
    assert [command.argv for command in runner.commands] == [
        ("docker", "version", "--format", "{{json .Server}}"),
        ("docker", "image", "inspect", IMAGE_REFERENCE),
    ]
    assert all(command.source == "internal" for command in runner.commands)
    assert all(decision.disposition == "allow_host" for decision in runner.decisions)
    assert all("pull" not in command.argv for command in runner.commands)


@pytest.mark.parametrize(
    ("responses", "reason_fragment", "expected_calls"),
    [
        (
            [{"status": "not_found", "error_reason": "docker missing"}],
            "docker missing",
            1,
        ),
        (
            [_version_response("windows")],
            "only Linux containers",
            1,
        ),
        (
            [
                _version_response(),
                {"status": "failed", "stderr": "No such image"},
            ],
            "automatic pull is disabled",
            2,
        ),
        (
            [_version_response(), _inspect_response(os_name="windows")],
            "only Linux images",
            2,
        ),
        (
            [_version_response(), _inspect_response(digest="not-pinned")],
            "pinned sha256 digest",
            2,
        ),
    ],
)
def test_probe_fails_closed_for_unavailable_or_unsupported_docker(
    tmp_path: Path,
    responses: list[Mapping[str, object]],
    reason_fragment: str,
    expected_calls: int,
) -> None:
    runner = ScriptedProcessRunner(responses)
    capability = DockerSandboxBackend(process_runner=runner).probe_capability(
        tmp_path
    )

    assert capability.available is False
    assert capability.image_digest is None
    assert reason_fragment in (capability.reason or "")
    assert len(runner.commands) == expected_calls
    assert all("pull" not in command.argv for command in runner.commands)


@pytest.mark.parametrize("disposition", ["sandbox_required", "allow_host"])
def test_execute_builds_hardened_argv_with_pinned_digest_and_filtered_snapshot(
    tmp_path: Path,
    disposition: str,
) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    runner = ScriptedProcessRunner(
        [
            _version_response(),
            _inspect_response(),
            {"status": "passed", "stdout": "3 passed\n"},
        ]
    )
    backend = DockerSandboxBackend(process_runner=runner)
    plan = _plan(cwd="src", disposition=disposition)
    environment = {
        "PATH": "host-path",
        "HOME": "host-home",
        "LANG": "C.UTF-8",
        "OPENAI_API_KEY": "secret-value",
    }
    security_events = []

    outcome = backend.execute(
        tmp_path,
        plan,
        session_id="session-1",
        call_id="call-1",
        environment=environment,
        event_handler=lambda event_type, payload: security_events.append(
            (event_type, payload)
        ),
    )

    snapshot_path = (
        tmp_path
        / ".coding-agent"
        / "sandboxes"
        / "session-1"
        / "call-1"
        / "workspace"
    )
    expected_argv = (
        "docker",
        "run",
        "--rm",
        "--name",
        "coding-agent-session-1-call-1",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--pids-limit",
        "32",
        "--memory",
        "384m",
        "--cpus",
        "1.5",
        "--user",
        DEFAULT_DOCKER_USER,
        "--tmpfs",
        DEFAULT_DOCKER_TMPFS,
        "--mount",
        f"type=bind,src={snapshot_path},dst=/workspace",
        "--workdir",
        "/workspace/src",
        "--env",
        "HOME=/tmp",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "LANG=C.UTF-8",
        IMAGE_A,
        "python",
        "-m",
        "pytest",
        "-q",
    )
    assert runner.commands[2].argv == expected_argv
    assert outcome.backend_argv == expected_argv
    assert IMAGE_REFERENCE not in expected_argv
    assert "secret-value" not in repr(outcome.to_dict())
    assert "host-home" not in repr(expected_argv)
    assert outcome.result.status == "passed"
    assert outcome.result.sandboxed is True
    assert outcome.result.image_digest == IMAGE_A
    assert outcome.result.output == "3 passed\n"
    assert outcome.snapshot_summary is not None
    assert outcome.snapshot_summary["file_count"] == 1
    assert outcome.snapshot_cleanup_succeeded is True
    assert not snapshot_path.parent.exists()
    assert [event_type for event_type, _payload in security_events] == [
        "sandbox.capability_checked",
        "sandbox.snapshot_created",
        "sandbox.started",
    ]
    assert security_events[1][1]["snapshot"]["file_count"] == 1
    assert security_events[2][1]["network_mode"] == "none"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX mode bits are not portable to Windows",
)
def test_execute_makes_only_disposable_snapshot_writable_by_non_root_user(
    tmp_path: Path,
) -> None:
    source = tmp_path / "private.txt"
    source.write_text("private input\n", encoding="utf-8")
    os.chmod(source, 0o600)

    def inspect_snapshot(
        _workspace: Path,
        command: CommandSpec,
    ) -> Mapping[str, object]:
        mount = command.argv[command.argv.index("--mount") + 1]
        mount_source = Path(
            mount.removeprefix("type=bind,src=").removesuffix(",dst=/workspace")
        )
        directory_mode = stat.S_IMODE(os.lstat(mount_source).st_mode)
        file_mode = stat.S_IMODE(os.lstat(mount_source / "private.txt").st_mode)
        assert directory_mode & 0o007 == 0o007
        assert file_mode & 0o006 == 0o006
        return {"status": "passed", "stdout": "ok\n"}

    runner = ScriptedProcessRunner(
        [_version_response(), _inspect_response(), inspect_snapshot]
    )
    outcome = DockerSandboxBackend(process_runner=runner).execute(
        tmp_path,
        _plan(),
        session_id="permission-session",
        call_id="permission-call",
    )

    assert outcome.result.status == "passed"
    assert stat.S_IMODE(os.lstat(source).st_mode) == 0o600
    assert outcome.snapshot_cleanup_succeeded is True


def test_sandbox_writes_never_modify_the_real_workspace(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("original\n", encoding="utf-8")

    def mutate_snapshot(
        _workspace: Path,
        command: CommandSpec,
    ) -> Mapping[str, object]:
        mount = command.argv[command.argv.index("--mount") + 1]
        snapshot = Path(
            mount.removeprefix("type=bind,src=").removesuffix(",dst=/workspace")
        )
        (snapshot / "app.py").write_text("mutated\n", encoding="utf-8")
        (snapshot / "generated.py").write_text("generated\n", encoding="utf-8")
        return {"status": "passed", "stdout": "changed snapshot\n"}

    runner = ScriptedProcessRunner(
        [_version_response(), _inspect_response(), mutate_snapshot]
    )

    outcome = DockerSandboxBackend(process_runner=runner).execute(
        tmp_path,
        _plan(),
        session_id="isolation-session",
        call_id="isolation-call",
    )

    assert outcome.result.status == "passed"
    assert source.read_text(encoding="utf-8") == "original\n"
    assert not (tmp_path / "generated.py").exists()
    assert outcome.snapshot_cleanup_succeeded is True


def test_execute_refuses_image_digest_drift_before_snapshot_or_docker_run(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    runner = ScriptedProcessRunner(
        [_version_response(), _inspect_response(digest=IMAGE_B)]
    )
    backend = DockerSandboxBackend(process_runner=runner)

    outcome = backend.execute(
        tmp_path,
        _plan(digest=IMAGE_A),
        session_id="drift-session",
        call_id="drift-call",
    )

    assert outcome.result.status == "sandbox_unavailable"
    assert "digest changed" in (outcome.result.error_reason or "")
    assert outcome.backend_argv == ()
    assert len(runner.commands) == 2
    assert not (
        tmp_path / ".coding-agent" / "sandboxes" / "drift-session"
    ).exists()


def test_timeout_force_removes_container_and_cleans_snapshot(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    runner = ScriptedProcessRunner(
        [
            _version_response(),
            _inspect_response(),
            {
                "status": "timed_out",
                "stdout": "partial output\n",
                "duration_ms": 1_234,
            },
            {"status": "passed"},
        ]
    )
    backend = DockerSandboxBackend(process_runner=runner)

    outcome = backend.execute(
        tmp_path,
        _plan(),
        session_id="timeout-session",
        call_id="timeout-call",
    )

    assert outcome.result.status == "timed_out"
    assert outcome.result.output == "partial output\n"
    assert runner.commands[3].argv == (
        "docker",
        "rm",
        "-f",
        "coding-agent-timeout-session-timeout-call",
    )
    assert outcome.container_cleanup_attempted is True
    assert outcome.container_cleanup_succeeded is True
    assert outcome.container_cleanup_error is None
    assert outcome.snapshot_cleanup_succeeded is True
    assert not (
        tmp_path / ".coding-agent" / "sandboxes" / "timeout-session" / "timeout-call"
    ).exists()


def test_cleanup_failure_is_audited_without_changing_command_status(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    runner = ScriptedProcessRunner(
        [
            _version_response(),
            _inspect_response(),
            {"status": "passed", "stdout": "ok\n"},
        ]
    )

    def failed_cleanup(_snapshot):
        from coding_agent.security.snapshot import SnapshotCleanupResult

        return SnapshotCleanupResult(
            removed=False,
            cleanup_error="simulated cleanup failure",
        )

    outcome = DockerSandboxBackend(
        process_runner=runner,
        snapshot_cleaner=failed_cleanup,
    ).execute(
        tmp_path,
        _plan(),
        session_id="cleanup-session",
        call_id="cleanup-call",
    )

    assert outcome.result.status == "passed"
    assert outcome.snapshot_cleanup_succeeded is False
    assert outcome.snapshot_cleanup_error == "simulated cleanup failure"


def test_approval_required_plan_is_rejected_before_capability_probe(
    tmp_path: Path,
) -> None:
    runner = ScriptedProcessRunner([])
    backend = DockerSandboxBackend(process_runner=runner)

    with pytest.raises(SandboxAuthorizationError):
        backend.execute(
            tmp_path,
            _plan(disposition="approval_required"),
            session_id="approval-session",
            call_id="approval-call",
        )

    assert runner.commands == []


@pytest.mark.parametrize(
    ("inspect_response", "expected"),
    [
        (
            {"status": "failed", "stderr": "Error: No such object: missing"},
            (False, True, None),
        ),
        (
            {"status": "passed", "stdout": "running"},
            (True, True, None),
        ),
    ],
)
def test_reconcile_interrupted_container_is_idempotent(
    tmp_path: Path,
    inspect_response: Mapping[str, object],
    expected: tuple[bool, bool, str | None],
) -> None:
    responses = [inspect_response]
    if inspect_response["status"] == "passed":
        responses.append({"status": "passed"})
    runner = ScriptedProcessRunner(responses)

    result = DockerSandboxBackend(
        process_runner=runner
    ).reconcile_interrupted_container(tmp_path, "coding-agent-session-call")

    assert result == expected
    assert runner.commands[0].argv[:3] == ("docker", "container", "inspect")


def test_container_name_is_deterministic_and_bounded() -> None:
    session_id = "s" * 128
    call_id = "c" * 128

    first = build_docker_container_name(session_id, call_id)
    second = build_docker_container_name(session_id, call_id)

    assert first == second
    assert first.startswith("coding-agent-")
    assert len(first) <= 128

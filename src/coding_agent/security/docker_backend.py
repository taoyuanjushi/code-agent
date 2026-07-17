from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Protocol

from ..path_safety import is_link_or_reparse_point
from .models import (
    CommandPolicyDecision,
    CommandSpec,
    ExecutionLimits,
    SandboxCapability,
    SandboxExecutionPlan,
    SecureExecutionResult,
)
from .process_runner import (
    HostProcessResult,
    HostProcessRunner,
    build_child_environment,
)
from .sandbox import (
    SandboxAuthorizationError,
    SandboxExecutionOutcome,
)
from .snapshot import (
    DEFAULT_SNAPSHOT_MAX_BINARY_FILE_BYTES,
    DEFAULT_SNAPSHOT_MAX_BYTES,
    DEFAULT_SNAPSHOT_MAX_FILES,
    SandboxWorkspaceSnapshot,
    SnapshotCleanupResult,
    cleanup_sandbox_workspace_snapshot,
    create_sandbox_workspace_snapshot,
)

DEFAULT_DOCKER_IMAGE = "python:3.12-slim"
DEFAULT_DOCKER_USER = "65532:65532"
DEFAULT_DOCKER_TMPFS = "/tmp:rw,nosuid,nodev,noexec,size=64m"
DOCKER_CONTAINER_ENV_ALLOWLIST = frozenset({"LANG", "LC_ALL"})
DOCKER_PROBE_TIMEOUT_MS = 10_000
DOCKER_CLEANUP_TIMEOUT_MS = 10_000

_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_REFERENCE_MAX_BYTES = 512
_CONTAINER_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CONTAINER_NAME_MAX_CHARS = 128
_REASON_MAX_CHARS = 500

_DOCKER_CLI_DECISION = CommandPolicyDecision(
    disposition="allow_host",
    rule_id="internal.docker-cli",
    reasons=("Execute backend-generated Docker CLI argv with shell disabled.",),
    normalized_executable="docker",
    requires_approval=False,
    requires_sandbox=False,
)


class DockerProcessRunner(Protocol):
    def run(
        self,
        workspace: str | Path,
        command: CommandSpec,
        decision: CommandPolicyDecision,
        *,
        approval_granted: bool = False,
        environment: Mapping[str, str] | None = None,
    ) -> HostProcessResult: ...


class SnapshotFactory(Protocol):
    def __call__(
        self,
        workspace: str | Path,
        *,
        session_id: str,
        call_id: str,
        max_files: int,
        max_bytes: int,
        max_binary_file_bytes: int,
    ) -> SandboxWorkspaceSnapshot: ...


SnapshotCleaner = Callable[[SandboxWorkspaceSnapshot], SnapshotCleanupResult]
SecurityEventHandler = Callable[[str, Mapping[str, object]], None]


class DockerSandboxBackend:
    """Run sandbox-required commands through a pinned local Docker image."""

    def __init__(
        self,
        image_reference: str = DEFAULT_DOCKER_IMAGE,
        *,
        docker_executable: str = "docker",
        process_runner: DockerProcessRunner | None = None,
        snapshot_factory: SnapshotFactory = create_sandbox_workspace_snapshot,
        snapshot_cleaner: SnapshotCleaner = cleanup_sandbox_workspace_snapshot,
        snapshot_max_files: int = DEFAULT_SNAPSHOT_MAX_FILES,
        snapshot_max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
        snapshot_max_binary_file_bytes: int = DEFAULT_SNAPSHOT_MAX_BINARY_FILE_BYTES,
    ) -> None:
        self.image_reference = _validate_image_reference(image_reference)
        self.docker_executable = _validate_docker_executable(docker_executable)
        self._process_runner = process_runner or HostProcessRunner()
        self._snapshot_factory = snapshot_factory
        self._snapshot_cleaner = snapshot_cleaner
        self._snapshot_max_files = _positive_int(
            snapshot_max_files,
            "snapshot_max_files",
        )
        self._snapshot_max_bytes = _positive_int(
            snapshot_max_bytes,
            "snapshot_max_bytes",
        )
        self._snapshot_max_binary_file_bytes = _positive_int(
            snapshot_max_binary_file_bytes,
            "snapshot_max_binary_file_bytes",
        )

    def probe_capability(
        self,
        workspace: str | Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> SandboxCapability:
        """Probe the local daemon and image without pulling or changing state."""

        version_command = self._control_command(
            (
                self.docker_executable,
                "version",
                "--format",
                "{{json .Server}}",
            ),
            purpose="Probe the local Docker daemon and container operating system",
            timeout_ms=DOCKER_PROBE_TIMEOUT_MS,
        )
        try:
            version_result = self._run_control(
                workspace,
                version_command,
                environment=environment,
            )
        except Exception as exc:
            return self._unavailable(
                f"Docker daemon probe failed: {_bounded_reason(exc)}"
            )
        if version_result.status != "passed":
            return self._unavailable(
                _process_failure_reason("Docker daemon probe", version_result)
            )

        try:
            server = _json_object(version_result.stdout, "Docker version output")
            server_os = _required_string_field(
                server,
                ("Os", "OS", "OSType"),
                "Docker server operating system",
            ).lower()
        except ValueError as exc:
            return self._unavailable(str(exc))
        if server_os != "linux":
            return self._unavailable(
                "Docker server uses unsupported container mode "
                f"{server_os!r}; only Linux containers are supported."
            )

        inspect_command = self._control_command(
            (
                self.docker_executable,
                "image",
                "inspect",
                self.image_reference,
            ),
            purpose="Inspect the configured local Docker image without pulling it",
            timeout_ms=DOCKER_PROBE_TIMEOUT_MS,
        )
        try:
            inspect_result = self._run_control(
                workspace,
                inspect_command,
                environment=environment,
            )
        except Exception as exc:
            return self._unavailable(
                f"Docker image inspection failed: {_bounded_reason(exc)}"
            )
        if inspect_result.status != "passed":
            return self._unavailable(
                "Configured Docker image is not available locally and automatic "
                "pull is disabled. "
                + _process_failure_reason("Docker image inspection", inspect_result)
            )

        try:
            image = _first_json_object(
                inspect_result.stdout,
                "Docker image inspection output",
            )
            image_os = _required_string_field(
                image,
                ("Os", "OS"),
                "Docker image operating system",
            ).lower()
            image_digest = _required_string_field(
                image,
                ("Id", "ID"),
                "Docker image ID",
            )
            if not _IMAGE_DIGEST.fullmatch(image_digest):
                raise ValueError(
                    "Docker image ID must be a pinned sha256 digest with 64 "
                    "lowercase hexadecimal characters."
                )
        except ValueError as exc:
            return self._unavailable(str(exc))
        if image_os != "linux":
            return self._unavailable(
                "Configured Docker image uses unsupported operating system "
                f"{image_os!r}; only Linux images are supported."
            )

        return SandboxCapability(
            backend="docker",
            available=True,
            reason=None,
            image_reference=self.image_reference,
            image_digest=image_digest,
        )

    def execute(
        self,
        workspace: str | Path,
        plan: SandboxExecutionPlan,
        *,
        session_id: str,
        call_id: str,
        approval_granted: bool = False,
        environment: Mapping[str, str] | None = None,
        event_handler: SecurityEventHandler | None = None,
    ) -> SandboxExecutionOutcome:
        """Execute a validated plan in a disposable filtered workspace snapshot."""

        self._validate_execution_authorization(
            plan,
            approval_granted=approval_granted,
        )
        container_name = build_docker_container_name(session_id, call_id)
        capability = self.probe_capability(workspace, environment=environment)
        if event_handler is not None:
            event_handler(
                "sandbox.capability_checked",
                {"capability": capability.to_dict()},
            )
        incompatibility = _capability_incompatibility(
            plan,
            capability,
            configured_image=self.image_reference,
        )
        if incompatibility is not None:
            return SandboxExecutionOutcome(
                result=_unavailable_result(plan, incompatibility),
                capability=capability,
                container_name=container_name,
            )

        try:
            snapshot = self._snapshot_factory(
                workspace,
                session_id=session_id,
                call_id=call_id,
                max_files=self._snapshot_max_files,
                max_bytes=self._snapshot_max_bytes,
                max_binary_file_bytes=self._snapshot_max_binary_file_bytes,
            )
            if event_handler is not None:
                event_handler(
                    "sandbox.snapshot_created",
                    {"snapshot": snapshot.audit_summary()},
                )
        except Exception as exc:
            return SandboxExecutionOutcome(
                result=_internal_error_result(
                    plan,
                    "Could not create sandbox workspace snapshot: "
                    f"{_bounded_reason(exc)}",
                ),
                capability=capability,
                container_name=container_name,
            )

        backend_argv: tuple[str, ...] = ()
        container_cleanup_attempted = False
        container_cleanup_succeeded: bool | None = None
        container_cleanup_error: str | None = None
        try:
            _require_safe_snapshot_mount(
                workspace,
                snapshot,
                command_cwd=plan.command.cwd,
            )
            _prepare_snapshot_permissions(snapshot)
            _require_safe_snapshot_mount(
                workspace,
                snapshot,
                command_cwd=plan.command.cwd,
            )
            backend_argv = build_docker_run_argv(
                plan,
                snapshot,
                docker_executable=self.docker_executable,
                container_name=container_name,
                environment=environment,
            )
            docker_command = CommandSpec(
                argv=backend_argv,
                cwd=".",
                source="internal",
                purpose="Execute the authorized command in the Docker sandbox",
                limits=plan.command.limits,
            )
            try:
                if event_handler is not None:
                    event_handler(
                        "sandbox.started",
                        {
                            "backend": "docker",
                            "container_name": container_name,
                            "image_digest": plan.image_digest,
                            "network_mode": "none",
                            "snapshot_scope": "temporary",
                        },
                    )
                process_result = self._run_control(
                    workspace,
                    docker_command,
                    environment=environment,
                )
            except Exception as exc:
                container_cleanup_attempted = True
                (
                    container_cleanup_succeeded,
                    container_cleanup_error,
                ) = self._remove_container(
                    workspace,
                    container_name,
                    environment=environment,
                )
                secure_result = _internal_error_result(
                    plan,
                    f"Docker execution failed internally: {_bounded_reason(exc)}",
                    image_digest=capability.image_digest,
                )
            else:
                if process_result.status == "timed_out":
                    container_cleanup_attempted = True
                    (
                        container_cleanup_succeeded,
                        container_cleanup_error,
                    ) = self._remove_container(
                        workspace,
                        container_name,
                        environment=environment,
                    )
                secure_result = _secure_result_from_process(
                    plan,
                    process_result,
                    image_digest=capability.image_digest,
                )
        except Exception as exc:
            secure_result = _internal_error_result(
                plan,
                f"Docker sandbox setup failed: {_bounded_reason(exc)}",
                image_digest=capability.image_digest,
            )
        finally:
            try:
                snapshot_cleanup = self._snapshot_cleaner(snapshot)
            except Exception as exc:
                snapshot_cleanup = SnapshotCleanupResult(
                    removed=False,
                    cleanup_error=_bounded_reason(exc),
                )

        return SandboxExecutionOutcome(
            result=secure_result,
            capability=capability,
            container_name=container_name,
            backend_argv=backend_argv,
            snapshot_summary=snapshot.audit_summary(),
            container_cleanup_attempted=container_cleanup_attempted,
            container_cleanup_succeeded=container_cleanup_succeeded,
            container_cleanup_error=container_cleanup_error,
            snapshot_cleanup_succeeded=snapshot_cleanup.removed,
            snapshot_cleanup_error=snapshot_cleanup.cleanup_error,
        )

    def _validate_execution_authorization(
        self,
        plan: SandboxExecutionPlan,
        *,
        approval_granted: bool,
    ) -> None:
        if not isinstance(plan, SandboxExecutionPlan):
            raise TypeError("plan must be a SandboxExecutionPlan instance.")
        if plan.backend != "docker" or not plan.sandboxed:
            raise SandboxAuthorizationError(
                "DockerSandboxBackend requires a sandboxed Docker execution plan."
            )
        if plan.network_mode != "none":
            raise SandboxAuthorizationError(
                "Docker sandbox execution requires network_mode='none'."
            )
        if plan.decision.disposition == "approval_required" and not approval_granted:
            raise SandboxAuthorizationError(
                "Docker command requires a completed approval before execution."
            )
        if plan.decision.disposition not in {
            "allow_host",
            "approval_required",
            "sandbox_required",
        }:
            raise SandboxAuthorizationError(
                f"Command disposition {plan.decision.disposition!r} is not "
                "authorized for Docker execution."
            )

    def _control_command(
        self,
        argv: tuple[str, ...],
        *,
        purpose: str,
        timeout_ms: int,
    ) -> CommandSpec:
        return CommandSpec(
            argv=argv,
            cwd=".",
            source="internal",
            purpose=purpose,
            limits=ExecutionLimits(
                timeout_ms=timeout_ms,
                max_output_bytes=64 * 1024,
                max_output_lines=500,
            ),
        )

    def _run_control(
        self,
        workspace: str | Path,
        command: CommandSpec,
        *,
        environment: Mapping[str, str] | None,
    ) -> HostProcessResult:
        return self._process_runner.run(
            workspace,
            command,
            _DOCKER_CLI_DECISION,
            environment=environment,
        )

    def reconcile_interrupted_container(
        self,
        workspace: str | Path,
        container_name: str,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> tuple[bool, bool, str | None]:
        """Find and remove one deterministic interrupted container."""

        if not isinstance(container_name, str) or not _CONTAINER_COMPONENT.fullmatch(
            container_name
        ):
            raise ValueError("container_name must be a Docker-safe name.")
        command = self._control_command(
            (
                self.docker_executable,
                "container",
                "inspect",
                "--format",
                "{{.State.Status}}",
                container_name,
            ),
            purpose="Inspect an interrupted sandbox container",
            timeout_ms=DOCKER_CLEANUP_TIMEOUT_MS,
        )
        try:
            result = self._run_control(workspace, command, environment=environment)
        except Exception as exc:
            return False, False, f"Docker container inspection failed: {_bounded_reason(exc)}"
        if result.status == "passed":
            removed, error = self._remove_container(
                workspace,
                container_name,
                environment=environment,
            )
            return True, removed, error
        output = f"{result.stdout}\n{result.stderr}".casefold()
        if "no such object" in output or "no such container" in output:
            return False, True, None
        return False, False, _process_failure_reason(
            "Docker container inspection",
            result,
        )

    def _remove_container(
        self,
        workspace: str | Path,
        container_name: str,
        *,
        environment: Mapping[str, str] | None,
    ) -> tuple[bool, str | None]:
        command = self._control_command(
            (self.docker_executable, "rm", "-f", container_name),
            purpose="Force-remove a timed-out or interrupted sandbox container",
            timeout_ms=DOCKER_CLEANUP_TIMEOUT_MS,
        )
        try:
            result = self._run_control(
                workspace,
                command,
                environment=environment,
            )
        except Exception as exc:
            return False, f"Docker container cleanup failed: {_bounded_reason(exc)}"
        if result.status == "passed":
            return True, None
        return False, _process_failure_reason("Docker container cleanup", result)

    def _unavailable(self, reason: str) -> SandboxCapability:
        return SandboxCapability(
            backend="docker",
            available=False,
            reason=_bounded_reason(reason),
            image_reference=self.image_reference,
            image_digest=None,
        )


def build_docker_container_name(session_id: str, call_id: str) -> str:
    """Create a deterministic Docker-safe name without unbounded identifiers."""

    for value, label in ((session_id, "session_id"), (call_id, "call_id")):
        if not isinstance(value, str):
            raise TypeError(f"{label} must be a string.")
        if not _CONTAINER_COMPONENT.fullmatch(value):
            raise ValueError(
                f"{label} must be 1-128 Docker-safe letters, digits, '.', '_' or '-'."
            )

    raw_name = f"coding-agent-{session_id}-{call_id}"
    if len(raw_name) <= _CONTAINER_NAME_MAX_CHARS:
        return raw_name
    suffix = hashlib.sha256(raw_name.encode("utf-8")).hexdigest()[:16]
    prefix_budget = _CONTAINER_NAME_MAX_CHARS - len("coding-agent---") - len(suffix)
    session_budget = prefix_budget // 2
    call_budget = prefix_budget - session_budget
    return (
        f"coding-agent-{session_id[:session_budget]}-"
        f"{call_id[:call_budget]}-{suffix}"
    )


def build_docker_run_argv(
    plan: SandboxExecutionPlan,
    snapshot: SandboxWorkspaceSnapshot,
    *,
    docker_executable: str = "docker",
    container_name: str,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Build deterministic, networkless Docker CLI argv for one execution."""

    if not isinstance(plan, SandboxExecutionPlan):
        raise TypeError("plan must be a SandboxExecutionPlan instance.")
    if plan.backend != "docker" or not plan.sandboxed:
        raise ValueError("Docker argv requires a sandboxed Docker plan.")
    if plan.network_mode != "none":
        raise ValueError("Docker argv requires network_mode='none'.")
    if plan.image_digest is None or not _IMAGE_DIGEST.fullmatch(plan.image_digest):
        raise ValueError("Docker argv requires a pinned sha256 image digest.")
    if not isinstance(snapshot, SandboxWorkspaceSnapshot):
        raise TypeError("snapshot must be a SandboxWorkspaceSnapshot instance.")
    docker_executable = _validate_docker_executable(docker_executable)
    if not isinstance(container_name, str) or not _CONTAINER_COMPONENT.fullmatch(
        container_name
    ):
        raise ValueError("container_name must be a Docker-safe identifier.")

    mount_source = str(snapshot.workspace_directory)
    if "," in mount_source or any(ord(character) < 32 for character in mount_source):
        raise ValueError(
            "Snapshot path cannot be represented safely by Docker --mount."
        )
    workdir = _container_workdir(plan.command.cwd)
    limits = plan.command.limits

    argv: list[str] = [
        docker_executable,
        "run",
        "--rm",
        "--name",
        container_name,
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
        str(limits.pids_limit),
        "--memory",
        f"{limits.memory_mb}m",
        "--cpus",
        _format_cpus(limits.cpus),
        "--user",
        DEFAULT_DOCKER_USER,
        "--tmpfs",
        DEFAULT_DOCKER_TMPFS,
        "--mount",
        f"type=bind,src={mount_source},dst=/workspace",
        "--workdir",
        workdir,
    ]
    for value in _container_environment(environment):
        argv.extend(("--env", value))
    argv.append(plan.image_digest)
    argv.extend(plan.command.argv)
    return tuple(argv)


def _container_environment(
    environment: Mapping[str, str] | None,
) -> tuple[str, ...]:
    inherited = build_child_environment(
        environment,
        allowlist=DOCKER_CONTAINER_ENV_ALLOWLIST,
    )
    values = [
        "HOME=/tmp",
        "TMPDIR=/tmp",
        "PYTHONDONTWRITEBYTECODE=1",
    ]
    for key in sorted(inherited):
        value = inherited[key]
        if any(ord(character) < 32 and character not in {"\t"} for character in value):
            continue
        if len(value.encode("utf-8")) > 8 * 1024:
            continue
        values.append(f"{key}={value}")
    return tuple(values)


def _container_workdir(cwd: str) -> str:
    if cwd == ".":
        return "/workspace"
    path = PurePosixPath(cwd)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Command cwd must be a canonical workspace-relative path.")
    return f"/workspace/{path.as_posix()}"


def _require_safe_snapshot_mount(
    workspace: str | Path,
    snapshot: SandboxWorkspaceSnapshot,
    *,
    command_cwd: str,
) -> None:
    root = Path(workspace).resolve(strict=True)
    if root != snapshot.source_workspace:
        raise ValueError(
            "Snapshot source workspace does not match execution workspace."
        )
    expected_call = (
        root
        / ".coding-agent"
        / "sandboxes"
        / snapshot.session_id
        / snapshot.call_id
    )
    if snapshot.call_directory != expected_call:
        raise ValueError("Snapshot call directory no longer matches its identifiers.")

    relative_paths = (
        Path(".coding-agent"),
        Path(".coding-agent") / "sandboxes",
        Path(".coding-agent") / "sandboxes" / snapshot.session_id,
        Path(".coding-agent") / "sandboxes" / snapshot.session_id / snapshot.call_id,
        Path(".coding-agent")
        / "sandboxes"
        / snapshot.session_id
        / snapshot.call_id
        / "workspace",
    )
    for relative in relative_paths:
        candidate = root / relative
        if not candidate.exists():
            raise FileNotFoundError(f"Sandbox snapshot path disappeared: {relative}")
        if is_link_or_reparse_point(candidate):
            raise ValueError(f"Sandbox snapshot path became a link: {relative}")
    if not snapshot.workspace_directory.is_dir():
        raise ValueError("Sandbox snapshot workspace is not a directory.")
    resolved = snapshot.workspace_directory.resolve(strict=True)
    if os.path.normcase(str(resolved)) != os.path.normcase(
        str(snapshot.workspace_directory)
    ):
        raise ValueError("Sandbox snapshot workspace changed before mounting.")

    relative_cwd = (
        Path()
        if command_cwd == "."
        else Path(*PurePosixPath(command_cwd).parts)
    )
    resolved_cwd = snapshot.workspace_directory / relative_cwd
    if not resolved_cwd.exists() or not resolved_cwd.is_dir():
        raise ValueError(
            "Command working directory is not present in the filtered snapshot."
        )
    if is_link_or_reparse_point(resolved_cwd):
        raise ValueError("Command working directory became a link before execution.")


def _prepare_snapshot_permissions(snapshot: SandboxWorkspaceSnapshot) -> None:
    """Make only the disposable snapshot usable by the fixed non-root UID."""

    for current_directory, directory_names, file_names in os.walk(
        snapshot.workspace_directory,
        topdown=True,
        followlinks=False,
    ):
        directory = Path(current_directory)
        if is_link_or_reparse_point(directory):
            raise ValueError("Sandbox snapshot directory became a link.")
        directory_mode = stat.S_IMODE(os.lstat(directory).st_mode)
        os.chmod(
            directory,
            directory_mode | stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH,
        )

        for name in directory_names:
            candidate = directory / name
            if is_link_or_reparse_point(candidate):
                raise ValueError("Sandbox snapshot directory became a link.")
        for name in file_names:
            candidate = directory / name
            if is_link_or_reparse_point(candidate):
                raise ValueError("Sandbox snapshot file became a link.")
            status = os.lstat(candidate)
            if not stat.S_ISREG(status.st_mode):
                raise ValueError("Sandbox snapshot contains a non-regular file.")
            current_mode = stat.S_IMODE(status.st_mode)
            desired_mode = current_mode | stat.S_IROTH | stat.S_IWOTH
            if current_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                desired_mode |= stat.S_IXOTH
            os.chmod(candidate, desired_mode)


def _capability_incompatibility(
    plan: SandboxExecutionPlan,
    capability: SandboxCapability,
    *,
    configured_image: str,
) -> str | None:
    if not capability.available:
        return capability.reason or "Docker sandbox is unavailable."
    if plan.capability.image_reference != configured_image:
        return (
            "Docker image reference changed after planning; execution was refused."
        )
    if capability.image_reference != plan.capability.image_reference:
        return (
            "Docker capability image reference does not match the approved plan."
        )
    if capability.image_digest != plan.image_digest:
        return (
            "Docker image digest changed after planning; execution was refused "
            "instead of using the drifting tag."
        )
    return None


def _secure_result_from_process(
    plan: SandboxExecutionPlan,
    process: HostProcessResult,
    *,
    image_digest: str | None,
) -> SecureExecutionResult:
    output = _combined_output(process)
    common = {
        "command": plan.command,
        "decision": plan.decision,
        "backend": "docker",
        "image_digest": image_digest,
        "output": output,
        "output_truncated": process.output_truncated,
        "omitted_lines": process.omitted_lines,
        "omitted_bytes": process.omitted_bytes,
    }
    if process.status == "passed":
        return SecureExecutionResult(
            status="passed",
            sandboxed=True,
            exit_code=0,
            timed_out=False,
            duration_ms=process.duration_ms,
            error_reason=None,
            **common,
        )
    if process.status == "failed":
        return SecureExecutionResult(
            status="failed",
            sandboxed=True,
            exit_code=process.exit_code,
            timed_out=False,
            duration_ms=process.duration_ms,
            error_reason=None,
            **common,
        )
    if process.status == "timed_out":
        return SecureExecutionResult(
            status="timed_out",
            sandboxed=True,
            exit_code=None,
            timed_out=True,
            duration_ms=process.duration_ms,
            error_reason=None,
            **common,
        )
    if process.status == "not_found":
        reason = _process_failure_reason("Docker CLI", process)
        unavailable = _unavailable_result(plan, reason)
        return unavailable
    return SecureExecutionResult(
        status="internal_error",
        sandboxed=process.actual_executable is not None,
        exit_code=None,
        timed_out=False,
        duration_ms=process.duration_ms,
        error_reason=_process_failure_reason("Docker execution", process),
        **common,
    )


def _unavailable_result(
    plan: SandboxExecutionPlan,
    reason: str,
) -> SecureExecutionResult:
    status = (
        "sandbox_unavailable"
        if plan.decision.requires_sandbox
        else "internal_error"
    )
    return SecureExecutionResult(
        command=plan.command,
        decision=plan.decision,
        status=status,
        backend="docker",
        sandboxed=False,
        image_digest=None,
        exit_code=None,
        timed_out=False,
        duration_ms=0,
        output="",
        output_truncated=False,
        omitted_lines=0,
        omitted_bytes=0,
        error_reason=_bounded_reason(reason),
    )


def _internal_error_result(
    plan: SandboxExecutionPlan,
    reason: str,
    *,
    image_digest: str | None = None,
) -> SecureExecutionResult:
    return SecureExecutionResult(
        command=plan.command,
        decision=plan.decision,
        status="internal_error",
        backend="docker",
        sandboxed=False,
        image_digest=image_digest,
        exit_code=None,
        timed_out=False,
        duration_ms=0,
        output="",
        output_truncated=False,
        omitted_lines=0,
        omitted_bytes=0,
        error_reason=_bounded_reason(reason),
    )


def _combined_output(result: HostProcessResult) -> str:
    if not result.stdout:
        return result.stderr
    if not result.stderr:
        return result.stdout
    return result.stdout + result.stderr


def _process_failure_reason(label: str, result: HostProcessResult) -> str:
    detail = result.error_reason or result.stderr.strip() or result.stdout.strip()
    if not detail:
        detail = f"status={result.status}"
        if result.exit_code is not None:
            detail += f", exit_code={result.exit_code}"
    return _bounded_reason(f"{label} failed: {detail}")


def _json_object(value: str, label: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"{label} is not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return parsed


def _first_json_object(value: str, label: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"{label} is not valid JSON.") from exc
    if (
        not isinstance(parsed, list)
        or len(parsed) != 1
        or not isinstance(parsed[0], dict)
    ):
        raise ValueError(f"{label} must contain exactly one image object.")
    return parsed[0]


def _required_string_field(
    data: Mapping[str, object],
    names: tuple[str, ...],
    label: str,
) -> str:
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value:
            return value
    raise ValueError(f"{label} is missing from Docker JSON output.")


def _format_cpus(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return format(value, ".12g")


def _validate_image_reference(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("image_reference must be a string.")
    if (
        not value
        or value.startswith("-")
        or any(character.isspace() or ord(character) < 32 for character in value)
        or len(value.encode("utf-8")) > _IMAGE_REFERENCE_MAX_BYTES
    ):
        raise ValueError("image_reference is not a safe Docker image argument.")
    return value


def _validate_docker_executable(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("docker_executable must be a string.")
    if not value or value.startswith("-") or "\x00" in value:
        raise ValueError("docker_executable must be a safe executable argument.")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _bounded_reason(value: object) -> str:
    text = str(value).strip() or "Unknown Docker sandbox error."
    if len(text) <= _REASON_MAX_CHARS:
        return text
    return text[: _REASON_MAX_CHARS - 3] + "..."


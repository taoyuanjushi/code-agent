from dataclasses import FrozenInstanceError
import json
from typing import Any

import pytest

from coding_agent.security.models import (
    COMMAND_DISPOSITIONS,
    COMMAND_SOURCES,
    DEFAULT_COMMAND_TIMEOUT_MS,
    DEFAULT_CPUS,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_OUTPUT_LINES,
    DEFAULT_MEMORY_MB,
    DEFAULT_PIDS_LIMIT,
    MAX_ARGUMENT_BYTES,
    MAX_ARGV_BYTES,
    MAX_COMMAND_ARGUMENTS,
    MAX_COMMAND_TIMEOUT_MS,
    PATH_OPERATIONS,
    SANDBOX_BACKENDS,
    SANDBOX_NETWORK_MODES,
    SECURE_EXECUTION_STATUSES,
    SECURITY_POLICY_VERSION,
    SECURITY_SCHEMA_VERSION,
    CommandPolicyDecision,
    CommandSpec,
    ExecutionLimits,
    SandboxCapability,
    SandboxExecutionPlan,
    SecureExecutionResult,
    SensitivePathDecision,
)

IMAGE_DIGEST = "sha256:" + "a" * 64


def _command(*, cwd: str = ".") -> CommandSpec:
    return CommandSpec(
        argv=("python", "-m", "pytest", "-q"),
        cwd=cwd,
        source="verification",
        purpose="Run the discovered test command",
    )


def _decision(disposition: str) -> CommandPolicyDecision:
    flags = {
        "allow_host": (False, False),
        "approval_required": (True, False),
        "sandbox_required": (False, True),
        "deny": (False, False),
        "unknown": (False, False),
    }
    requires_approval, requires_sandbox = flags[disposition]
    return CommandPolicyDecision(
        disposition=disposition,  # type: ignore[arg-type]
        rule_id=f"{disposition}.fixture",
        reasons=("Acceptance fixture decision.",),
        normalized_executable="python",
        requires_approval=requires_approval,
        requires_sandbox=requires_sandbox,
    )


def _host_capability() -> SandboxCapability:
    return SandboxCapability(
        backend="host",
        available=True,
        reason=None,
        image_reference=None,
        image_digest=None,
    )


def _docker_capability() -> SandboxCapability:
    return SandboxCapability(
        backend="docker",
        available=True,
        reason=None,
        image_reference="python:3.12-slim",
        image_digest=IMAGE_DIGEST,
    )


def _host_plan() -> SandboxExecutionPlan:
    return SandboxExecutionPlan(
        command=_command(),
        decision=_decision("approval_required"),
        capability=_host_capability(),
        backend="host",
        sandboxed=False,
        network_mode="host",
        image_digest=None,
    )


def _docker_plan() -> SandboxExecutionPlan:
    return SandboxExecutionPlan(
        command=_command(),
        decision=_decision("sandbox_required"),
        capability=_docker_capability(),
        backend="docker",
        sandboxed=True,
        network_mode="none",
        image_digest=IMAGE_DIGEST,
    )


def _result_values() -> dict[str, object]:
    return {
        "command": _command(),
        "decision": _decision("allow_host"),
        "status": "passed",
        "backend": "host",
        "sandboxed": False,
        "image_digest": None,
        "exit_code": 0,
        "timed_out": False,
        "duration_ms": 10,
        "output": "ok",
        "output_truncated": False,
        "omitted_lines": 0,
        "omitted_bytes": 0,
        "error_reason": None,
    }


def _result(**overrides: object) -> SecureExecutionResult:
    values = _result_values()
    values.update(overrides)
    status = values["status"]
    if status in {"denied", "sandbox_unavailable", "internal_error"}:
        if "exit_code" not in overrides:
            values["exit_code"] = None
        if "duration_ms" not in overrides:
            values["duration_ms"] = 0
    return SecureExecutionResult(**values)  # type: ignore[arg-type]


def test_security_versions_vocabularies_and_default_limits_are_fixed() -> None:
    limits = ExecutionLimits()

    assert SECURITY_SCHEMA_VERSION == 1
    assert SECURITY_POLICY_VERSION == 1
    assert COMMAND_DISPOSITIONS == {
        "allow_host",
        "approval_required",
        "sandbox_required",
        "deny",
    }
    assert COMMAND_SOURCES == {"internal", "verification", "tool"}
    assert PATH_OPERATIONS == {
        "list",
        "search",
        "read",
        "write",
        "execute",
        "artifact_expand",
        "snapshot",
    }
    assert SANDBOX_BACKENDS == {"host", "docker"}
    assert SANDBOX_NETWORK_MODES == {"host", "none"}
    assert SECURE_EXECUTION_STATUSES == {
        "passed",
        "failed",
        "timed_out",
        "denied",
        "sandbox_unavailable",
        "internal_error",
    }
    assert limits == ExecutionLimits(
        timeout_ms=DEFAULT_COMMAND_TIMEOUT_MS,
        max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
        max_output_lines=DEFAULT_MAX_OUTPUT_LINES,
        memory_mb=DEFAULT_MEMORY_MB,
        pids_limit=DEFAULT_PIDS_LIMIT,
        cpus=DEFAULT_CPUS,
    )
    assert limits.timeout_ms == 30_000
    assert limits.max_output_bytes == 32_768
    assert limits.max_output_lines == 200
    assert MAX_COMMAND_TIMEOUT_MS == 300_000


def test_security_models_are_immutable() -> None:
    limits = ExecutionLimits()
    command = _command()

    with pytest.raises(FrozenInstanceError):
        limits.timeout_ms = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        command.cwd = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("timeout_ms", 0),
        ("timeout_ms", -1),
        ("timeout_ms", True),
        ("max_output_bytes", 0),
        ("max_output_lines", -1),
        ("memory_mb", 0),
        ("pids_limit", -1),
        ("cpus", 0),
        ("cpus", -0.5),
        ("cpus", float("inf")),
        ("cpus", True),
    ],
)
def test_execution_limits_reject_invalid_values(
    field_name: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "timeout_ms": 30_000,
        "max_output_bytes": 32_768,
        "max_output_lines": 200,
        "memory_mb": 1024,
        "pids_limit": 256,
        "cpus": 2.0,
    }
    values[field_name] = value

    with pytest.raises((TypeError, ValueError)):
        ExecutionLimits(**values)  # type: ignore[arg-type]


def test_execution_limits_reject_timeout_above_global_maximum() -> None:
    with pytest.raises(ValueError, match="at most 300000"):
        ExecutionLimits(timeout_ms=MAX_COMMAND_TIMEOUT_MS + 1)


def test_command_spec_preserves_argv_metacharacters_without_shell_parsing() -> None:
    command = CommandSpec(
        argv=("python", "-c", "value && echo no", ">", "result.txt"),
        cwd="packages/service",
        source="tool",
        purpose="Run a controlled command",
    )

    assert command.argv[2:] == ("value && echo no", ">", "result.txt")
    assert command.cwd == "packages/service"
    assert command.limits == ExecutionLimits()


@pytest.mark.parametrize(
    "argv",
    [
        [],
        (),
        ("python", ""),
        ("python", "bad\x00argument"),
        ("python", "\ud800"),
        ("x" * (MAX_ARGUMENT_BYTES + 1),),
        tuple("x" for _ in range(MAX_COMMAND_ARGUMENTS + 1)),
        tuple("x" * MAX_ARGUMENT_BYTES for _ in range(5)),
    ],
)
def test_command_spec_rejects_invalid_or_over_budget_argv(argv: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        CommandSpec(
            argv=argv,  # type: ignore[arg-type]
            cwd=".",
            source="tool",
            purpose="Invalid argv fixture",
        )

    assert MAX_ARGV_BYTES == 65_536


@pytest.mark.parametrize(
    "cwd",
    [
        "",
        "/absolute",
        "C:/workspace",
        "../outside",
        "src/../../outside",
        "src\\package",
        "./src",
        "src//package",
        "src/./package",
    ],
)
def test_command_spec_requires_canonical_workspace_relative_posix_cwd(
    cwd: str,
) -> None:
    with pytest.raises(ValueError, match="cwd"):
        _command(cwd=cwd)


def test_command_policy_decision_flags_are_derived_by_disposition_contract() -> None:
    expected = {
        "allow_host": (False, False),
        "approval_required": (True, False),
        "sandbox_required": (False, True),
        "deny": (False, False),
    }

    for disposition, flags in expected.items():
        decision = _decision(disposition)
        assert (decision.requires_approval, decision.requires_sandbox) == flags
        assert decision.policy_version == SECURITY_POLICY_VERSION


def test_command_policy_decision_rejects_unknown_or_conflicting_values() -> None:
    with pytest.raises(ValueError, match="disposition"):
        _decision("unknown")
    with pytest.raises(ValueError, match="conflict"):
        CommandPolicyDecision(
            disposition="deny",
            rule_id="deny.fixture",
            reasons=("fixture",),
            normalized_executable="python",
            requires_approval=True,
            requires_sandbox=False,
        )
    with pytest.raises(ValueError, match="policy version"):
        CommandPolicyDecision(
            disposition="allow_host",
            rule_id="allow.fixture",
            reasons=("fixture",),
            normalized_executable="python",
            requires_approval=False,
            requires_sandbox=False,
            policy_version=2,
        )


def test_sensitive_path_decision_uses_posix_paths_and_known_operations() -> None:
    decision = SensitivePathDecision(
        path="config/.env",
        operation="read",
        allowed=False,
        rule_id="deny.sensitive_env",
        reasons=("Environment files may contain secrets.",),
    )

    assert decision.allowed is False
    assert decision.policy_version == SECURITY_POLICY_VERSION

    with pytest.raises(ValueError, match="path"):
        SensitivePathDecision(
            path="..\\outside",
            operation="read",
            allowed=False,
            rule_id="deny.workspace_escape",
            reasons=("outside",),
        )
    with pytest.raises(ValueError, match="operation"):
        SensitivePathDecision(
            path="src/app.py",
            operation="delete",  # type: ignore[arg-type]
            allowed=False,
            rule_id="deny.unknown_operation",
            reasons=("unknown",),
        )


def test_sandbox_capability_validates_backend_availability_and_image_digest() -> None:
    host = _host_capability()
    docker = _docker_capability()
    unavailable = SandboxCapability(
        backend="docker",
        available=False,
        reason="docker daemon unavailable",
        image_reference="python:3.12-slim",
        image_digest=None,
    )

    assert host.image_digest is None
    assert docker.image_digest == IMAGE_DIGEST
    assert unavailable.available is False

    with pytest.raises(ValueError, match="backend"):
        SandboxCapability(
            backend="podman",  # type: ignore[arg-type]
            available=True,
            reason=None,
            image_reference=None,
            image_digest=None,
        )
    with pytest.raises((TypeError, ValueError), match="reason"):
        SandboxCapability(
            backend="host",
            available=False,
            reason=None,
            image_reference=None,
            image_digest=None,
        )
    with pytest.raises(ValueError, match="image_digest"):
        SandboxCapability(
            backend="docker",
            available=True,
            reason=None,
            image_reference="python:3.12-slim",
            image_digest="python:3.12-slim",
        )


def test_execution_plans_enforce_backend_and_policy_consistency() -> None:
    host_plan = _host_plan()
    docker_plan = _docker_plan()

    assert host_plan.sandboxed is False
    assert host_plan.network_mode == "host"
    assert docker_plan.sandboxed is True
    assert docker_plan.network_mode == "none"
    assert docker_plan.image_digest == IMAGE_DIGEST

    stricter_docker_plan = SandboxExecutionPlan(
        command=_command(),
        decision=_decision("allow_host"),
        capability=_docker_capability(),
        backend="docker",
        sandboxed=True,
        network_mode="none",
        image_digest=IMAGE_DIGEST,
    )
    assert stricter_docker_plan.decision.disposition == "allow_host"
    stricter_result = _result(
        decision=_decision("allow_host"),
        backend="docker",
        sandboxed=True,
        image_digest=IMAGE_DIGEST,
    )
    assert stricter_result.backend == "docker"

    with pytest.raises(ValueError, match="sandbox-required"):
        SandboxExecutionPlan(
            command=_command(),
            decision=_decision("sandbox_required"),
            capability=_host_capability(),
            backend="host",
            sandboxed=False,
            network_mode="host",
            image_digest=None,
        )
    with pytest.raises(ValueError, match="network_mode='none'"):
        SandboxExecutionPlan(
            command=_command(),
            decision=_decision("sandbox_required"),
            capability=_docker_capability(),
            backend="docker",
            sandboxed=True,
            network_mode="host",
            image_digest=IMAGE_DIGEST,
        )
    with pytest.raises(ValueError, match="Denied commands"):
        SandboxExecutionPlan(
            command=_command(),
            decision=_decision("deny"),
            capability=_host_capability(),
            backend="host",
            sandboxed=False,
            network_mode="host",
            image_digest=None,
        )


def test_secure_execution_result_supports_all_required_statuses() -> None:
    results = (
        _result(status="passed"),
        _result(status="failed", exit_code=1),
        _result(status="timed_out", exit_code=None, timed_out=True),
        _result(
            status="denied",
            decision=_decision("deny"),
            backend=None,
            duration_ms=0,
            error_reason="Command is hard denied.",
        ),
        _result(
            status="sandbox_unavailable",
            decision=_decision("sandbox_required"),
            backend="docker",
            duration_ms=0,
            error_reason="Docker is unavailable.",
        ),
        _result(
            status="internal_error",
            backend=None,
            duration_ms=0,
            error_reason="Runner initialization failed.",
        ),
    )

    assert {result.status for result in results} == SECURE_EXECUTION_STATUSES
    assert results[0].policy_version == SECURITY_POLICY_VERSION
    assert results[0].rule_id == "allow_host.fixture"


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "passed", "exit_code": 1},
        {"status": "failed", "exit_code": 0},
        {"status": "timed_out", "exit_code": None, "timed_out": False},
        {"output_truncated": False, "omitted_bytes": 1},
        {"output_truncated": True, "omitted_bytes": 0, "omitted_lines": 0},
        {"status": "denied", "decision": None, "backend": None},
        {"backend": "docker", "sandboxed": True, "image_digest": None},
    ],
)
def test_secure_execution_result_rejects_conflicting_state(
    overrides: dict[str, object],
) -> None:
    values = _result_values()
    if overrides.get("status") == "denied" and overrides.get("decision") is None:
        overrides = dict(overrides)
        overrides["decision"] = _decision("allow_host")
        overrides["duration_ms"] = 0
        overrides["error_reason"] = "denied"
    values.update(overrides)

    with pytest.raises((TypeError, ValueError)):
        SecureExecutionResult(**values)  # type: ignore[arg-type]


def test_all_security_models_round_trip_through_json_data() -> None:
    values = (
        ExecutionLimits(),
        _command(),
        _decision("approval_required"),
        SensitivePathDecision(
            path="src/app.py",
            operation="read",
            allowed=True,
            rule_id="allow.source",
            reasons=("Source files are readable.",),
        ),
        _host_capability(),
        _docker_capability(),
        _host_plan(),
        _docker_plan(),
        _result(status="passed"),
        _result(
            status="denied",
            decision=_decision("deny"),
            backend=None,
            duration_ms=0,
            error_reason="Command is hard denied.",
        ),
    )

    for value in values:
        serialized = json.loads(json.dumps(value.to_dict()))
        assert type(value).from_dict(serialized) == value


@pytest.mark.parametrize(
    "value",
    [
        ExecutionLimits(),
        _command(),
        _decision("allow_host"),
        SensitivePathDecision(
            path="src/app.py",
            operation="read",
            allowed=True,
            rule_id="allow.source",
            reasons=("readable",),
        ),
        _host_capability(),
        _host_plan(),
        _result(status="passed"),
    ],
)
def test_all_security_models_reject_unknown_fields_and_schema_versions(
    value: Any,
) -> None:
    unknown_field = value.to_dict()
    unknown_field["future_field"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        type(value).from_dict(unknown_field)

    unknown_version = value.to_dict()
    unknown_version["schema_version"] = SECURITY_SCHEMA_VERSION + 1
    with pytest.raises(ValueError, match="schema version"):
        type(value).from_dict(unknown_version)


def test_nested_models_reject_unknown_schema_versions() -> None:
    data = _docker_plan().to_dict()
    command = dict(data["command"])  # type: ignore[arg-type]
    command["schema_version"] = 99
    data["command"] = command

    with pytest.raises(ValueError, match="CommandSpec schema version"):
        SandboxExecutionPlan.from_dict(data)

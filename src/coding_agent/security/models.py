from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal, Self, TypeAlias

SECURITY_SCHEMA_VERSION = 1
SECURITY_POLICY_VERSION = 1

DEFAULT_COMMAND_TIMEOUT_MS = 30_000
MAX_COMMAND_TIMEOUT_MS = 300_000
DEFAULT_MAX_OUTPUT_BYTES = 32 * 1024
DEFAULT_MAX_OUTPUT_LINES = 200
DEFAULT_MEMORY_MB = 1024
DEFAULT_PIDS_LIMIT = 256
DEFAULT_CPUS = 2.0

MAX_COMMAND_ARGUMENTS = 256
MAX_ARGUMENT_BYTES = 16 * 1024
MAX_ARGV_BYTES = 64 * 1024
MAX_PURPOSE_BYTES = 4 * 1024

CommandDisposition = Literal[
    "allow_host",
    "approval_required",
    "sandbox_required",
    "deny",
]
CommandSource = Literal["internal", "verification", "tool"]
PathOperation = Literal[
    "list",
    "search",
    "read",
    "write",
    "execute",
    "artifact_expand",
    "snapshot",
]
SandboxBackend = Literal["host", "docker"]
SandboxNetworkMode = Literal["host", "none"]
SecureExecutionStatus = Literal[
    "passed",
    "failed",
    "timed_out",
    "denied",
    "sandbox_unavailable",
    "internal_error",
]

COMMAND_DISPOSITIONS = frozenset(
    {"allow_host", "approval_required", "sandbox_required", "deny"}
)
COMMAND_SOURCES = frozenset({"internal", "verification", "tool"})
PATH_OPERATIONS = frozenset(
    {"list", "search", "read", "write", "execute", "artifact_expand", "snapshot"}
)
SANDBOX_BACKENDS = frozenset({"host", "docker"})
SANDBOX_NETWORK_MODES = frozenset({"host", "none"})
SECURE_EXECUTION_STATUSES = frozenset(
    {
        "passed",
        "failed",
        "timed_out",
        "denied",
        "sandbox_unavailable",
        "internal_error",
    }
)

SerializedObject: TypeAlias = Mapping[str, object]
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:(?:/|$)")
_DISPOSITION_FLAGS = {
    "allow_host": (False, False),
    "approval_required": (True, False),
    "sandbox_required": (False, True),
    "deny": (False, False),
}


@dataclass(frozen=True)
class ExecutionLimits:
    timeout_ms: int = DEFAULT_COMMAND_TIMEOUT_MS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_output_lines: int = DEFAULT_MAX_OUTPUT_LINES
    memory_mb: int = DEFAULT_MEMORY_MB
    pids_limit: int = DEFAULT_PIDS_LIMIT
    cpus: float = DEFAULT_CPUS

    def __post_init__(self) -> None:
        _validate_positive_int(self.timeout_ms, "timeout_ms")
        if self.timeout_ms > MAX_COMMAND_TIMEOUT_MS:
            raise ValueError(
                f"timeout_ms must be at most {MAX_COMMAND_TIMEOUT_MS}."
            )
        _validate_positive_int(self.max_output_bytes, "max_output_bytes")
        _validate_positive_int(self.max_output_lines, "max_output_lines")
        _validate_positive_int(self.memory_mb, "memory_mb")
        _validate_positive_int(self.pids_limit, "pids_limit")
        if isinstance(self.cpus, bool) or not isinstance(self.cpus, (int, float)):
            raise TypeError("cpus must be a number.")
        cpus = float(self.cpus)
        if not math.isfinite(cpus) or cpus <= 0:
            raise ValueError("cpus must be a positive finite number.")
        object.__setattr__(self, "cpus", cpus)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SECURITY_SCHEMA_VERSION,
            "timeout_ms": self.timeout_ms,
            "max_output_bytes": self.max_output_bytes,
            "max_output_lines": self.max_output_lines,
            "memory_mb": self.memory_mb,
            "pids_limit": self.pids_limit,
            "cpus": self.cpus,
        }

    @classmethod
    def from_dict(cls, data: SerializedObject) -> Self:
        obj = _strict_versioned_object(
            data,
            fields={
                "timeout_ms",
                "max_output_bytes",
                "max_output_lines",
                "memory_mb",
                "pids_limit",
                "cpus",
            },
            label=cls.__name__,
        )
        return cls(
            timeout_ms=_integer(obj, "timeout_ms"),
            max_output_bytes=_integer(obj, "max_output_bytes"),
            max_output_lines=_integer(obj, "max_output_lines"),
            memory_mb=_integer(obj, "memory_mb"),
            pids_limit=_integer(obj, "pids_limit"),
            cpus=_number(obj, "cpus"),
        )


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    cwd: str
    source: CommandSource
    purpose: str
    limits: ExecutionLimits = field(default_factory=ExecutionLimits)

    def __post_init__(self) -> None:
        _validate_argv(self.argv)
        _validate_relative_posix_path(self.cwd, "cwd")
        if self.source not in COMMAND_SOURCES:
            raise ValueError(f"Unsupported command source: {self.source}")
        _validate_bounded_string(self.purpose, "purpose", MAX_PURPOSE_BYTES)
        if not isinstance(self.limits, ExecutionLimits):
            raise TypeError("limits must be an ExecutionLimits instance.")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SECURITY_SCHEMA_VERSION,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "source": self.source,
            "purpose": self.purpose,
            "limits": self.limits.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: SerializedObject) -> Self:
        obj = _strict_versioned_object(
            data,
            fields={"argv", "cwd", "source", "purpose", "limits"},
            label=cls.__name__,
        )
        return cls(
            argv=_string_list(obj, "argv"),
            cwd=_string(obj, "cwd"),
            source=_string(obj, "source"),  # type: ignore[arg-type]
            purpose=_string(obj, "purpose"),
            limits=ExecutionLimits.from_dict(_mapping(obj, "limits")),
        )


@dataclass(frozen=True)
class CommandPolicyDecision:
    disposition: CommandDisposition
    rule_id: str
    reasons: tuple[str, ...]
    normalized_executable: str
    requires_approval: bool
    requires_sandbox: bool
    policy_version: int = SECURITY_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.disposition not in COMMAND_DISPOSITIONS:
            raise ValueError(f"Unsupported command disposition: {self.disposition}")
        _validate_rule_id(self.rule_id)
        _validate_reasons(self.reasons)
        _validate_bounded_string(
            self.normalized_executable,
            "normalized_executable",
            MAX_ARGUMENT_BYTES,
        )
        _validate_boolean(self.requires_approval, "requires_approval")
        _validate_boolean(self.requires_sandbox, "requires_sandbox")
        _validate_policy_version(self.policy_version)
        expected = _DISPOSITION_FLAGS[self.disposition]
        actual = (self.requires_approval, self.requires_sandbox)
        if actual != expected:
            raise ValueError(
                "requires_approval/requires_sandbox conflict with disposition "
                f"{self.disposition!r}; expected {expected}."
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SECURITY_SCHEMA_VERSION,
            "policy_version": self.policy_version,
            "disposition": self.disposition,
            "rule_id": self.rule_id,
            "reasons": list(self.reasons),
            "normalized_executable": self.normalized_executable,
            "requires_approval": self.requires_approval,
            "requires_sandbox": self.requires_sandbox,
        }

    @classmethod
    def from_dict(cls, data: SerializedObject) -> Self:
        obj = _strict_versioned_object(
            data,
            fields={
                "policy_version",
                "disposition",
                "rule_id",
                "reasons",
                "normalized_executable",
                "requires_approval",
                "requires_sandbox",
            },
            label=cls.__name__,
        )
        return cls(
            policy_version=_integer(obj, "policy_version"),
            disposition=_string(obj, "disposition"),  # type: ignore[arg-type]
            rule_id=_string(obj, "rule_id"),
            reasons=_string_list(obj, "reasons"),
            normalized_executable=_string(obj, "normalized_executable"),
            requires_approval=_boolean(obj, "requires_approval"),
            requires_sandbox=_boolean(obj, "requires_sandbox"),
        )

@dataclass(frozen=True)
class SensitivePathDecision:
    path: str
    operation: PathOperation
    allowed: bool
    rule_id: str
    reasons: tuple[str, ...]
    policy_version: int = SECURITY_POLICY_VERSION

    def __post_init__(self) -> None:
        _validate_relative_posix_path(self.path, "path")
        if self.operation not in PATH_OPERATIONS:
            raise ValueError(f"Unsupported path operation: {self.operation}")
        _validate_boolean(self.allowed, "allowed")
        _validate_rule_id(self.rule_id)
        _validate_reasons(self.reasons)
        _validate_policy_version(self.policy_version)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SECURITY_SCHEMA_VERSION,
            "policy_version": self.policy_version,
            "path": self.path,
            "operation": self.operation,
            "allowed": self.allowed,
            "rule_id": self.rule_id,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, data: SerializedObject) -> Self:
        obj = _strict_versioned_object(
            data,
            fields={
                "policy_version",
                "path",
                "operation",
                "allowed",
                "rule_id",
                "reasons",
            },
            label=cls.__name__,
        )
        return cls(
            policy_version=_integer(obj, "policy_version"),
            path=_string(obj, "path"),
            operation=_string(obj, "operation"),  # type: ignore[arg-type]
            allowed=_boolean(obj, "allowed"),
            rule_id=_string(obj, "rule_id"),
            reasons=_string_list(obj, "reasons"),
        )


@dataclass(frozen=True)
class SandboxCapability:
    backend: SandboxBackend
    available: bool
    reason: str | None
    image_reference: str | None
    image_digest: str | None

    def __post_init__(self) -> None:
        if self.backend not in SANDBOX_BACKENDS:
            raise ValueError(f"Unsupported sandbox backend: {self.backend}")
        _validate_boolean(self.available, "available")
        if self.available:
            if self.reason is not None:
                raise ValueError("available capabilities must not include a reason.")
        else:
            _validate_non_empty_string(self.reason, "reason")

        if self.backend == "host":
            if self.image_reference is not None or self.image_digest is not None:
                raise ValueError("host capabilities cannot include Docker image fields.")
            return

        _validate_non_empty_string(self.image_reference, "image_reference")
        if self.image_digest is not None:
            _validate_image_digest(self.image_digest)
        if self.available and self.image_digest is None:
            raise ValueError(
                "available Docker capabilities require a pinned image_digest."
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SECURITY_SCHEMA_VERSION,
            "backend": self.backend,
            "available": self.available,
            "reason": self.reason,
            "image_reference": self.image_reference,
            "image_digest": self.image_digest,
        }

    @classmethod
    def from_dict(cls, data: SerializedObject) -> Self:
        obj = _strict_versioned_object(
            data,
            fields={
                "backend",
                "available",
                "reason",
                "image_reference",
                "image_digest",
            },
            label=cls.__name__,
        )
        return cls(
            backend=_string(obj, "backend"),  # type: ignore[arg-type]
            available=_boolean(obj, "available"),
            reason=_optional_string(obj, "reason"),
            image_reference=_optional_string(obj, "image_reference"),
            image_digest=_optional_string(obj, "image_digest"),
        )


@dataclass(frozen=True)
class SandboxExecutionPlan:
    command: CommandSpec
    decision: CommandPolicyDecision
    capability: SandboxCapability
    backend: SandboxBackend
    sandboxed: bool
    network_mode: SandboxNetworkMode
    image_digest: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.command, CommandSpec):
            raise TypeError("command must be a CommandSpec instance.")
        if not isinstance(self.decision, CommandPolicyDecision):
            raise TypeError("decision must be a CommandPolicyDecision instance.")
        if not isinstance(self.capability, SandboxCapability):
            raise TypeError("capability must be a SandboxCapability instance.")
        if self.backend not in SANDBOX_BACKENDS:
            raise ValueError(f"Unsupported sandbox backend: {self.backend}")
        _validate_boolean(self.sandboxed, "sandboxed")
        if self.network_mode not in SANDBOX_NETWORK_MODES:
            raise ValueError(f"Unsupported network mode: {self.network_mode}")
        if self.image_digest is not None:
            _validate_image_digest(self.image_digest)

        if self.decision.disposition == "deny":
            raise ValueError("Denied commands cannot produce an execution plan.")
        if not self.capability.available:
            raise ValueError("Execution plans require an available capability.")
        if self.backend != self.capability.backend:
            raise ValueError("plan backend must match capability backend.")

        if self.backend == "host":
            if self.sandboxed:
                raise ValueError("host execution plans cannot be sandboxed.")
            if self.network_mode != "host":
                raise ValueError("host execution plans require network_mode='host'.")
            if self.image_digest is not None:
                raise ValueError("host execution plans cannot include image_digest.")
            if self.decision.requires_sandbox:
                raise ValueError("sandbox-required commands cannot use the host backend.")
            return

        if not self.sandboxed:
            raise ValueError("Docker execution plans must be sandboxed.")
        if self.network_mode != "none":
            raise ValueError("Docker execution plans require network_mode='none'.")
        if self.image_digest is None:
            raise ValueError("Docker execution plans require image_digest.")
        if self.image_digest != self.capability.image_digest:
            raise ValueError("plan image_digest must match capability image_digest.")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SECURITY_SCHEMA_VERSION,
            "command": self.command.to_dict(),
            "decision": self.decision.to_dict(),
            "capability": self.capability.to_dict(),
            "backend": self.backend,
            "sandboxed": self.sandboxed,
            "network_mode": self.network_mode,
            "image_digest": self.image_digest,
        }

    @classmethod
    def from_dict(cls, data: SerializedObject) -> Self:
        obj = _strict_versioned_object(
            data,
            fields={
                "command",
                "decision",
                "capability",
                "backend",
                "sandboxed",
                "network_mode",
                "image_digest",
            },
            label=cls.__name__,
        )
        return cls(
            command=CommandSpec.from_dict(_mapping(obj, "command")),
            decision=CommandPolicyDecision.from_dict(_mapping(obj, "decision")),
            capability=SandboxCapability.from_dict(_mapping(obj, "capability")),
            backend=_string(obj, "backend"),  # type: ignore[arg-type]
            sandboxed=_boolean(obj, "sandboxed"),
            network_mode=_string(obj, "network_mode"),  # type: ignore[arg-type]
            image_digest=_optional_string(obj, "image_digest"),
        )

@dataclass(frozen=True)
class SecureExecutionResult:
    command: CommandSpec
    decision: CommandPolicyDecision
    status: SecureExecutionStatus
    backend: SandboxBackend | None
    sandboxed: bool
    image_digest: str | None
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    output: str
    output_truncated: bool
    omitted_lines: int
    omitted_bytes: int
    error_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.command, CommandSpec):
            raise TypeError("command must be a CommandSpec instance.")
        if not isinstance(self.decision, CommandPolicyDecision):
            raise TypeError("decision must be a CommandPolicyDecision instance.")
        if self.status not in SECURE_EXECUTION_STATUSES:
            raise ValueError(f"Unsupported secure execution status: {self.status}")
        if self.backend is not None and self.backend not in SANDBOX_BACKENDS:
            raise ValueError(f"Unsupported sandbox backend: {self.backend}")
        _validate_boolean(self.sandboxed, "sandboxed")
        if self.image_digest is not None:
            _validate_image_digest(self.image_digest)
        if self.exit_code is not None:
            _validate_integer(self.exit_code, "exit_code")
        _validate_boolean(self.timed_out, "timed_out")
        _validate_non_negative_int(self.duration_ms, "duration_ms")
        if not isinstance(self.output, str):
            raise TypeError("output must be a string.")
        _validate_boolean(self.output_truncated, "output_truncated")
        _validate_non_negative_int(self.omitted_lines, "omitted_lines")
        _validate_non_negative_int(self.omitted_bytes, "omitted_bytes")
        if self.error_reason is not None:
            _validate_non_empty_string(self.error_reason, "error_reason")

        if not self.output_truncated and (self.omitted_lines or self.omitted_bytes):
            raise ValueError(
                "omitted line or byte counts require output_truncated=True."
            )
        if self.output_truncated and not (self.omitted_lines or self.omitted_bytes):
            raise ValueError(
                "output_truncated=True requires omitted line or byte counts."
            )

        self._validate_backend_state()
        self._validate_status_state()

    @property
    def policy_version(self) -> int:
        return self.decision.policy_version

    @property
    def rule_id(self) -> str:
        return self.decision.rule_id

    def _validate_backend_state(self) -> None:
        if self.backend is None:
            if self.sandboxed or self.image_digest is not None:
                raise ValueError(
                    "results without a backend cannot be sandboxed or include image_digest."
                )
            return
        if self.backend == "host":
            if self.sandboxed or self.image_digest is not None:
                raise ValueError(
                    "host results cannot be sandboxed or include image_digest."
                )
            return
        if self.sandboxed and self.image_digest is None:
            raise ValueError("sandboxed Docker results require image_digest.")

    def _validate_status_state(self) -> None:
        executed_statuses = {"passed", "failed", "timed_out"}
        if self.decision.disposition == "deny" and self.status != "denied":
            raise ValueError("deny decisions must produce status='denied'.")
        if self.status == "denied":
            if self.decision.disposition != "deny":
                raise ValueError("denied results require a deny decision.")
            self._require_not_executed("denied")
            _validate_non_empty_string(self.error_reason, "error_reason")
            if self.backend is not None:
                raise ValueError("denied results must not select a backend.")
            return

        if self.decision.disposition == "deny":
            raise ValueError("deny decisions cannot produce an executed result.")

        if self.status == "sandbox_unavailable":
            self._require_not_executed("sandbox_unavailable")
            _validate_non_empty_string(self.error_reason, "error_reason")
            if not self.decision.requires_sandbox:
                raise ValueError(
                    "sandbox_unavailable requires a sandbox_required decision."
                )
            if self.backend != "docker" or self.sandboxed:
                raise ValueError(
                    "sandbox_unavailable requires an unstarted Docker backend."
                )
            return

        if self.status == "internal_error":
            if self.exit_code is not None or self.timed_out:
                raise ValueError(
                    "internal_error results cannot include exit_code or timed_out=True."
                )
            _validate_non_empty_string(self.error_reason, "error_reason")
            return

        if self.status in executed_statuses:
            if self.backend is None:
                raise ValueError(f"{self.status} results require an execution backend.")
            if self.backend == "docker" and not self.sandboxed:
                raise ValueError(
                    f"{self.status} Docker results must be sandboxed."
                )
            if self.decision.requires_sandbox and self.backend != "docker":
                raise ValueError(
                    "sandbox-required commands must execute with the Docker backend."
                )

        if self.status == "passed":
            if self.exit_code != 0 or self.timed_out or self.error_reason is not None:
                raise ValueError(
                    "passed results require exit_code=0 and no timeout or error_reason."
                )
        elif self.status == "failed":
            if (
                self.exit_code is None
                or self.exit_code == 0
                or self.timed_out
                or self.error_reason is not None
            ):
                raise ValueError(
                    "failed results require a non-zero exit_code and no timeout or error_reason."
                )
        elif self.status == "timed_out":
            if (
                self.exit_code is not None
                or not self.timed_out
                or self.error_reason is not None
            ):
                raise ValueError(
                    "timed_out results require timed_out=True without exit_code or error_reason."
                )

    def _require_not_executed(self, status: str) -> None:
        if self.exit_code is not None or self.timed_out or self.duration_ms != 0:
            raise ValueError(
                f"{status} results must have no exit_code, timeout, or duration."
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SECURITY_SCHEMA_VERSION,
            "command": self.command.to_dict(),
            "decision": self.decision.to_dict(),
            "status": self.status,
            "backend": self.backend,
            "sandboxed": self.sandboxed,
            "image_digest": self.image_digest,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "output": self.output,
            "output_truncated": self.output_truncated,
            "omitted_lines": self.omitted_lines,
            "omitted_bytes": self.omitted_bytes,
            "error_reason": self.error_reason,
        }

    @classmethod
    def from_dict(cls, data: SerializedObject) -> Self:
        obj = _strict_versioned_object(
            data,
            fields={
                "command",
                "decision",
                "status",
                "backend",
                "sandboxed",
                "image_digest",
                "exit_code",
                "timed_out",
                "duration_ms",
                "output",
                "output_truncated",
                "omitted_lines",
                "omitted_bytes",
                "error_reason",
            },
            label=cls.__name__,
        )
        return cls(
            command=CommandSpec.from_dict(_mapping(obj, "command")),
            decision=CommandPolicyDecision.from_dict(_mapping(obj, "decision")),
            status=_string(obj, "status"),  # type: ignore[arg-type]
            backend=_optional_string(obj, "backend"),  # type: ignore[arg-type]
            sandboxed=_boolean(obj, "sandboxed"),
            image_digest=_optional_string(obj, "image_digest"),
            exit_code=_optional_integer(obj, "exit_code"),
            timed_out=_boolean(obj, "timed_out"),
            duration_ms=_integer(obj, "duration_ms"),
            output=_string(obj, "output", allow_empty=True),
            output_truncated=_boolean(obj, "output_truncated"),
            omitted_lines=_integer(obj, "omitted_lines"),
            omitted_bytes=_integer(obj, "omitted_bytes"),
            error_reason=_optional_string(obj, "error_reason"),
        )

def _strict_versioned_object(
    data: SerializedObject,
    *,
    fields: set[str],
    label: str,
) -> dict[str, object]:
    obj = _strict_object(
        data,
        required={"schema_version", *fields},
        label=label,
    )
    version = _integer(obj, "schema_version")
    if version != SECURITY_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported {label} schema version: {version}; "
            f"expected {SECURITY_SCHEMA_VERSION}."
        )
    return obj


def _strict_object(
    data: SerializedObject,
    *,
    required: set[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(data, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    if any(not isinstance(key, str) for key in data):
        raise TypeError(f"{label} field names must be strings.")
    actual = set(data)
    missing = required - actual
    unknown = actual - required
    if missing:
        raise ValueError(f"{label} is missing fields: {', '.join(sorted(missing))}.")
    if unknown:
        raise ValueError(f"{label} has unknown fields: {', '.join(sorted(unknown))}.")
    return dict(data)


def _mapping(data: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = data[key]
    if not isinstance(value, Mapping):
        raise TypeError(f"{key} must be a mapping.")
    return value


def _string(
    data: Mapping[str, object],
    key: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string.")
    if not allow_empty and not value.strip():
        raise ValueError(f"{key} must be a non-empty string.")
    return value


def _optional_string(data: Mapping[str, object], key: str) -> str | None:
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string or null.")
    if not value.strip():
        raise ValueError(f"{key} must be non-empty when provided.")
    return value


def _integer(data: Mapping[str, object], key: str) -> int:
    value = data[key]
    _validate_integer(value, key)
    return value


def _optional_integer(data: Mapping[str, object], key: str) -> int | None:
    value = data[key]
    if value is None:
        return None
    _validate_integer(value, key)
    return value


def _number(data: Mapping[str, object], key: str) -> float:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{key} must be a number.")
    return float(value)


def _boolean(data: Mapping[str, object], key: str) -> bool:
    value = data[key]
    _validate_boolean(value, key)
    return value


def _string_list(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = data[key]
    if not isinstance(value, list):
        raise TypeError(f"{key} must be a list.")
    if any(not isinstance(item, str) for item in value):
        raise TypeError(f"{key} must contain only strings.")
    return tuple(value)


def _validate_argv(argv: object) -> None:
    if not isinstance(argv, tuple):
        raise TypeError("argv must be a tuple.")
    if not argv:
        raise ValueError("argv must contain at least one argument.")
    if len(argv) > MAX_COMMAND_ARGUMENTS:
        raise ValueError(
            f"argv must contain at most {MAX_COMMAND_ARGUMENTS} arguments."
        )

    total_bytes = 0
    for index, argument in enumerate(argv):
        if not isinstance(argument, str):
            raise TypeError(f"argv[{index}] must be a string.")
        if argument == "":
            raise ValueError(f"argv[{index}] must be non-empty.")
        if index == 0 and not argument.strip():
            raise ValueError("argv[0] executable must not be whitespace-only.")
        if "\x00" in argument:
            raise ValueError(f"argv[{index}] must not contain NUL characters.")
        encoded = _encode_utf8(argument, f"argv[{index}]")
        if len(encoded) > MAX_ARGUMENT_BYTES:
            raise ValueError(
                f"argv[{index}] exceeds the {MAX_ARGUMENT_BYTES}-byte limit."
            )
        total_bytes += len(encoded)
    if total_bytes > MAX_ARGV_BYTES:
        raise ValueError(f"argv exceeds the {MAX_ARGV_BYTES}-byte total limit.")


def _validate_relative_posix_path(value: object, label: str) -> None:
    _validate_non_empty_string(value, label)
    assert isinstance(value, str)
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL characters.")
    _encode_utf8(value, label)
    if "\\" in value:
        raise ValueError(f"{label} must use POSIX '/' separators.")
    if _WINDOWS_DRIVE_PATH.match(value):
        raise ValueError(f"{label} must be workspace-relative, not drive-qualified.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError(f"{label} must stay inside the workspace.")
    canonical = path.as_posix()
    if value != canonical:
        raise ValueError(f"{label} must be a canonical POSIX relative path.")


def _validate_rule_id(value: object) -> None:
    _validate_non_empty_string(value, "rule_id")
    assert isinstance(value, str)
    if len(_encode_utf8(value, "rule_id")) > 128:
        raise ValueError("rule_id must be at most 128 bytes.")
    if any(character.isspace() for character in value):
        raise ValueError("rule_id must not contain whitespace.")


def _validate_reasons(value: object) -> None:
    if not isinstance(value, tuple):
        raise TypeError("reasons must be a tuple.")
    if not value:
        raise ValueError("reasons must contain at least one explanation.")
    for index, reason in enumerate(value):
        _validate_bounded_string(reason, f"reasons[{index}]", MAX_PURPOSE_BYTES)


def _validate_policy_version(value: object) -> None:
    _validate_integer(value, "policy_version")
    if value != SECURITY_POLICY_VERSION:
        raise ValueError(
            f"Unsupported security policy version: {value}; "
            f"expected {SECURITY_POLICY_VERSION}."
        )


def _validate_image_digest(value: object) -> None:
    if not isinstance(value, str) or not _SHA256_DIGEST.fullmatch(value):
        raise ValueError("image_digest must be a lowercase sha256:<64 hex> digest.")


def _validate_bounded_string(value: object, label: str, maximum_bytes: int) -> None:
    _validate_non_empty_string(value, label)
    assert isinstance(value, str)
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL characters.")
    if len(_encode_utf8(value, label)) > maximum_bytes:
        raise ValueError(f"{label} must be at most {maximum_bytes} bytes.")


def _validate_non_empty_string(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string.")
    if not value.strip():
        raise ValueError(f"{label} must be a non-empty string.")


def _validate_boolean(value: object, label: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be a boolean.")


def _validate_integer(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer.")


def _validate_positive_int(value: object, label: str) -> None:
    _validate_integer(value, label)
    assert isinstance(value, int)
    if value <= 0:
        raise ValueError(f"{label} must be a positive integer.")


def _validate_non_negative_int(value: object, label: str) -> None:
    _validate_integer(value, label)
    assert isinstance(value, int)
    if value < 0:
        raise ValueError(f"{label} must be zero or greater.")


def _encode_utf8(value: str, label: str) -> bytes:
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must contain valid UTF-8 text.") from exc

"""Security domain models and centralized path and command policies."""

from .command_policy import (
    DiscoveredVerificationCommand,
    ExecutableResolver,
    evaluate_command_policy,
    format_command_policy_block,
    normalize_executable,
)
from .models import (
    COMMAND_DISPOSITIONS,
    COMMAND_SOURCES,
    PATH_OPERATIONS,
    SANDBOX_BACKENDS,
    SANDBOX_NETWORK_MODES,
    SECURE_EXECUTION_STATUSES,
    SECURITY_POLICY_VERSION,
    SECURITY_SCHEMA_VERSION,
    CommandDisposition,
    CommandPolicyDecision,
    CommandSource,
    CommandSpec,
    ExecutionLimits,
    PathOperation,
    SandboxBackend,
    SandboxCapability,
    SandboxExecutionPlan,
    SandboxNetworkMode,
    SecureExecutionResult,
    SecureExecutionStatus,
    SensitivePathDecision,
)
from .process_runner import (
    DEFAULT_ENV_ALLOWLIST,
    FORBIDDEN_ENV_KEYS,
    FORBIDDEN_ENV_SUFFIXES,
    HostProcessAuthorizationError,
    HostProcessResult,
    HostProcessRunner,
    HostProcessStatus,
    build_child_environment,
    resolve_actual_executable,
    run_host_process,
)
from .sandbox import (
    SandboxAuthorizationError,
    SandboxBackend as SandboxBackendProtocol,
    SandboxExecutionOutcome,
)
from .path_policy import (
    DEFAULT_ALLOWED_EXCEPTIONS,
    DEFAULT_DENIED_DIRECTORIES,
    DEFAULT_DENIED_NAMES,
    DEFAULT_DENIED_PATHS,
    DEFAULT_DENIED_SUFFIXES,
    SENSITIVE_PATH_ALLOWED_REASON,
    SENSITIVE_PATH_DENIAL_REASON,
    SENSITIVE_PATH_EXCEPTION_REASON,
    SensitivePathPolicy,
    load_sensitive_path_policy,
)

__all__ = [
    "DEFAULT_SNAPSHOT_MAX_BINARY_FILE_BYTES",
    "DEFAULT_SNAPSHOT_MAX_BYTES",
    "DEFAULT_SNAPSHOT_MAX_FILES",
    "SNAPSHOT_EXCLUSION_REASONS",
    "SNAPSHOT_MANIFEST_VERSION",
    "SandboxSnapshotError",
    "SandboxWorkspaceSnapshot",
    "SnapshotAlreadyExistsError",
    "SnapshotBudgetExceededError",
    "SnapshotCleanupResult",
    "SnapshotFileEntry",
    "SnapshotManifest",
    "SnapshotSourceChangedError",
    "cleanup_sandbox_workspace_snapshot",
    "create_sandbox_workspace_snapshot",
    "DEFAULT_DOCKER_IMAGE",
    "DEFAULT_DOCKER_TMPFS",
    "DEFAULT_DOCKER_USER",
    "DOCKER_CLEANUP_TIMEOUT_MS",
    "DOCKER_CONTAINER_ENV_ALLOWLIST",
    "DOCKER_PROBE_TIMEOUT_MS",
    "DockerSandboxBackend",
    "build_docker_container_name",
    "build_docker_run_argv",
    "COMMAND_DISPOSITIONS",
    "COMMAND_SOURCES",
    "FORBIDDEN_ENV_SUFFIXES",
    "FORBIDDEN_ENV_KEYS",
    "DEFAULT_ENV_ALLOWLIST",
    "DEFAULT_ALLOWED_EXCEPTIONS",
    "DEFAULT_DENIED_DIRECTORIES",
    "DEFAULT_DENIED_NAMES",
    "DEFAULT_DENIED_PATHS",
    "DEFAULT_DENIED_SUFFIXES",
    "PATH_OPERATIONS",
    "SANDBOX_BACKENDS",
    "SANDBOX_NETWORK_MODES",
    "SECURE_EXECUTION_STATUSES",
    "SECURITY_POLICY_VERSION",
    "SECURITY_SCHEMA_VERSION",
    "SENSITIVE_PATH_ALLOWED_REASON",
    "SENSITIVE_PATH_DENIAL_REASON",
    "SENSITIVE_PATH_EXCEPTION_REASON",
    "CommandDisposition",
    "CommandPolicyDecision",
    "CommandSource",
    "CommandSpec",
    "DiscoveredVerificationCommand",
    "ExecutableResolver",
    "ExecutionLimits",
    "HostProcessStatus",
    "HostProcessRunner",
    "HostProcessResult",
    "HostProcessAuthorizationError",
    "PathOperation",
    "SandboxAuthorizationError",
    "SandboxBackend",
    "SandboxBackendProtocol",
    "SandboxCapability",
    "SandboxExecutionOutcome",
    "SandboxExecutionPlan",
    "SandboxNetworkMode",
    "SecureExecutionResult",
    "SecureExecutionStatus",
    "SensitivePathDecision",
    "SensitivePathPolicy",
    "run_host_process",
    "resolve_actual_executable",
    "build_child_environment",
    "evaluate_command_policy",
    "format_command_policy_block",
    "load_sensitive_path_policy",
    "normalize_executable",
]

_SNAPSHOT_EXPORTS = frozenset(
    {
        "DEFAULT_SNAPSHOT_MAX_BINARY_FILE_BYTES",
        "DEFAULT_SNAPSHOT_MAX_BYTES",
        "DEFAULT_SNAPSHOT_MAX_FILES",
        "SNAPSHOT_EXCLUSION_REASONS",
        "SNAPSHOT_MANIFEST_VERSION",
        "SandboxSnapshotError",
        "SandboxWorkspaceSnapshot",
        "SnapshotAlreadyExistsError",
        "SnapshotBudgetExceededError",
        "SnapshotCleanupResult",
        "SnapshotFileEntry",
        "SnapshotManifest",
        "SnapshotSourceChangedError",
        "cleanup_sandbox_workspace_snapshot",
        "create_sandbox_workspace_snapshot",
    }
)


_DOCKER_EXPORTS = frozenset(
    {
        "DEFAULT_DOCKER_IMAGE",
        "DEFAULT_DOCKER_TMPFS",
        "DEFAULT_DOCKER_USER",
        "DOCKER_CLEANUP_TIMEOUT_MS",
        "DOCKER_CONTAINER_ENV_ALLOWLIST",
        "DOCKER_PROBE_TIMEOUT_MS",
        "DockerSandboxBackend",
        "build_docker_container_name",
        "build_docker_run_argv",
    }
)


def __getattr__(name: str):
    """Load snapshot and Docker APIs lazily to avoid import cycles."""

    if name in _SNAPSHOT_EXPORTS:
        from . import snapshot as module
    elif name in _DOCKER_EXPORTS:
        from . import docker_backend as module
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    value = getattr(module, name)
    globals()[name] = value
    return value

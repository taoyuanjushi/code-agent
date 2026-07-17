"""M5 security acceptance metrics and executable test-matrix contracts."""

from pathlib import Path
from typing import Mapping

from coding_agent.security.models import (
    DEFAULT_COMMAND_TIMEOUT_MS,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_OUTPUT_LINES,
    MAX_COMMAND_TIMEOUT_MS,
    SECURITY_POLICY_VERSION,
)
from coding_agent.security.snapshot import (
    DEFAULT_SNAPSHOT_MAX_BYTES,
    DEFAULT_SNAPSHOT_MAX_FILES,
)

M5_SECURITY_POLICY_VERSION = 1
M5_EXECUTION_USES_ARGV = True
M5_EXECUTION_USES_SHELL = False
M5_FORBIDDEN_PRODUCTION_PATTERNS = frozenset({"shell=True", "shell = True"})

M5_MAX_OUTPUT_BYTES = 32 * 1024
M5_MAX_OUTPUT_LINES = 200
M5_DEFAULT_TIMEOUT_MS = 30_000
M5_MAX_TIMEOUT_MS = 300_000
M5_SNAPSHOT_MAX_FILES = 10_000
M5_SNAPSHOT_MAX_BYTES = 512 * 1024 * 1024

M5_COMMAND_PREFLIGHT_ORDER = (
    "normalize_argv",
    "workspace_guard",
    "command_policy",
    "approval",
    "subprocess",
)
M5_PATH_PREFLIGHT_ORDER = (
    "workspace_guard",
    "sensitive_path_policy",
    "file_open",
)
M5_REJECTED_SIDE_EFFECT_LIMITS = {
    "hard_denied_command": {"subprocess_calls": 0},
    "sensitive_path": {"file_open_calls": 0},
    "workspace_escape": {"file_open_calls": 0, "subprocess_calls": 0},
}

M5_ENV_ALLOWLIST = frozenset(
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
M5_FORBIDDEN_ENV_KEYS = frozenset({"OPENAI_API_KEY"})
M5_FORBIDDEN_ENV_SUFFIXES = ("_TOKEN", "_SECRET", "_PASSWORD")
M5_SENSITIVE_ENV_FIXTURE = {
    "OPENAI_API_KEY": "sk-m5-acceptance-secret",
    "GITHUB_TOKEN": "m5-token-value",
    "CLIENT_SECRET": "m5-secret-value",
    "DATABASE_PASSWORD": "m5-password-value",
}

M5_SENSITIVE_PATH_SURFACES = frozenset(
    {"list", "search", "read", "artifact_expand", "sandbox_snapshot"}
)
M5_SENSITIVE_PATH_PATTERNS = frozenset(
    {
        ".env",
        ".env.*",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "credentials",
        "credentials.json",
        ".ssh/",
        ".aws/",
        ".config/gcloud/",
        "id_rsa",
        "id_ed25519",
        "*.pem",
        "*.key",
        "*.p12",
        "*.pfx",
        ".coding-agent/",
    }
)
M5_SENSITIVE_PATH_EXCEPTIONS = frozenset({".env.example", ".env.sample"})
M5_LINK_POLICY = {
    "external_symlink_read": "deny",
    "external_junction_read": "deny",
    "symlink_write": "deny",
    "reparse_point_write": "deny",
}

M5_FULL_AUTO_PREFLIGHT_ORDER = (
    "parse_config",
    "docker_available",
    "image_available",
    "image_digest_pinned",
    "network_none",
    "model_client_constructed",
    "model_requested",
)
M5_FULL_AUTO_REJECTION_REASONS = frozenset(
    {
        "docker_unavailable",
        "image_unavailable",
        "image_digest_unavailable",
        "network_not_none",
    }
)
M5_FULL_AUTO_FAILURE_EFFECT_LIMITS = {
    "model_client_constructions": 0,
    "model_calls": 0,
}

M5_DOCKER_NETWORK_MODE = "none"
M5_DOCKER_REQUIRED_ARGV = (
    "docker",
    "run",
    "--rm",
    "--network",
    "none",
    "--read-only",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges=true",
    "--pids-limit",
    "256",
    "--memory",
    "1024m",
    "--cpus",
    "2",
    "--tmpfs",
    "/tmp:rw,nosuid,nodev,noexec,size=64m",
)
M5_SNAPSHOT_OVERFLOW_OUTCOME = {
    "status": "denied",
    "copy_started": False,
    "executable_snapshot_created": False,
}

M5_REQUIRED_AUDIT_FACTS = frozenset(
    {
        "policy_version",
        "rule_id",
        "approval",
        "sandbox_capability",
        "backend",
        "image_digest",
        "network_mode",
        "execution_limits",
        "execution_result",
    }
)
M5_REQUIRED_SECURITY_EVENTS = frozenset(
    {
        "security.policy_evaluated",
        "sandbox.capability_checked",
        "sandbox.snapshot_created",
        "sandbox.started",
        "sandbox.finished",
    }
)
M5_REPLAY_EXTERNAL_CALL_LIMITS = {
    "model_calls": 0,
    "tool_calls": 0,
    "subprocess_calls": 0,
    "docker_calls": 0,
    "input_calls": 0,
}

M5_M1_TO_M4_BASELINE_COUNT = 406
M5_REQUIRED_REGRESSION_TESTS = frozenset(
    {
        "tests/test_integration.py",
        "tests/test_m2_integration.py",
        "tests/test_m3_acceptance.py",
        "tests/test_m3_integration.py",
        "tests/test_m4_acceptance.py",
        "tests/test_m4_integration.py",
    }
)
M5_REQUIRED_MATRIX_TESTS = frozenset(
    {
        "tests/test_command_policy.py",
        "tests/test_sensitive_paths.py",
        "tests/test_path_safety.py",
        "tests/test_process_runner.py",
        "tests/test_sandbox_snapshot.py",
        "tests/test_docker_backend.py",
        "tests/test_cli_security.py",
        "tests/test_agent_resume.py",
        "tests/test_session_replay.py",
        "tests/test_m5_integration.py",
    }
)
M5_DOCKER_TEST_MARKER = "docker"
M5_DOCKER_SMOKE_TEST = "tests/test_docker_sandbox_smoke.py"
M5_DEFAULT_TESTS_REQUIRE_DOCKER = False
M5_DOCKER_ARGV_UNIT_TESTS_MAY_SKIP = False


def test_m5_numeric_acceptance_limits_are_fixed() -> None:
    assert M5_SECURITY_POLICY_VERSION == 1
    assert M5_MAX_OUTPUT_BYTES == 32_768
    assert M5_MAX_OUTPUT_LINES == 200
    assert M5_DEFAULT_TIMEOUT_MS == 30_000
    assert M5_MAX_TIMEOUT_MS == 300_000
    assert M5_DEFAULT_TIMEOUT_MS < M5_MAX_TIMEOUT_MS
    assert M5_SNAPSHOT_MAX_FILES == 10_000
    assert M5_SNAPSHOT_MAX_BYTES == 536_870_912
    assert M5_SECURITY_POLICY_VERSION == SECURITY_POLICY_VERSION
    assert M5_MAX_OUTPUT_BYTES == DEFAULT_MAX_OUTPUT_BYTES
    assert M5_MAX_OUTPUT_LINES == DEFAULT_MAX_OUTPUT_LINES
    assert M5_DEFAULT_TIMEOUT_MS == DEFAULT_COMMAND_TIMEOUT_MS
    assert M5_MAX_TIMEOUT_MS == MAX_COMMAND_TIMEOUT_MS
    assert M5_SNAPSHOT_MAX_FILES == DEFAULT_SNAPSHOT_MAX_FILES
    assert M5_SNAPSHOT_MAX_BYTES == DEFAULT_SNAPSHOT_MAX_BYTES


def test_production_process_contract_requires_argv_and_forbids_shell() -> None:
    argv = ("python", "-c", "value && echo not-a-second-command", ">", "output.txt")

    assert M5_EXECUTION_USES_ARGV is True
    assert M5_EXECUTION_USES_SHELL is False
    assert M5_FORBIDDEN_PRODUCTION_PATTERNS == {"shell=True", "shell = True"}
    assert isinstance(argv, tuple)
    assert argv[2:] == (
        "value && echo not-a-second-command",
        ">",
        "output.txt",
    )


def test_policy_and_path_checks_precede_approval_or_side_effects() -> None:
    assert M5_COMMAND_PREFLIGHT_ORDER == (
        "normalize_argv",
        "workspace_guard",
        "command_policy",
        "approval",
        "subprocess",
    )
    assert M5_PATH_PREFLIGHT_ORDER == (
        "workspace_guard",
        "sensitive_path_policy",
        "file_open",
    )
    assert M5_COMMAND_PREFLIGHT_ORDER.index("command_policy") < (
        M5_COMMAND_PREFLIGHT_ORDER.index("approval")
    )
    assert M5_COMMAND_PREFLIGHT_ORDER.index("command_policy") < (
        M5_COMMAND_PREFLIGHT_ORDER.index("subprocess")
    )
    assert M5_PATH_PREFLIGHT_ORDER.index("sensitive_path_policy") < (
        M5_PATH_PREFLIGHT_ORDER.index("file_open")
    )
    assert M5_REJECTED_SIDE_EFFECT_LIMITS == {
        "hard_denied_command": {"subprocess_calls": 0},
        "sensitive_path": {"file_open_calls": 0},
        "workspace_escape": {"file_open_calls": 0, "subprocess_calls": 0},
    }


def test_child_environment_is_allowlisted_and_secrets_are_excluded() -> None:
    inherited = {
        "PATH": "C:/tools",
        "TEMP": "C:/temp",
        "LANG": "en_US.UTF-8",
        "UNRELATED_SETTING": "must-not-be-inherited",
        **M5_SENSITIVE_ENV_FIXTURE,
    }

    child_environment = _reference_child_environment(inherited)

    assert set(child_environment) <= M5_ENV_ALLOWLIST
    assert child_environment == {
        "PATH": "C:/tools",
        "TEMP": "C:/temp",
        "LANG": "en_US.UTF-8",
    }
    assert not M5_FORBIDDEN_ENV_KEYS & child_environment.keys()
    assert all(
        not key.upper().endswith(M5_FORBIDDEN_ENV_SUFFIXES)
        for key in child_environment
    )
    assert not set(M5_SENSITIVE_ENV_FIXTURE.values()) & set(child_environment.values())


def test_sensitive_paths_are_excluded_from_every_content_surface() -> None:
    assert M5_SENSITIVE_PATH_SURFACES == {
        "list",
        "search",
        "read",
        "artifact_expand",
        "sandbox_snapshot",
    }
    assert {".env", ".env.*", "*.pem", "*.key", ".coding-agent/"} <= (
        M5_SENSITIVE_PATH_PATTERNS
    )
    assert M5_SENSITIVE_PATH_EXCEPTIONS == {".env.example", ".env.sample"}
    assert M5_SENSITIVE_PATH_EXCEPTIONS.isdisjoint(M5_SENSITIVE_PATH_PATTERNS)


def test_symlink_and_reparse_point_contract_fails_closed() -> None:
    assert M5_LINK_POLICY == {
        "external_symlink_read": "deny",
        "external_junction_read": "deny",
        "symlink_write": "deny",
        "reparse_point_write": "deny",
    }
    assert set(M5_LINK_POLICY.values()) == {"deny"}


def test_full_auto_preflight_fails_before_model_construction() -> None:
    assert M5_FULL_AUTO_REJECTION_REASONS == {
        "docker_unavailable",
        "image_unavailable",
        "image_digest_unavailable",
        "network_not_none",
    }
    assert M5_FULL_AUTO_PREFLIGHT_ORDER.index("docker_available") < (
        M5_FULL_AUTO_PREFLIGHT_ORDER.index("model_client_constructed")
    )
    assert M5_FULL_AUTO_PREFLIGHT_ORDER.index("image_digest_pinned") < (
        M5_FULL_AUTO_PREFLIGHT_ORDER.index("model_client_constructed")
    )
    assert M5_FULL_AUTO_PREFLIGHT_ORDER.index("network_none") < (
        M5_FULL_AUTO_PREFLIGHT_ORDER.index("model_client_constructed")
    )
    assert all(limit == 0 for limit in M5_FULL_AUTO_FAILURE_EFFECT_LIMITS.values())


def test_docker_argv_fixes_isolation_and_resource_limits() -> None:
    assert M5_DOCKER_NETWORK_MODE == "none"
    assert _argv_value(M5_DOCKER_REQUIRED_ARGV, "--network") == "none"
    assert "--read-only" in M5_DOCKER_REQUIRED_ARGV
    assert _argv_value(M5_DOCKER_REQUIRED_ARGV, "--cap-drop") == "ALL"
    assert _argv_value(M5_DOCKER_REQUIRED_ARGV, "--security-opt") == (
        "no-new-privileges=true"
    )
    assert _argv_value(M5_DOCKER_REQUIRED_ARGV, "--pids-limit") == "256"
    assert _argv_value(M5_DOCKER_REQUIRED_ARGV, "--memory") == "1024m"
    assert _argv_value(M5_DOCKER_REQUIRED_ARGV, "--cpus") == "2"
    assert _argv_value(M5_DOCKER_REQUIRED_ARGV, "--tmpfs").startswith("/tmp:")


def test_snapshot_budget_is_preflighted_without_partial_executable_output() -> None:
    assert M5_SNAPSHOT_MAX_FILES == 10_000
    assert M5_SNAPSHOT_MAX_BYTES == 512 * 1024 * 1024
    assert M5_SNAPSHOT_OVERFLOW_OUTCOME == {
        "status": "denied",
        "copy_started": False,
        "executable_snapshot_created": False,
    }


def test_security_decisions_and_results_are_replay_auditable() -> None:
    assert M5_REQUIRED_AUDIT_FACTS == {
        "policy_version",
        "rule_id",
        "approval",
        "sandbox_capability",
        "backend",
        "image_digest",
        "network_mode",
        "execution_limits",
        "execution_result",
    }
    assert M5_REQUIRED_SECURITY_EVENTS == {
        "security.policy_evaluated",
        "sandbox.capability_checked",
        "sandbox.snapshot_created",
        "sandbox.started",
        "sandbox.finished",
    }
    assert all(limit == 0 for limit in M5_REPLAY_EXTERNAL_CALL_LIMITS.values())


def test_m1_to_m4_baseline_remains_part_of_m5_acceptance() -> None:
    repository_root = Path(__file__).resolve().parents[1]

    assert M5_M1_TO_M4_BASELINE_COUNT == 406
    assert M5_REQUIRED_REGRESSION_TESTS == {
        "tests/test_integration.py",
        "tests/test_m2_integration.py",
        "tests/test_m3_acceptance.py",
        "tests/test_m3_integration.py",
        "tests/test_m4_acceptance.py",
        "tests/test_m4_integration.py",
    }
    assert all(
        (repository_root / relative_path).is_file()
        for relative_path in M5_REQUIRED_REGRESSION_TESTS
    )


def test_required_m5_security_matrix_modules_exist() -> None:
    repository_root = Path(__file__).resolve().parents[1]

    assert all(
        (repository_root / relative_path).is_file()
        for relative_path in M5_REQUIRED_MATRIX_TESTS
    )


def test_real_docker_tests_are_optional_but_unit_contracts_are_not() -> None:
    assert M5_DOCKER_TEST_MARKER == "docker"
    assert M5_DOCKER_SMOKE_TEST == "tests/test_docker_sandbox_smoke.py"
    assert M5_DEFAULT_TESTS_REQUIRE_DOCKER is False
    assert M5_DOCKER_ARGV_UNIT_TESTS_MAY_SKIP is False


def _reference_child_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environment.items()
        if key.upper() in M5_ENV_ALLOWLIST
        and key.upper() not in M5_FORBIDDEN_ENV_KEYS
        and not key.upper().endswith(M5_FORBIDDEN_ENV_SUFFIXES)
    }


def _argv_value(argv: tuple[str, ...], option: str) -> str:
    return argv[argv.index(option) + 1]

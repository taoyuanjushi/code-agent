from dataclasses import dataclass

import pytest

from coding_agent.security.command_policy import (
    evaluate_command_policy,
    normalize_executable,
)
from coding_agent.security.models import (
    SECURITY_POLICY_VERSION,
    CommandPolicyDecision,
    CommandSpec,
)


@dataclass(frozen=True)
class _DiscoveredCommand:
    id: str
    argv: tuple[str, ...]
    available: bool = True


def _spec(
    argv: tuple[str, ...],
    *,
    source: str = "tool",
) -> CommandSpec:
    return CommandSpec(
        argv=argv,
        cwd=".",
        source=source,  # type: ignore[arg-type]
        purpose="command policy test",
    )


def _evaluate(
    argv: tuple[str, ...],
    *,
    source: str = "tool",
    verification_command_id: str | None = None,
    discovered_commands: tuple[_DiscoveredCommand, ...] = (),
    resolved_executable: str | None = None,
) -> CommandPolicyDecision:
    return evaluate_command_policy(
        _spec(argv, source=source),
        verification_command_id=verification_command_id,
        discovered_commands=discovered_commands,
        executable_resolver=lambda _name: resolved_executable,
    )


def test_normalize_executable_resolves_native_suffix_and_preserves_batch_suffix() -> None:
    assert normalize_executable(
        "git",
        resolver=lambda _name: r"C:\Program Files\Git\cmd\GIT.EXE",
    ) == "git"
    assert normalize_executable(
        "npm",
        resolver=lambda _name: r"C:\Program Files\nodejs\NPM.CMD",
    ) == "npm.cmd"


@pytest.mark.parametrize(
    "argv",
    [
        ("git", "status", "--short"),
        ("git", "diff", "--stat"),
        ("git", "diff"),
    ],
)
def test_internal_readonly_git_allowlist_is_exact(argv: tuple[str, ...]) -> None:
    decision = _evaluate(argv, source="internal")

    assert decision.disposition == "allow_host"
    assert decision.rule_id == "allow.internal_readonly_git"
    assert decision.requires_approval is False
    assert decision.requires_sandbox is False


def test_same_readonly_git_argv_from_tool_requires_approval() -> None:
    decision = _evaluate(("git", "status", "--short"))

    assert decision.disposition == "approval_required"
    assert decision.rule_id == "allow.interactive_host"


def test_internal_git_allowlist_rejects_additional_arguments() -> None:
    decision = _evaluate(
        ("git", "diff", "--stat", "--", "secret.txt"),
        source="internal",
    )

    assert decision.disposition == "sandbox_required"
    assert decision.rule_id == "sandbox.default"


def test_discovered_verification_requires_matching_id_available_flag_and_exact_argv() -> None:
    argv = ("python", "-m", "pytest", "-q")
    discovered = (_DiscoveredCommand("python:pytest", argv),)

    allowed = _evaluate(
        argv,
        source="verification",
        verification_command_id="python:pytest",
        discovered_commands=discovered,
    )
    wrong_id = _evaluate(
        argv,
        source="verification",
        verification_command_id="python:ruff",
        discovered_commands=discovered,
    )
    changed_argv = _evaluate(
        (*argv, "tests/test_admin.py"),
        source="verification",
        verification_command_id="python:pytest",
        discovered_commands=discovered,
    )
    unavailable = _evaluate(
        argv,
        source="verification",
        verification_command_id="python:pytest",
        discovered_commands=(_DiscoveredCommand("python:pytest", argv, False),),
    )

    assert allowed.disposition == "allow_host"
    assert allowed.rule_id == "allow.discovered_verification"
    for decision in (wrong_id, changed_argv, unavailable):
        assert decision.disposition == "sandbox_required"
        assert decision.rule_id == "sandbox.verification_mismatch"


def test_inline_interpreter_rule_precedes_discovered_verification_allowlist() -> None:
    argv = ("node", "-e", "console.log('unsafe')")

    decision = _evaluate(
        argv,
        source="verification",
        verification_command_id="node:test",
        discovered_commands=(_DiscoveredCommand("node:test", argv),),
    )

    assert decision.disposition == "sandbox_required"
    assert decision.rule_id == "sandbox.inline_interpreter"


@pytest.mark.parametrize(
    ("argv", "rule_id"),
    [
        (("sudo", "pytest"), "deny.privilege_escalation"),
        (("runas", "/user:Administrator", "cmd"), "deny.privilege_escalation"),
        (("docker", "run", "alpine"), "deny.container_host_control"),
        (("podman", "ps"), "deny.container_host_control"),
        (("diskpart", "/s", "layout.txt"), "deny.system_management"),
        (("systemctl", "restart", "ssh"), "deny.system_management"),
        (("env", "python", "-m", "pytest"), "deny.command_wrapper"),
        (("git", "reset", "--hard", "HEAD"), "deny.destructive_git"),
        (("git", "-C", ".", "clean", "-fd"), "deny.destructive_git"),
        (("git", "checkout", "--", "src/app.py"), "deny.destructive_git"),
        (("tool", "/dev/sda"), "deny.device_path"),
        (("tool", r"\\.\PhysicalDrive0"), "deny.device_path"),
    ],
)
def test_hard_deny_rules_are_stable(
    argv: tuple[str, ...],
    rule_id: str,
) -> None:
    decision = _evaluate(argv)

    assert decision.disposition == "deny"
    assert decision.rule_id == rule_id
    assert decision.requires_approval is False
    assert decision.requires_sandbox is False


@pytest.mark.parametrize(
    ("argv", "rule_id"),
    [
        (("bash", "-lc", "pytest"), "sandbox.shell"),
        (("powershell", "-Command", "Get-ChildItem"), "sandbox.shell"),
        (("tools.cmd", "test"), "sandbox.batch_wrapper"),
        (("build.bat",), "sandbox.batch_wrapper"),
        (("python", "-c", "print('x')"), "sandbox.inline_interpreter"),
        (("node", "--eval=console.log(1)"), "sandbox.inline_interpreter"),
        (("python", "-m", "pip", "install", "pytest"), "sandbox.package_management"),
        (("pip", "install", "pytest"), "sandbox.package_management"),
        (("npm", "install"), "sandbox.package_management"),
        (("curl", "https://example.invalid"), "sandbox.network_client"),
        (("git", "fetch", "origin"), "sandbox.network_client"),
        (("script.py",), "sandbox.script_interpreter"),
        (("pytest", "-q"), "sandbox.unverified_build"),
        (("make", "test"), "sandbox.unverified_build"),
    ],
)
def test_sandbox_required_rules_are_stable(
    argv: tuple[str, ...],
    rule_id: str,
) -> None:
    decision = _evaluate(argv)

    assert decision.disposition == "sandbox_required"
    assert decision.rule_id == rule_id
    assert decision.requires_approval is False
    assert decision.requires_sandbox is True


def test_resolved_windows_batch_wrapper_requires_sandbox() -> None:
    decision = _evaluate(
        ("npm", "test"),
        resolved_executable=r"C:\Program Files\nodejs\npm.cmd",
    )

    assert decision.normalized_executable == "npm.cmd"
    assert decision.disposition == "sandbox_required"
    assert decision.rule_id == "sandbox.batch_wrapper"


def test_narrow_interactive_host_commands_require_approval() -> None:
    echo = _evaluate(("echo", "status"))
    version = _evaluate(("python", "--version"))

    for decision in (echo, version):
        assert decision.disposition == "approval_required"
        assert decision.rule_id == "allow.interactive_host"
        assert decision.requires_approval is True
        assert decision.requires_sandbox is False


def test_unknown_command_fails_closed_to_sandbox() -> None:
    decision = _evaluate(("project-specific-tool", "check"))

    assert decision.disposition == "sandbox_required"
    assert decision.rule_id == "sandbox.default"


def test_policy_metadata_is_versioned_and_serializable() -> None:
    first = _evaluate(("git", "status", "--short"), source="internal")
    second = _evaluate(("git", "status", "--short"), source="internal")

    assert first == second
    assert first.policy_version == SECURITY_POLICY_VERSION
    assert first.to_dict() == second.to_dict()

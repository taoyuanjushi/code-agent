from __future__ import annotations

import re
import shutil
from collections.abc import Callable, Iterable
from typing import Protocol

from .models import CommandPolicyDecision, CommandSpec

ExecutableResolver = Callable[[str], str | None]


class DiscoveredVerificationCommand(Protocol):
    """Minimal discovery record required by the command policy."""

    id: str
    argv: tuple[str, ...]
    available: bool


_PRIVILEGE_EXECUTABLES = frozenset(
    {"sudo", "su", "doas", "runas", "pkexec", "gsudo"}
)
_CONTAINER_HOST_EXECUTABLES = frozenset(
    {"docker", "docker-compose", "podman", "podman-compose"}
)
_SYSTEM_MANAGEMENT_EXECUTABLES = frozenset(
    {
        "bcdedit",
        "chown",
        "diskpart",
        "fdisk",
        "format",
        "fsutil",
        "halt",
        "icacls",
        "manage-bde",
        "mkfs",
        "mount",
        "net",
        "netsh",
        "parted",
        "poweroff",
        "reboot",
        "reg",
        "regedit",
        "sc",
        "service",
        "shutdown",
        "systemctl",
        "takeown",
        "umount",
        "vssadmin",
        "wmic",
    }
)
_COMMAND_WRAPPERS = frozenset(
    {"command", "env", "eval", "exec", "nohup", "script", "start", "xargs"}
)
_SHELL_EXECUTABLES = frozenset(
    {
        "ash",
        "bash",
        "cmd",
        "command.com",
        "csh",
        "dash",
        "fish",
        "ksh",
        "powershell",
        "pwsh",
        "sh",
        "tcsh",
        "wsl",
        "zsh",
    }
)
_PYTHON_EXECUTABLES = frozenset(
    {"python", "python2", "python3", "py", "pypy", "pypy3"}
)
_NODE_EXECUTABLES = frozenset({"node", "nodejs", "deno", "bun"})
_SCRIPT_INTERPRETERS = frozenset(
    {
        *_PYTHON_EXECUTABLES,
        *_NODE_EXECUTABLES,
        "java",
        "jruby",
        "lua",
        "perl",
        "php",
        "ruby",
        "tclsh",
    }
)
_NETWORK_CLIENTS = frozenset(
    {
        "aria2c",
        "curl",
        "ftp",
        "gh",
        "glab",
        "nc",
        "ncat",
        "scp",
        "sftp",
        "ssh",
        "telnet",
        "wget",
    }
)
_BUILD_EXECUTABLES = frozenset(
    {
        "cargo",
        "cmake",
        "dotnet",
        "gradle",
        "gradlew",
        "jest",
        "make",
        "mvn",
        "mvnw",
        "mypy",
        "ninja",
        "npm",
        "pnpm",
        "pytest",
        "ruff",
        "tox",
        "tsc",
        "uv",
        "vitest",
        "yarn",
    }
)
_INTERACTIVE_HOST_EXECUTABLES = frozenset({"echo", "printf"})
_VERSION_QUERY_EXECUTABLES = frozenset(
    {
        "git",
        "node",
        "npm",
        "pnpm",
        "python",
        "python3",
        "py",
        "ruff",
        "uv",
        "yarn",
    }
)
_INTERNAL_GIT_ARGV = frozenset(
    {
        ("status", "--short"),
        ("diff", "--stat"),
        ("diff",),
    }
)
_SCRIPT_SUFFIXES = (
    ".bash",
    ".cjs",
    ".js",
    ".lua",
    ".mjs",
    ".php",
    ".pl",
    ".ps1",
    ".py",
    ".rb",
    ".sh",
    ".zsh",
)
_WINDOWS_RESERVED_DEVICE = re.compile(
    r"^(?:aux|con|nul|prn|com[1-9]|lpt[1-9])(?::|$)",
    flags=re.IGNORECASE,
)


def normalize_executable(
    executable: str,
    *,
    resolver: ExecutableResolver = shutil.which,
) -> str:
    """Return a stable, case-insensitive executable identity.

    Bare executable names are resolved through ``PATH`` when possible so a
    Windows ``npm.cmd``/``tool.bat`` wrapper cannot masquerade as a native
    executable. Native ``.exe`` and ``.com`` suffixes are removed; script and
    batch suffixes remain visible to policy rules.
    """

    candidate = executable
    if "/" not in executable and "\\" not in executable:
        try:
            resolved = resolver(executable)
        except (OSError, TypeError, ValueError):
            resolved = None
        if isinstance(resolved, str) and resolved:
            candidate = resolved

    basename = candidate.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for suffix in (".exe", ".com"):
        if basename.endswith(suffix):
            return basename[: -len(suffix)]
    return basename


def evaluate_command_policy(
    command: CommandSpec,
    *,
    verification_command_id: str | None = None,
    discovered_commands: Iterable[DiscoveredVerificationCommand] = (),
    executable_resolver: ExecutableResolver = shutil.which,
) -> CommandPolicyDecision:
    """Classify a structured command before approval or process creation."""

    if not isinstance(command, CommandSpec):
        raise TypeError("command must be a CommandSpec instance.")
    if verification_command_id is not None and (
        not isinstance(verification_command_id, str)
        or not verification_command_id.strip()
    ):
        raise ValueError("verification_command_id must be non-empty when provided.")

    executable = normalize_executable(
        command.argv[0],
        resolver=executable_resolver,
    )
    arguments = command.argv[1:]

    if _contains_device_path(command.argv):
        return _decision(
            "deny",
            "deny.device_path",
            "Command arguments reference an operating-system device path.",
            executable,
        )
    if executable in _PRIVILEGE_EXECUTABLES:
        return _decision(
            "deny",
            "deny.privilege_escalation",
            "Privilege-escalation commands are never permitted.",
            executable,
        )
    if executable in _CONTAINER_HOST_EXECUTABLES:
        return _decision(
            "deny",
            "deny.container_host_control",
            "Model commands cannot control the host container runtime.",
            executable,
        )
    if executable in _SYSTEM_MANAGEMENT_EXECUTABLES:
        return _decision(
            "deny",
            "deny.system_management",
            "Disk, service, permission, and operating-system management commands are denied.",
            executable,
        )
    if executable in _COMMAND_WRAPPERS:
        return _decision(
            "deny",
            "deny.command_wrapper",
            "Command wrappers that obscure the final executable are denied.",
            executable,
        )
    if executable == "git" and _is_destructive_git(arguments):
        return _decision(
            "deny",
            "deny.destructive_git",
            "Destructive Git operations are never permitted through command execution.",
            executable,
        )

    if executable in _SHELL_EXECUTABLES:
        return _decision(
            "sandbox_required",
            "sandbox.shell",
            "Shell and PowerShell execution requires an isolated sandbox.",
            executable,
        )
    if executable.endswith((".cmd", ".bat")):
        return _decision(
            "sandbox_required",
            "sandbox.batch_wrapper",
            "Windows batch wrappers require an isolated sandbox.",
            executable,
        )
    if _is_inline_interpreter(executable, arguments):
        return _decision(
            "sandbox_required",
            "sandbox.inline_interpreter",
            "Inline interpreter code can execute arbitrary instructions.",
            executable,
        )
    if _is_package_management(executable, arguments):
        return _decision(
            "sandbox_required",
            "sandbox.package_management",
            "Package installation or dependency modification requires a sandbox.",
            executable,
        )
    if _is_network_client(executable, arguments):
        return _decision(
            "sandbox_required",
            "sandbox.network_client",
            "Network-capable commands require a sandbox with explicit network policy.",
            executable,
        )

    discovered = tuple(discovered_commands)
    if command.source == "verification":
        if _matches_discovered_verification(
            command,
            verification_command_id=verification_command_id,
            discovered_commands=discovered,
        ):
            return _decision(
                "allow_host",
                "allow.discovered_verification",
                "The command ID and argv exactly match an available discovery result.",
                executable,
            )
        return _decision(
            "sandbox_required",
            "sandbox.verification_mismatch",
            "Verification execution did not exactly match a current discovery result.",
            executable,
        )

    if command.source == "internal" and (
        executable == "git" and arguments in _INTERNAL_GIT_ARGV
    ):
        return _decision(
            "allow_host",
            "allow.internal_readonly_git",
            "The command exactly matches the internal read-only Git allowlist.",
            executable,
        )

    if command.source == "tool" and _is_interactive_host_command(
        executable,
        arguments,
    ):
        return _decision(
            "approval_required",
            "allow.interactive_host",
            "This narrowly scoped host command requires explicit approval.",
            executable,
        )
    if _is_script_or_interpreter(executable):
        return _decision(
            "sandbox_required",
            "sandbox.script_interpreter",
            "Undiscovered scripts and interpreter commands require a sandbox.",
            executable,
        )
    if _is_unverified_build_command(executable, arguments):
        return _decision(
            "sandbox_required",
            "sandbox.unverified_build",
            "Undiscovered build, test, lint, or type-check commands require a sandbox.",
            executable,
        )

    return _decision(
        "sandbox_required",
        "sandbox.default",
        "Commands not covered by a narrow host allowlist require a sandbox.",
        executable,
    )


def format_command_policy_block(decision: CommandPolicyDecision) -> str:
    """Return the stable fail-closed message for a non-host decision."""

    if not isinstance(decision, CommandPolicyDecision):
        raise TypeError("decision must be a CommandPolicyDecision instance.")
    reason = " ".join(decision.reasons)
    if decision.disposition == "deny":
        return (
            f"Command denied by security policy [{decision.rule_id}]: {reason}"
        )
    if decision.disposition == "sandbox_required":
        return (
            "Command requires sandbox execution, but no sandbox backend is "
            f"available [{decision.rule_id}]: {reason}"
        )
    raise ValueError(
        "Only deny and sandbox_required decisions produce blocking messages."
    )

def _decision(
    disposition: str,
    rule_id: str,
    reason: str,
    executable: str,
) -> CommandPolicyDecision:
    return CommandPolicyDecision(
        disposition=disposition,  # type: ignore[arg-type]
        rule_id=rule_id,
        reasons=(reason,),
        normalized_executable=executable,
        requires_approval=disposition == "approval_required",
        requires_sandbox=disposition == "sandbox_required",
    )


def _contains_device_path(argv: tuple[str, ...]) -> bool:
    for argument in argv:
        normalized = argument.replace("\\", "/").casefold()
        if normalized.startswith(("/dev/", "/proc/sys/", "/sys/")):
            return True
        if normalized.startswith(("//./", "//?/globalroot/")):
            return True
        basename = normalized.rsplit("/", 1)[-1]
        if _WINDOWS_RESERVED_DEVICE.fullmatch(basename):
            return True
    return False


def _git_subcommand(arguments: tuple[str, ...]) -> tuple[str | None, tuple[str, ...]]:
    options_with_value = {
        "-c",
        "-C",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--work-tree",
    }
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            index += 1
            break
        if argument in options_with_value:
            index += 2
            continue
        if any(
            argument.startswith(f"{option}=")
            for option in options_with_value
            if option.startswith("--")
        ):
            index += 1
            continue
        if argument.startswith("-"):
            index += 1
            continue
        return argument.casefold(), arguments[index + 1 :]
    if index < len(arguments):
        return arguments[index].casefold(), arguments[index + 1 :]
    return None, ()


def _is_destructive_git(arguments: tuple[str, ...]) -> bool:
    subcommand, remaining = _git_subcommand(arguments)
    if subcommand in {"checkout", "clean", "restore", "switch"}:
        return True
    if subcommand == "reset":
        return any(argument.casefold() in {"--hard", "--keep", "--merge"} for argument in remaining)
    return False


def _is_inline_interpreter(executable: str, arguments: tuple[str, ...]) -> bool:
    lowered = tuple(argument.casefold() for argument in arguments)
    if executable in _PYTHON_EXECUTABLES:
        return any(
            argument == "-c" or (argument.startswith("-c") and len(argument) > 2)
            for argument in lowered
        )
    if executable in _NODE_EXECUTABLES:
        return any(
            argument in {"-e", "--eval", "-p", "--print"}
            or argument.startswith(("--eval=", "--print="))
            for argument in lowered
        )
    return False


def _is_package_management(executable: str, arguments: tuple[str, ...]) -> bool:
    lowered = tuple(argument.casefold() for argument in arguments)
    if executable in _PYTHON_EXECUTABLES and len(lowered) >= 3:
        if lowered[0:2] == ("-m", "pip"):
            return lowered[2] in {
                "download",
                "install",
                "uninstall",
                "wheel",
            }
    if not lowered:
        return False

    subcommand = lowered[0]
    mutating: dict[str, frozenset[str]] = {
        "pip": frozenset({"download", "install", "uninstall", "wheel"}),
        "pip3": frozenset({"download", "install", "uninstall", "wheel"}),
        "npm": frozenset({"ci", "i", "install", "uninstall", "remove", "update"}),
        "pnpm": frozenset({"add", "install", "remove", "update"}),
        "yarn": frozenset({"add", "install", "remove", "upgrade"}),
        "bun": frozenset({"add", "install", "remove", "update"}),
        "poetry": frozenset({"add", "install", "remove", "update"}),
        "uv": frozenset({"add", "remove", "sync"}),
        "conda": frozenset({"create", "install", "remove", "update"}),
        "gem": frozenset({"install", "uninstall", "update"}),
    }
    if executable in mutating and subcommand in mutating[executable]:
        return True
    if executable == "uv" and len(lowered) >= 2 and lowered[:2] == ("pip", "install"):
        return True
    if executable == "npm" and lowered[:2] == ("audit", "fix"):
        return True
    return False


def _is_network_client(executable: str, arguments: tuple[str, ...]) -> bool:
    if executable in _NETWORK_CLIENTS:
        return True
    if executable == "git":
        subcommand, _remaining = _git_subcommand(arguments)
        return subcommand in {"clone", "fetch", "pull", "push"}
    return False


def _matches_discovered_verification(
    command: CommandSpec,
    *,
    verification_command_id: str | None,
    discovered_commands: tuple[DiscoveredVerificationCommand, ...],
) -> bool:
    if verification_command_id is None:
        return False
    return any(
        discovered.id == verification_command_id
        and discovered.available
        and tuple(discovered.argv) == command.argv
        for discovered in discovered_commands
    )


def _is_script_or_interpreter(executable: str) -> bool:
    return executable in _SCRIPT_INTERPRETERS or executable.endswith(_SCRIPT_SUFFIXES)


def _is_unverified_build_command(
    executable: str,
    arguments: tuple[str, ...],
) -> bool:
    if executable in _BUILD_EXECUTABLES:
        return True
    if executable in _PYTHON_EXECUTABLES and arguments[:1] == ("-m",):
        return True
    return False


def _is_interactive_host_command(
    executable: str,
    arguments: tuple[str, ...],
) -> bool:
    if executable in _INTERACTIVE_HOST_EXECUTABLES:
        return True
    if executable == "git" and arguments in _INTERNAL_GIT_ARGV:
        return True
    if executable in _VERSION_QUERY_EXECUTABLES and arguments in {
        ("--version",),
        ("-V",),
        ("-v",),
        ("version",),
    }:
        return True
    return False



from dataclasses import dataclass, replace
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess as _subprocess
import sys
import time
import tomllib
from typing import Any, Callable, Literal

from .path_safety import resolve_inside_workspace


class _SubprocessProxy:
    """Keep verification monkeypatches isolated from other subprocess users."""

    run = staticmethod(_subprocess.run)
    TimeoutExpired = _subprocess.TimeoutExpired
    DEVNULL = _subprocess.DEVNULL


subprocess = _SubprocessProxy()

VerificationKind = Literal["test", "lint", "typecheck", "build"]
VerificationStatus = Literal[
    "passed",
    "failed",
    "timed_out",
    "not_found",
    "error",
]

VERIFICATION_KINDS: frozenset[str] = frozenset(
    {"test", "lint", "typecheck", "build"}
)
VERIFICATION_STATUSES: frozenset[str] = frozenset(
    {"passed", "failed", "timed_out", "not_found", "error"}
)

DEFAULT_VERIFICATION_TIMEOUT_MS = 30_000
MAX_VERIFICATION_TIMEOUT_MS = 300_000
DEFAULT_VERIFICATION_MAX_OUTPUT_BYTES = 32 * 1024
DEFAULT_VERIFICATION_MAX_OUTPUT_LINES = 200
MAX_VERIFICATION_OUTPUT_BYTES = 1_048_576
MAX_VERIFICATION_OUTPUT_LINES = 5_000
PASSED_VERIFICATION_MAX_OUTPUT_BYTES = 4 * 1024
PASSED_VERIFICATION_MAX_OUTPUT_LINES = 20
OUTPUT_CONTEXT_LINES = 2

_ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:\][^\x07]*(?:\x07|\x1B\\)|\[[0-?]*[ -/]*[@-~])"
)
_ACTIONABLE_OUTPUT_RE = re.compile(
    r"\b(?:error|failed|failure|traceback|assert(?:ion)?|exception)\b|"
    r"(?:[A-Za-z]:)?\S+\."
    r"(?:py|pyi|js|jsx|ts|tsx|java|go|rs|cpp|c|h|cs|rb|php)"
    r":\d+(?::\d+)?|"
    r'File\s+"[^"]+",\s+line\s+\d+',
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class VerificationCommand:
    id: str
    kind: VerificationKind
    argv: tuple[str, ...]
    cwd: str
    source: str
    available: bool
    unavailable_reason: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _validate_command_id(self.id)
        _validate_kind(self.kind)
        _validate_argv(self.argv)
        _validate_absolute_path(self.cwd, "cwd")
        _validate_non_empty_string(self.source, "source")

        if not isinstance(self.available, bool):
            raise TypeError("available must be a boolean.")
        if self.unavailable_reason is not None:
            _validate_non_empty_string(
                self.unavailable_reason,
                "unavailable_reason",
            )
        if self.available and self.unavailable_reason is not None:
            raise ValueError(
                "available commands cannot include an unavailable_reason."
            )
        if not self.available and self.unavailable_reason is None:
            raise ValueError(
                "unavailable commands must include an unavailable_reason."
            )
        if self.reason is not None:
            _validate_non_empty_string(self.reason, "reason")


@dataclass(frozen=True)
class VerificationDiscoveryResult:
    workspace: str
    commands: tuple[VerificationCommand, ...]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        workspace = _validate_absolute_path(self.workspace, "workspace")
        if not isinstance(self.commands, tuple):
            raise TypeError("commands must be a tuple.")
        if not all(isinstance(command, VerificationCommand) for command in self.commands):
            raise TypeError("commands must contain only VerificationCommand values.")

        command_ids: set[str] = set()
        for command in self.commands:
            if command.id in command_ids:
                raise ValueError(f"Duplicate verification command id: {command.id}")
            command_ids.add(command.id)

            resolved_cwd = resolve_inside_workspace(workspace, command.cwd)
            if resolved_cwd != Path(command.cwd).resolve():
                raise ValueError(
                    f"Verification command cwd is not normalized: {command.cwd}"
                )

        _validate_string_tuple(self.warnings, "warnings")
        _validate_string_tuple(self.errors, "errors")


@dataclass(frozen=True)
class OutputSummary:
    output: str
    truncated: bool
    omitted_lines: int
    omitted_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.output, str):
            raise TypeError("output must be a string.")
        if not isinstance(self.truncated, bool):
            raise TypeError("truncated must be a boolean.")
        _validate_non_negative_int(self.omitted_lines, "omitted_lines")
        _validate_non_negative_int(self.omitted_bytes, "omitted_bytes")
        if not self.truncated and (self.omitted_lines or self.omitted_bytes):
            raise ValueError(
                "omitted line or byte counts require truncated=True."
            )
        if self.truncated and not (self.omitted_lines or self.omitted_bytes):
            raise ValueError(
                "truncated=True requires omitted line or byte counts."
            )


@dataclass(frozen=True)
class VerificationResult:
    command_id: str
    kind: VerificationKind
    status: VerificationStatus
    argv: tuple[str, ...]
    cwd: str
    exit_code: int | None
    duration_ms: int
    output: str
    truncated: bool
    omitted_lines: int
    omitted_bytes: int
    attempt: int

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)
        _validate_kind(self.kind)
        if self.status not in VERIFICATION_STATUSES:
            raise ValueError(f"Unsupported verification status: {self.status}")
        _validate_argv(self.argv)
        _validate_absolute_path(self.cwd, "cwd")

        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise TypeError("exit_code must be an integer or None.")
        if isinstance(self.duration_ms, bool) or not isinstance(self.duration_ms, int):
            raise TypeError("duration_ms must be an integer.")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be zero or greater.")
        if not isinstance(self.output, str):
            raise TypeError("output must be a string.")
        if not isinstance(self.truncated, bool):
            raise TypeError("truncated must be a boolean.")

        _validate_non_negative_int(self.omitted_lines, "omitted_lines")
        _validate_non_negative_int(self.omitted_bytes, "omitted_bytes")
        if not self.truncated and (self.omitted_lines or self.omitted_bytes):
            raise ValueError(
                "omitted line or byte counts require truncated=True."
            )
        if self.truncated and not (self.omitted_lines or self.omitted_bytes):
            raise ValueError(
                "truncated=True requires omitted line or byte counts."
            )

        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise TypeError("attempt must be an integer.")
        if self.attempt <= 0:
            raise ValueError("attempt must be a positive integer.")

        if self.status == "passed" and self.exit_code != 0:
            raise ValueError("passed results must have exit_code=0.")
        if self.status == "failed" and (
            self.exit_code is None or self.exit_code == 0
        ):
            raise ValueError("failed results must have a non-zero exit_code.")
        if self.status in {"timed_out", "not_found", "error"} and self.exit_code is not None:
            raise ValueError(
                f"{self.status} results must not include an exit_code."
            )


def create_verification_command(
    *,
    workspace: str | Path,
    command_id: str,
    kind: VerificationKind,
    argv: tuple[str, ...],
    cwd: str | Path = ".",
    source: str,
    available: bool,
    unavailable_reason: str | None = None,
    reason: str | None = None,
) -> VerificationCommand:
    workspace_path = Path(workspace).resolve()
    resolved_cwd = resolve_inside_workspace(workspace_path, str(cwd))

    return VerificationCommand(
        id=command_id,
        kind=kind,
        argv=argv,
        cwd=str(resolved_cwd),
        source=source,
        available=available,
        unavailable_reason=unavailable_reason,
        reason=reason,
    )


def classify_verification_status(
    *,
    exit_code: int | None = None,
    timed_out: bool = False,
    not_found: bool = False,
    execution_error: bool = False,
) -> VerificationStatus:
    signals = [timed_out, not_found, execution_error]
    if any(not isinstance(signal, bool) for signal in signals):
        raise TypeError("status signals must be booleans.")
    if sum(signals) > 1:
        raise ValueError("Only one exceptional verification status may be set.")
    if any(signals) and exit_code is not None:
        raise ValueError(
            "Exceptional verification statuses cannot include an exit code."
        )

    if timed_out:
        return "timed_out"
    if not_found:
        return "not_found"
    if execution_error:
        return "error"
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise ValueError(
            "An integer exit_code is required for passed or failed status."
        )
    return "passed" if exit_code == 0 else "failed"



@dataclass(frozen=True)
class _PythonCommandCandidate:
    command_id: str
    kind: VerificationKind
    module_name: str
    argv: tuple[str, ...]
    source: str
    priority: int


_VERIFICATION_KIND_ORDER: dict[VerificationKind, int] = {
    "test": 0,
    "lint": 1,
    "typecheck": 2,
    "build": 3,
}
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def discover_python_verification_commands(
    workspace: str | Path,
) -> VerificationDiscoveryResult:
    """Discover safe Python verification commands from root-level evidence."""
    workspace_path = Path(workspace).resolve()
    candidates: dict[VerificationKind, _PythonCommandCandidate] = {}
    errors: list[str] = []

    pyproject = _load_root_pyproject(workspace_path, errors)
    if pyproject is not None:
        _add_pyproject_candidates(candidates, pyproject)

    root_test_configs = (
        ("pytest.ini", 110),
        ("tox.ini", 100),
        ("setup.cfg", 90),
    )
    for config_name, priority in root_test_configs:
        if (workspace_path / config_name).is_file():
            _add_python_candidate(
                candidates,
                command_id="python:pytest",
                kind="test",
                module_name="pytest",
                argv=(sys.executable, "-m", "pytest", "-q"),
                source=config_name,
                priority=priority,
            )

    if (workspace_path / "tests").is_dir():
        _add_python_candidate(
            candidates,
            command_id="python:pytest",
            kind="test",
            module_name="pytest",
            argv=(sys.executable, "-m", "pytest", "-q"),
            source="tests/",
            priority=50,
        )

    commands: list[VerificationCommand] = []
    for candidate in sorted(
        candidates.values(),
        key=lambda item: _VERIFICATION_KIND_ORDER[item.kind],
    ):
        available = _python_module_available(candidate.module_name)
        unavailable_reason = None
        if not available:
            unavailable_reason = (
                f"Python module '{candidate.module_name}' is not installed."
            )
        commands.append(
            create_verification_command(
                workspace=workspace_path,
                command_id=candidate.command_id,
                kind=candidate.kind,
                argv=candidate.argv,
                cwd=workspace_path,
                source=candidate.source,
                available=available,
                unavailable_reason=unavailable_reason,
            )
        )

    return VerificationDiscoveryResult(
        workspace=str(workspace_path),
        commands=tuple(commands),
        errors=tuple(errors),
    )


def _load_root_pyproject(
    workspace: Path,
    errors: list[str],
) -> dict[str, Any] | None:
    path = workspace / "pyproject.toml"
    if not path.is_file():
        return None

    try:
        with path.open("rb") as pyproject_file:
            return tomllib.load(pyproject_file)
    except tomllib.TOMLDecodeError as exc:
        errors.append(f"Failed to parse pyproject.toml: {exc}")
    except OSError as exc:
        errors.append(f"Failed to read pyproject.toml: {exc}")
    return None


def _add_pyproject_candidates(
    candidates: dict[VerificationKind, _PythonCommandCandidate],
    pyproject: dict[str, Any],
) -> None:
    tool = _as_mapping(pyproject.get("tool"))
    if "pytest" in tool:
        pytest_config = _as_mapping(tool.get("pytest"))
        if "ini_options" in pytest_config:
            _add_python_candidate(
                candidates,
                command_id="python:pytest",
                kind="test",
                module_name="pytest",
                argv=(sys.executable, "-m", "pytest", "-q"),
                source="pyproject.toml#tool.pytest.ini_options",
                priority=120,
            )

    if "ruff" in tool:
        _add_python_candidate(
            candidates,
            command_id="python:ruff",
            kind="lint",
            module_name="ruff",
            argv=(sys.executable, "-m", "ruff", "check", "."),
            source="pyproject.toml#tool.ruff",
            priority=120,
        )

    if "mypy" in tool:
        _add_python_candidate(
            candidates,
            command_id="python:mypy",
            kind="typecheck",
            module_name="mypy",
            argv=(sys.executable, "-m", "mypy", "."),
            source="pyproject.toml#tool.mypy",
            priority=120,
        )

    project = _as_mapping(pyproject.get("project"))
    optional_dependencies = _as_mapping(project.get("optional-dependencies"))
    dev_dependencies = optional_dependencies.get("dev", [])
    if isinstance(dev_dependencies, list):
        dependency_names = {
            name
            for requirement in dev_dependencies
            if isinstance(requirement, str)
            if (name := _requirement_project_name(requirement)) is not None
        }
        dependency_candidates = (
            (
                "pytest",
                "python:pytest",
                "test",
                (sys.executable, "-m", "pytest", "-q"),
            ),
            (
                "ruff",
                "python:ruff",
                "lint",
                (sys.executable, "-m", "ruff", "check", "."),
            ),
            (
                "mypy",
                "python:mypy",
                "typecheck",
                (sys.executable, "-m", "mypy", "."),
            ),
        )
        for module_name, command_id, kind, argv in dependency_candidates:
            if module_name in dependency_names:
                _add_python_candidate(
                    candidates,
                    command_id=command_id,
                    kind=kind,
                    module_name=module_name,
                    argv=argv,
                    source=(
                        "pyproject.toml#project.optional-dependencies.dev"
                    ),
                    priority=60,
                )

    build_system = _as_mapping(pyproject.get("build-system"))
    build_backend = build_system.get("build-backend")
    build_requires = build_system.get("requires")
    has_build_backend = isinstance(build_backend, str) and bool(
        build_backend.strip()
    )
    has_build_requirements = isinstance(build_requires, list) and any(
        isinstance(requirement, str) and requirement.strip()
        for requirement in build_requires
    )
    if has_build_backend or has_build_requirements:
        _add_python_candidate(
            candidates,
            command_id="python:build",
            kind="build",
            module_name="build",
            argv=(sys.executable, "-m", "build"),
            source="pyproject.toml#build-system",
            priority=120,
        )


def _add_python_candidate(
    candidates: dict[VerificationKind, _PythonCommandCandidate],
    *,
    command_id: str,
    kind: VerificationKind,
    module_name: str,
    argv: tuple[str, ...],
    source: str,
    priority: int,
) -> None:
    candidate = _PythonCommandCandidate(
        command_id=command_id,
        kind=kind,
        module_name=module_name,
        argv=argv,
        source=source,
        priority=priority,
    )
    current = candidates.get(kind)
    if current is None or candidate.priority > current.priority:
        candidates[kind] = candidate


def _requirement_project_name(requirement: str) -> str | None:
    match = _REQUIREMENT_NAME.match(requirement)
    if match is None:
        return None
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _as_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _python_module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (AttributeError, ImportError, ValueError):
        return False


_SUPPORTED_PACKAGE_MANAGERS: frozenset[str] = frozenset(
    {"npm", "pnpm", "yarn"}
)
_PACKAGE_MANAGER_LOCKFILES: tuple[tuple[str, str], ...] = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"),
)


def discover_typescript_verification_commands(
    workspace: str | Path,
) -> VerificationDiscoveryResult:
    """Discover root package scripts without expanding their shell content."""
    workspace_path = Path(workspace).resolve()
    warnings: list[str] = []
    errors: list[str] = []
    package = _load_root_package_json(workspace_path, errors)
    if package is None:
        return VerificationDiscoveryResult(
            workspace=str(workspace_path),
            commands=(),
            errors=tuple(errors),
        )

    if _declares_workspaces(package.get("workspaces")):
        warnings.append(
            "package.json declares workspaces; only root scripts are discovered."
        )

    scripts = _as_mapping(package.get("scripts"))
    package_manager = _select_package_manager(
        workspace_path,
        package,
        warnings,
    )
    available = _executable_available(package_manager)
    unavailable_reason = None
    if not available:
        unavailable_reason = (
            f"Package manager '{package_manager}' is not installed."
        )

    script_candidates: list[tuple[str, VerificationKind]] = [
        ("test", "test"),
        ("lint", "lint"),
    ]
    if _is_non_empty_script(scripts.get("typecheck")):
        script_candidates.append(("typecheck", "typecheck"))
    elif _is_non_empty_script(scripts.get("type-check")):
        script_candidates.append(("type-check", "typecheck"))
    script_candidates.append(("build", "build"))

    commands: list[VerificationCommand] = []
    for script_name, kind in script_candidates:
        if not _is_non_empty_script(scripts.get(script_name)):
            continue
        commands.append(
            create_verification_command(
                workspace=workspace_path,
                command_id=f"node:{kind}",
                kind=kind,
                argv=(package_manager, "run", script_name),
                cwd=workspace_path,
                source=f"package.json#scripts.{script_name}",
                available=available,
                unavailable_reason=unavailable_reason,
            )
        )

    commands.sort(key=lambda command: _VERIFICATION_KIND_ORDER[command.kind])
    return VerificationDiscoveryResult(
        workspace=str(workspace_path),
        commands=tuple(commands),
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def _load_root_package_json(
    workspace: Path,
    errors: list[str],
) -> dict[str, Any] | None:
    path = workspace / "package.json"
    if not path.is_file():
        return None

    try:
        package = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        errors.append(f"Failed to parse package.json: {exc}")
        return None
    except (OSError, UnicodeError) as exc:
        errors.append(f"Failed to read package.json: {exc}")
        return None

    if not isinstance(package, dict):
        errors.append("Failed to parse package.json: root value must be an object.")
        return None
    return package


def _select_package_manager(
    workspace: Path,
    package: dict[str, Any],
    warnings: list[str],
) -> str:
    package_manager_value = package.get("packageManager")
    if isinstance(package_manager_value, str) and package_manager_value.strip():
        manager = package_manager_value.strip().split("@", maxsplit=1)[0].lower()
        if manager in _SUPPORTED_PACKAGE_MANAGERS:
            return manager
        warnings.append(
            f"Unsupported packageManager '{package_manager_value}'; "
            "defaulting to npm."
        )
        return "npm"

    if package_manager_value is not None:
        warnings.append(
            "packageManager must be a non-empty string; checking lockfiles."
        )

    for lockfile, manager in _PACKAGE_MANAGER_LOCKFILES:
        if (workspace / lockfile).is_file():
            return manager

    warnings.append(
        "No packageManager field or lockfile found; defaulting to npm."
    )
    return "npm"


def _declares_workspaces(value: object) -> bool:
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, dict):
        packages = value.get("packages")
        return isinstance(packages, list) and bool(packages)
    return False


def _is_non_empty_script(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _executable_available(executable: str) -> bool:
    try:
        return shutil.which(executable) is not None
    except OSError:
        return False



_TASK_KIND_PATTERNS: dict[VerificationKind, re.Pattern[str]] = {
    "test": re.compile(
        r"\b(?:tests?|testing|pytest|unittest|jest|vitest)\b|"
        r"\u5931\u8d25\u6d4b\u8bd5|\u6d4b\u8bd5\u5931\u8d25|"
        r"\u5355\u5143\u6d4b\u8bd5|\u5355\u6d4b|\u6d4b\u8bd5",
        flags=re.IGNORECASE,
    ),
    "lint": re.compile(
        r"\b(?:lint|linter|ruff|eslint|format|formatted|formatting)\b|"
        r"\u683c\u5f0f\u5316|\u4ee3\u7801\u98ce\u683c|"
        r"\u4ee3\u7801\u89c4\u8303",
        flags=re.IGNORECASE,
    ),
    "typecheck": re.compile(
        r"\b(?:type[\s_-]?(?:check|checking|error|errors)|"
        r"mypy|pyright|tsc|typing)\b|"
        r"\u7c7b\u578b\u9519\u8bef|\u7c7b\u578b\u68c0\u67e5",
        flags=re.IGNORECASE,
    ),
    "build": re.compile(
        r"\b(?:build|building|compile|compilation|package|packaging)\b|"
        r"\u6784\u5efa|\u7f16\u8bd1|\u6253\u5305",
        flags=re.IGNORECASE,
    ),
}
_FAST_VERIFICATION_KINDS: frozenset[str] = frozenset({"test", "lint"})


def rank_verification_commands(
    commands: tuple[VerificationCommand, ...] | list[VerificationCommand],
    task: str,
    failed_command_id: str | None = None,
    after_edit: bool = False,
) -> tuple[VerificationCommand, ...]:
    """Rank commands deterministically and attach an explainable reason."""
    if not isinstance(task, str):
        raise ValueError("task must be a string.")
    if not isinstance(after_edit, bool):
        raise ValueError("after_edit must be a boolean.")
    if failed_command_id is not None:
        _validate_non_empty_string(failed_command_id, "failed_command_id")
        _validate_command_id(failed_command_id)
    if not isinstance(commands, (tuple, list)) or any(
        not isinstance(command, VerificationCommand) for command in commands
    ):
        raise TypeError(
            "commands must be a tuple or list of VerificationCommand values."
        )

    mentioned_kinds = {
        kind
        for kind, pattern in _TASK_KIND_PATTERNS.items()
        if pattern.search(task)
    }

    def sort_key(command: VerificationCommand) -> tuple[int, int, int, int, str]:
        failed_priority = 0 if command.id == failed_command_id else 1
        edit_priority = (
            0
            if not after_edit or command.kind in _FAST_VERIFICATION_KINDS
            else 1
        )
        task_priority = 0 if command.kind in mentioned_kinds else 1
        return (
            failed_priority,
            edit_priority,
            task_priority,
            _VERIFICATION_KIND_ORDER[command.kind],
            command.id,
        )

    ranked: list[VerificationCommand] = []
    for command in sorted(commands, key=sort_key):
        if command.id == failed_command_id:
            reason = "previous attempt failed"
        elif command.kind in mentioned_kinds:
            reason = f"task mentions {command.kind}"
        elif after_edit and command.kind in _FAST_VERIFICATION_KINDS:
            reason = "fast check after edit"
        elif after_edit:
            reason = "broader check after fast checks"
        else:
            reason = "stable default order"
        ranked.append(replace(command, reason=reason))

    return tuple(ranked)

def discover_verification_commands(
    workspace: str | Path,
    *,
    task: str = "",
    failed_command_id: str | None = None,
    after_edit: bool = False,
) -> VerificationDiscoveryResult:
    """Combine root Python and TypeScript verification commands."""
    workspace_path = Path(workspace).resolve()
    discoveries = (
        discover_python_verification_commands(workspace_path),
        discover_typescript_verification_commands(workspace_path),
    )
    commands_by_id: dict[str, VerificationCommand] = {}
    warnings: list[str] = []
    errors: list[str] = []

    for discovery in discoveries:
        warnings.extend(discovery.warnings)
        errors.extend(discovery.errors)
        for command in discovery.commands:
            if command.id in commands_by_id:
                errors.append(
                    f"Duplicate verification command id ignored: {command.id}"
                )
                continue
            commands_by_id[command.id] = command

    commands = rank_verification_commands(
        list(commands_by_id.values()),
        task=task,
        failed_command_id=failed_command_id,
        after_edit=after_edit,
    )
    return VerificationDiscoveryResult(
        workspace=str(workspace_path),
        commands=commands,
        warnings=_unique_strings(warnings),
        errors=_unique_strings(errors),
    )


def run_verification_command(
    workspace: str | Path,
    *,
    command_id: str,
    discovery: VerificationDiscoveryResult | None = None,
    timeout_ms: int = DEFAULT_VERIFICATION_TIMEOUT_MS,
    max_output_bytes: int = DEFAULT_VERIFICATION_MAX_OUTPUT_BYTES,
    max_output_lines: int = DEFAULT_VERIFICATION_MAX_OUTPUT_LINES,
    attempt: int = 1,
    approval_callback: Callable[[VerificationCommand], bool] | None = None,
) -> VerificationResult:
    """Run one command selected by ID from a trusted discovery result."""
    workspace_path = Path(workspace).resolve()
    _validate_execution_limit(
        timeout_ms,
        "timeout_ms",
        MAX_VERIFICATION_TIMEOUT_MS,
    )
    _validate_execution_limit(
        max_output_bytes,
        "max_output_bytes",
        MAX_VERIFICATION_OUTPUT_BYTES,
    )
    _validate_execution_limit(
        max_output_lines,
        "max_output_lines",
        MAX_VERIFICATION_OUTPUT_LINES,
    )
    _validate_execution_limit(attempt, "attempt")
    _validate_command_id(command_id)

    if discovery is None:
        discovery = discover_verification_commands(workspace_path)
    elif Path(discovery.workspace).resolve() != workspace_path:
        raise ValueError(
            "Verification discovery workspace does not match execution workspace."
        )

    command = next(
        (item for item in discovery.commands if item.id == command_id),
        None,
    )
    if command is None:
        raise ValueError(f"Unknown verification command id: {command_id}")

    started = time.monotonic()
    try:
        resolved_cwd = resolve_inside_workspace(workspace_path, command.cwd)
        if resolved_cwd != Path(command.cwd).resolve():
            raise ValueError(
                f"Verification command cwd is not normalized: {command.cwd}"
            )
    except ValueError as exc:
        return _execution_result(
            command,
            status="error",
            output=str(exc),
            exit_code=None,
            started=started,
            attempt=attempt,
            max_output_bytes=max_output_bytes,
            max_output_lines=max_output_lines,
        )

    if not command.available:
        return _execution_result(
            command,
            status="not_found",
            output=command.unavailable_reason or "Verification runtime is unavailable.",
            exit_code=None,
            started=started,
            attempt=attempt,
            max_output_bytes=max_output_bytes,
            max_output_lines=max_output_lines,
        )

    if approval_callback is not None:
        try:
            approved = approval_callback(command)
        except Exception as exc:
            return _execution_result(
                command,
                status="error",
                output=f"Verification approval failed: {exc}",
                exit_code=None,
                started=started,
                attempt=attempt,
                max_output_bytes=max_output_bytes,
                max_output_lines=max_output_lines,
            )
        if not isinstance(approved, bool):
            return _execution_result(
                command,
                status="error",
                output="Verification approval callback must return a boolean.",
                exit_code=None,
                started=started,
                attempt=attempt,
                max_output_bytes=max_output_bytes,
                max_output_lines=max_output_lines,
            )
        if not approved:
            return _execution_result(
                command,
                status="error",
                output="User declined verification command execution.",
                exit_code=None,
                started=started,
                attempt=attempt,
                max_output_bytes=max_output_bytes,
                max_output_lines=max_output_lines,
            )

    try:
        completed = subprocess.run(
            command.argv,
            cwd=command.cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_ms / 1000,
        )
    except subprocess.TimeoutExpired as exc:
        return _execution_result(
            command,
            status="timed_out",
            stdout=_coerce_process_text(exc.stdout),
            stderr=_coerce_process_text(exc.stderr),
            exit_code=None,
            started=started,
            attempt=attempt,
            max_output_bytes=max_output_bytes,
            max_output_lines=max_output_lines,
        )
    except FileNotFoundError as exc:
        return _execution_result(
            command,
            status="not_found",
            output=str(exc) or f"Runtime not found: {command.argv[0]}",
            exit_code=None,
            started=started,
            attempt=attempt,
            max_output_bytes=max_output_bytes,
            max_output_lines=max_output_lines,
        )
    except OSError as exc:
        return _execution_result(
            command,
            status="error",
            output=str(exc) or "Failed to start verification command.",
            exit_code=None,
            started=started,
            attempt=attempt,
            max_output_bytes=max_output_bytes,
            max_output_lines=max_output_lines,
        )

    status = classify_verification_status(exit_code=completed.returncode)
    return _execution_result(
        command,
        status=status,
        stdout=_coerce_process_text(completed.stdout),
        stderr=_coerce_process_text(completed.stderr),
        exit_code=completed.returncode,
        started=started,
        attempt=attempt,
        max_output_bytes=max_output_bytes,
        max_output_lines=max_output_lines,
    )


def summarize_command_output(
    stdout: str,
    stderr: str,
    *,
    max_bytes: int,
    max_lines: int,
) -> OutputSummary:
    """Return a bounded summary that prioritizes actionable error context."""
    if not isinstance(stdout, str):
        raise TypeError("stdout must be a string.")
    if not isinstance(stderr, str):
        raise TypeError("stderr must be a string.")
    _validate_execution_limit(
        max_bytes,
        "max_bytes",
        MAX_VERIFICATION_OUTPUT_BYTES,
    )
    _validate_execution_limit(
        max_lines,
        "max_lines",
        MAX_VERIFICATION_OUTPUT_LINES,
    )

    stdout_lines = _tag_output_lines("stdout", stdout)
    stderr_lines = _tag_output_lines("stderr", stderr)
    lines = stdout_lines + stderr_lines
    if not lines:
        return OutputSummary("", False, 0, 0)

    full_output = "\n".join(lines)
    full_bytes = full_output.encode("utf-8")
    if len(lines) <= max_lines and len(full_bytes) <= max_bytes:
        return OutputSummary(full_output, False, 0, 0)

    stderr_start = len(stdout_lines)
    error_indices = [
        index for index, line in enumerate(lines) if _ACTIONABLE_OUTPUT_RE.search(line)
    ]
    priority = _summary_line_priority(
        line_count=len(lines),
        error_indices=error_indices,
        stderr_start=stderr_start,
    )

    selected: dict[int, str] = {}
    used_bytes = 0
    for index in priority:
        if len(selected) >= max_lines:
            break
        line = lines[index]
        line_bytes = line.encode("utf-8")
        separator_bytes = 1 if selected else 0
        if used_bytes + separator_bytes + len(line_bytes) <= max_bytes:
            selected[index] = line
            used_bytes += separator_bytes + len(line_bytes)
            continue
        if not selected:
            selected[index] = _truncate_utf8(line, max_bytes)
            used_bytes = len(selected[index].encode("utf-8"))
            break

    if not selected:
        return OutputSummary(
            output="",
            truncated=True,
            omitted_lines=len(lines),
            omitted_bytes=len(full_bytes),
        )

    output = "\n".join(selected[index] for index in sorted(selected))
    output_bytes = output.encode("utf-8")
    omitted_lines = max(0, len(lines) - len(selected))
    omitted_bytes = max(0, len(full_bytes) - len(output_bytes))
    return OutputSummary(
        output=output,
        truncated=bool(omitted_lines or omitted_bytes),
        omitted_lines=omitted_lines,
        omitted_bytes=omitted_bytes,
    )


def _tag_output_lines(label: str, content: str) -> list[str]:
    normalized = _ANSI_ESCAPE_RE.sub("", content)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.rstrip("\n")
    if not normalized:
        return []
    return [f"{label}: {line}" for line in normalized.split("\n")]


def _summary_line_priority(
    *,
    line_count: int,
    error_indices: list[int],
    stderr_start: int,
) -> list[int]:
    priority: list[int] = []
    seen: set[int] = set()

    def add(index: int) -> None:
        if 0 <= index < line_count and index not in seen:
            priority.append(index)
            seen.add(index)

    if error_indices:
        add(error_indices[0])

    if stderr_start < line_count:
        add(line_count - 1)
        if line_count - 2 >= stderr_start:
            add(line_count - 2)

    if error_indices:
        add(error_indices[-1])

    for index in error_indices:
        add(index)

    for index in error_indices:
        for distance in range(1, OUTPUT_CONTEXT_LINES + 1):
            add(index - distance)
            add(index + distance)

    boundary_count = min(3, line_count)
    for offset in range(boundary_count):
        add(offset)
        add(line_count - 1 - offset)

    for index in range(line_count):
        add(index)
    return priority


def _truncate_utf8(value: str, max_bytes: int) -> str:
    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def _execution_result(
    command: VerificationCommand,
    *,
    status: VerificationStatus,
    output: str | None = None,
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None,
    started: float,
    attempt: int,
    max_output_bytes: int,
    max_output_lines: int,
) -> VerificationResult:
    if output is None:
        effective_max_bytes = max_output_bytes
        effective_max_lines = max_output_lines
        if status == "passed":
            effective_max_bytes = min(
                effective_max_bytes,
                PASSED_VERIFICATION_MAX_OUTPUT_BYTES,
            )
            effective_max_lines = min(
                effective_max_lines,
                PASSED_VERIFICATION_MAX_OUTPUT_LINES,
            )
        summary = summarize_command_output(
            stdout,
            stderr,
            max_bytes=effective_max_bytes,
            max_lines=effective_max_lines,
        )
    else:
        limited_output, truncated, omitted_lines, omitted_bytes = (
            _limit_verification_output(
                output,
                max_output_bytes=max_output_bytes,
                max_output_lines=max_output_lines,
            )
        )
        summary = OutputSummary(
            output=limited_output,
            truncated=truncated,
            omitted_lines=omitted_lines,
            omitted_bytes=omitted_bytes,
        )

    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    return VerificationResult(
        command_id=command.id,
        kind=command.kind,
        status=status,
        argv=command.argv,
        cwd=command.cwd,
        exit_code=exit_code,
        duration_ms=duration_ms,
        output=summary.output,
        truncated=summary.truncated,
        omitted_lines=summary.omitted_lines,
        omitted_bytes=summary.omitted_bytes,
        attempt=attempt,
    )


def _coerce_process_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


def _limit_verification_output(
    output: str,
    *,
    max_output_bytes: int,
    max_output_lines: int,
) -> tuple[str, bool, int, int]:
    original_bytes = output.encode("utf-8")
    original_lines = output.splitlines()
    limited = "\n".join(original_lines[:max_output_lines])
    limited_bytes = limited.encode("utf-8")
    if len(limited_bytes) > max_output_bytes:
        limited = limited_bytes[:max_output_bytes].decode(
            "utf-8",
            errors="ignore",
        )

    final_bytes = limited.encode("utf-8")
    final_lines = limited.splitlines()
    omitted_lines = max(0, len(original_lines) - len(final_lines))
    omitted_bytes = max(0, len(original_bytes) - len(final_bytes))
    truncated = bool(omitted_lines or omitted_bytes)
    return limited, truncated, omitted_lines, omitted_bytes


def _unique_strings(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _validate_execution_limit(
    value: int,
    label: str,
    maximum: int | None = None,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} must be at most {maximum}.")

def _validate_command_id(command_id: str) -> None:
    _validate_non_empty_string(command_id, "command id")
    if ":" not in command_id:
        raise ValueError(
            "command id must include a stable namespace, for example python:pytest."
        )
    if any(character.isspace() for character in command_id):
        raise ValueError("command id must not contain whitespace.")


def _validate_kind(kind: str) -> None:
    if kind not in VERIFICATION_KINDS:
        raise ValueError(f"Unsupported verification kind: {kind}")


def _validate_argv(argv: tuple[str, ...]) -> None:
    if not isinstance(argv, tuple):
        raise TypeError("argv must be a tuple.")
    if not argv or any(not isinstance(argument, str) or not argument for argument in argv):
        raise ValueError("argv must contain one or more non-empty strings.")


def _validate_absolute_path(value: str, label: str) -> Path:
    _validate_non_empty_string(value, label)
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path.")
    return path.resolve()


def _validate_non_empty_string(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string.")


def _validate_non_negative_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer.")
    if value < 0:
        raise ValueError(f"{label} must be zero or greater.")


def _validate_string_tuple(value: tuple[str, ...], label: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{label} must be a tuple.")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{label} must contain only non-empty strings.")

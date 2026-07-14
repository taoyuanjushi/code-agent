"""M3 acceptance metrics and scenario contracts.

This module intentionally does not import production verification code. Step 1 fixes
what later M3 implementation steps must satisfy without changing ``src/coding_agent/``.
"""

import json
import tomllib
from pathlib import Path

M3_MAX_OUTPUT_BYTES = 32 * 1024
M3_MAX_OUTPUT_LINES = 200
M3_MAX_REPAIR_ATTEMPTS = 3
M3_EXECUTION_USES_SHELL = False

M3_REQUIRED_COMMAND_FIELDS = frozenset(
    {"id", "kind", "argv", "cwd", "source", "available"}
)
M3_REQUIRED_RESULT_FIELDS = frozenset(
    {
        "command_id",
        "kind",
        "status",
        "argv",
        "cwd",
        "exit_code",
        "duration_ms",
        "output",
        "truncated",
        "omitted_lines",
        "omitted_bytes",
        "attempt",
    }
)
M3_REQUIRED_FINAL_REPORT_FIELDS = frozenset(
    {"answer", "verifications", "final_status"}
)
M3_VERIFICATION_STATUSES = frozenset(
    {"passed", "failed", "timed_out", "not_found", "error"}
)
M3_EXPECTED_PYTHON_COMMAND = {
    "id": "python:pytest",
    "kind": "test",
    "argv_suffix": ("-m", "pytest", "-q"),
    "source": "pyproject.toml",
}
M3_EXPECTED_TYPESCRIPT_COMMANDS = {
    "test": ("node:test", ("npm", "run", "test")),
    "lint": ("node:lint", ("npm", "run", "lint")),
    "typecheck": ("node:typecheck", ("npm", "run", "typecheck")),
    "build": ("node:build", ("npm", "run", "build")),
}
M3_FORBIDDEN_SCRIPT_NAMES = frozenset({"install", "publish", "deploy"})
M3_REQUIRED_FAILURE_EVIDENCE = frozenset(
    {"exit_code", "command_id", "truncated", "omitted_lines", "output"}
)
M3_ACTIONABLE_ERROR_TERMS = frozenset(
    {"error", "failed", "failure", "traceback", "assert", "exception"}
)
M3_REPAIR_TOOL_SEQUENCE = (
    "discover_verification_commands",
    "search_text",
    "read_many_files",
    "apply_patch",
    "run_verification",
    "search_text",
    "read_many_files",
    "apply_patch",
    "run_verification",
)

FIXTURES = Path(__file__).parent / "fixtures"
PYTHON_FIXTURE = FIXTURES / "m3_python_project"
TYPESCRIPT_FIXTURE = FIXTURES / "m3_typescript_project"


def test_m3_numeric_acceptance_limits_are_fixed() -> None:
    assert M3_MAX_OUTPUT_BYTES == 32_768
    assert M3_MAX_OUTPUT_LINES == 200
    assert M3_MAX_REPAIR_ATTEMPTS == 3


def test_python_fixture_defines_pytest_as_a_test_command_source() -> None:
    pyproject = tomllib.loads(
        (PYTHON_FIXTURE / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert "pytest" in pyproject["tool"]
    assert (PYTHON_FIXTURE / "tests" / "test_refund_service.py.txt").is_file()
    assert M3_EXPECTED_PYTHON_COMMAND == {
        "id": "python:pytest",
        "kind": "test",
        "argv_suffix": ("-m", "pytest", "-q"),
        "source": "pyproject.toml",
    }
    assert M3_REQUIRED_COMMAND_FIELDS == {
        "id",
        "kind",
        "argv",
        "cwd",
        "source",
        "available",
    }


def test_typescript_fixture_fixes_allowed_and_forbidden_script_contracts() -> None:
    package = json.loads(
        (TYPESCRIPT_FIXTURE / "package.json").read_text(encoding="utf-8")
    )
    scripts = package["scripts"]

    assert package["packageManager"].startswith("npm@")
    assert M3_EXPECTED_TYPESCRIPT_COMMANDS == {
        "test": ("node:test", ("npm", "run", "test")),
        "lint": ("node:lint", ("npm", "run", "lint")),
        "typecheck": ("node:typecheck", ("npm", "run", "typecheck")),
        "build": ("node:build", ("npm", "run", "build")),
    }
    assert M3_EXPECTED_TYPESCRIPT_COMMANDS.keys() <= scripts.keys()
    assert M3_FORBIDDEN_SCRIPT_NAMES <= scripts.keys()
    assert not M3_FORBIDDEN_SCRIPT_NAMES & M3_EXPECTED_TYPESCRIPT_COMMANDS.keys()


def test_execution_and_result_contracts_are_structured() -> None:
    assert M3_EXECUTION_USES_SHELL is False
    assert all(
        isinstance(argv, tuple)
        for _command_id, argv in M3_EXPECTED_TYPESCRIPT_COMMANDS.values()
    )
    assert M3_VERIFICATION_STATUSES == {
        "passed",
        "failed",
        "timed_out",
        "not_found",
        "error",
    }
    assert M3_REQUIRED_FAILURE_EVIDENCE <= M3_REQUIRED_RESULT_FIELDS
    assert M3_ACTIONABLE_ERROR_TERMS == {
        "error",
        "failed",
        "failure",
        "traceback",
        "assert",
        "exception",
    }


def test_repair_loop_and_final_report_contracts_are_fixed() -> None:
    assert M3_REPAIR_TOOL_SEQUENCE == (
        "discover_verification_commands",
        "search_text",
        "read_many_files",
        "apply_patch",
        "run_verification",
        "search_text",
        "read_many_files",
        "apply_patch",
        "run_verification",
    )
    assert M3_REPAIR_TOOL_SEQUENCE.count("run_verification") == 2
    assert M3_REPAIR_TOOL_SEQUENCE.count("apply_patch") == 2
    assert M3_REQUIRED_FINAL_REPORT_FIELDS == {
        "answer",
        "verifications",
        "final_status",
    }

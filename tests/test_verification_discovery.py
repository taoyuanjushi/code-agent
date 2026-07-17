"""Tests for root-level Python verification command discovery."""

import sys
from pathlib import Path

import pytest

import coding_agent.verification as verification
from coding_agent.verification import discover_python_verification_commands

FIXTURE = Path(__file__).parent / "fixtures" / "m3_python_project"


def _set_available_modules(
    monkeypatch: pytest.MonkeyPatch,
    *module_names: str,
) -> None:
    available = frozenset(module_names)
    monkeypatch.setattr(
        verification,
        "_python_module_available",
        lambda module_name: module_name in available,
    )


def _write_pyproject(workspace: Path, content: str) -> None:
    (workspace / "pyproject.toml").write_text(content, encoding="utf-8")


def _create_symlink_or_skip(
    link: Path,
    target: Path,
    *,
    is_directory: bool = False,
) -> None:
    try:
        link.symlink_to(target, target_is_directory=is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")


def test_discovers_fixture_commands_in_stable_kind_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_available_modules(monkeypatch, "pytest", "ruff", "mypy", "build")

    result = discover_python_verification_commands(FIXTURE)

    assert [command.id for command in result.commands] == [
        "python:pytest",
        "python:ruff",
        "python:mypy",
        "python:build",
    ]
    assert [command.kind for command in result.commands] == [
        "test",
        "lint",
        "typecheck",
        "build",
    ]
    assert [command.argv for command in result.commands] == [
        (sys.executable, "-m", "pytest", "-q"),
        (sys.executable, "-m", "ruff", "check", "."),
        (sys.executable, "-m", "mypy", "."),
        (sys.executable, "-m", "build"),
    ]
    assert [command.source for command in result.commands] == [
        "pyproject.toml#tool.pytest.ini_options",
        "pyproject.toml#tool.ruff",
        "pyproject.toml#tool.mypy",
        "pyproject.toml#build-system",
    ]
    assert all(command.available for command in result.commands)
    assert result.workspace == str(FIXTURE.resolve())
    assert result.errors == ()


def test_discovery_ignores_external_python_metadata_and_test_directory_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-python"
    outside.mkdir()
    (outside / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    (outside / "tests").mkdir()
    _create_symlink_or_skip(
        tmp_path / "pyproject.toml",
        outside / "pyproject.toml",
    )
    _create_symlink_or_skip(
        tmp_path / "tests",
        outside / "tests",
        is_directory=True,
    )
    _set_available_modules(monkeypatch, "pytest")

    result = discover_python_verification_commands(tmp_path)

    assert result.commands == ()
    assert result.errors == ()


def test_tests_directory_alone_discovers_unavailable_pytest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "tests").mkdir()
    _set_available_modules(monkeypatch)

    result = discover_python_verification_commands(tmp_path)

    assert len(result.commands) == 1
    command = result.commands[0]
    assert command.id == "python:pytest"
    assert command.source == "tests/"
    assert command.available is False
    assert command.unavailable_reason == "Python module 'pytest' is not installed."


@pytest.mark.parametrize("config_name", ["pytest.ini", "tox.ini", "setup.cfg"])
def test_root_test_config_is_pytest_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_name: str,
) -> None:
    (tmp_path / config_name).write_text("[pytest]\n", encoding="utf-8")
    _set_available_modules(monkeypatch, "pytest")

    result = discover_python_verification_commands(tmp_path)

    assert [(command.id, command.source) for command in result.commands] == [
        ("python:pytest", config_name)
    ]


def test_dev_dependencies_match_exact_normalized_project_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pyproject(
        tmp_path,
        """
[project]
name = "requirements-only"
version = "0.0.0"

[project.optional-dependencies]
dev = [
  "pytest-cov>=5",
  "ruff>=0.5",
  "mypy[dmypy]>=1.10",
]
""",
    )
    _set_available_modules(monkeypatch, "pytest", "ruff", "mypy")

    result = discover_python_verification_commands(tmp_path)

    assert [command.id for command in result.commands] == [
        "python:ruff",
        "python:mypy",
    ]
    assert all(
        command.source == "pyproject.toml#project.optional-dependencies.dev"
        for command in result.commands
    )


def test_strongest_evidence_wins_without_duplicate_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    _write_pyproject(
        tmp_path,
        """
[project.optional-dependencies]
dev = ["pytest>=8"]

[tool.pytest.ini_options]
testpaths = ["tests"]
""",
    )
    _set_available_modules(monkeypatch, "pytest")

    result = discover_python_verification_commands(tmp_path)

    assert len(result.commands) == 1
    assert result.commands[0].kind == "test"
    assert result.commands[0].source == "pyproject.toml#tool.pytest.ini_options"


def test_invalid_pyproject_returns_error_without_stopping_other_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pyproject(tmp_path, "[tool.pytest.ini_options\n")
    (tmp_path / "tests").mkdir()
    _set_available_modules(monkeypatch, "pytest")

    result = discover_python_verification_commands(tmp_path)

    assert [command.id for command in result.commands] == ["python:pytest"]
    assert result.commands[0].source == "tests/"
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Failed to parse pyproject.toml:")


def test_no_root_evidence_returns_empty_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_available_modules(monkeypatch, "pytest", "ruff", "mypy", "build")

    result = discover_python_verification_commands(tmp_path)

    assert result.commands == ()
    assert result.errors == ()


def test_nested_configuration_and_readme_commands_are_not_discovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = tmp_path / "packages" / "service"
    nested.mkdir(parents=True)
    _write_pyproject(
        nested,
        """
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[tool.ruff]
""",
    )
    (tmp_path / "README.md").write_text(
        "Run `python -m pytest` and `python -m mypy .`.\n",
        encoding="utf-8",
    )
    _set_available_modules(monkeypatch, "pytest", "ruff", "mypy", "build")

    result = discover_python_verification_commands(tmp_path)

    assert result.commands == ()


def test_build_system_requirements_are_build_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pyproject(
        tmp_path,
        """
[build-system]
requires = ["setuptools"]
""",
    )
    _set_available_modules(monkeypatch, "build")

    result = discover_python_verification_commands(tmp_path)

    assert [(command.id, command.source) for command in result.commands] == [
        ("python:build", "pyproject.toml#build-system")
    ]


def test_configured_python_commands_are_all_reported_when_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pyproject(
        tmp_path,
        """
[tool.pytest.ini_options]
[tool.ruff]
[tool.mypy]

[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"
""",
    )
    _set_available_modules(monkeypatch)

    result = discover_python_verification_commands(tmp_path)

    assert [command.id for command in result.commands] == [
        "python:pytest",
        "python:ruff",
        "python:mypy",
        "python:build",
    ]
    assert all(command.available is False for command in result.commands)
    assert [command.unavailable_reason for command in result.commands] == [
        "Python module 'pytest' is not installed.",
        "Python module 'ruff' is not installed.",
        "Python module 'mypy' is not installed.",
        "Python module 'build' is not installed.",
    ]


def test_python_discovery_is_identical_across_repeated_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_available_modules(monkeypatch, "pytest", "ruff", "mypy", "build")

    first = discover_python_verification_commands(FIXTURE)
    second = discover_python_verification_commands(FIXTURE)

    assert first == second


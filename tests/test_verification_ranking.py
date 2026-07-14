"""Tests for deterministic verification command relevance ranking."""

import sys
from pathlib import Path

import pytest

import coding_agent.verification as verification
from coding_agent.verification import (
    VerificationCommand,
    create_verification_command,
    discover_verification_commands,
    rank_verification_commands,
)


def _commands(workspace: Path) -> tuple[VerificationCommand, ...]:
    definitions = (
        ("python:build", "build"),
        ("python:mypy", "typecheck"),
        ("python:ruff", "lint"),
        ("python:pytest", "test"),
        ("node:test", "test"),
    )
    return tuple(
        create_verification_command(
            workspace=workspace,
            command_id=command_id,
            kind=kind,
            argv=(sys.executable, "-c", "pass"),
            source="fixture",
            available=True,
        )
        for command_id, kind in definitions
    )


def test_default_ranking_uses_kind_then_command_id_stably(tmp_path: Path) -> None:
    commands = _commands(tmp_path)

    first = rank_verification_commands(commands, task="")
    second = rank_verification_commands(tuple(reversed(commands)), task="")

    expected = [
        "node:test",
        "python:pytest",
        "python:ruff",
        "python:mypy",
        "python:build",
    ]
    assert [command.id for command in first] == expected
    assert [command.id for command in second] == expected
    assert {command.reason for command in first} == {"stable default order"}


@pytest.mark.parametrize(
    ("task", "expected_id", "expected_reason"),
    [
        ("Fix the failing test for refunds", "node:test", "task mentions test"),
        ("运行退款测试", "node:test", "task mentions test"),
        ("Resolve the lint warning", "python:ruff", "task mentions lint"),
        ("Format the project", "python:ruff", "task mentions lint"),
        (
            "Fix this type error",
            "python:mypy",
            "task mentions typecheck",
        ),
        ("修复类型错误", "python:mypy", "task mentions typecheck"),
        ("Compile the package", "python:build", "task mentions build"),
    ],
)
def test_task_keywords_promote_relevant_kind(
    tmp_path: Path,
    task: str,
    expected_id: str,
    expected_reason: str,
) -> None:
    ranked = rank_verification_commands(_commands(tmp_path), task=task)

    assert ranked[0].id == expected_id
    assert ranked[0].reason == expected_reason


def test_previous_failed_command_overrides_task_relevance(tmp_path: Path) -> None:
    ranked = rank_verification_commands(
        _commands(tmp_path),
        task="Run lint checks",
        failed_command_id="python:build",
    )

    assert ranked[0].id == "python:build"
    assert ranked[0].reason == "previous attempt failed"
    assert ranked[1].id == "python:ruff"
    assert ranked[1].reason == "task mentions lint"


def test_after_edit_runs_fast_checks_before_typecheck_and_build(
    tmp_path: Path,
) -> None:
    ranked = rank_verification_commands(
        _commands(tmp_path),
        task="Build the release package",
        after_edit=True,
    )

    assert [command.kind for command in ranked] == [
        "test",
        "test",
        "lint",
        "build",
        "typecheck",
    ]
    assert ranked[0].reason == "fast check after edit"
    assert ranked[2].reason == "fast check after edit"
    assert ranked[3].reason == "task mentions build"


def test_failed_command_remains_first_after_edit(tmp_path: Path) -> None:
    ranked = rank_verification_commands(
        _commands(tmp_path),
        task="",
        failed_command_id="python:mypy",
        after_edit=True,
    )

    assert ranked[0].id == "python:mypy"
    assert ranked[0].reason == "previous attempt failed"


def test_unified_discovery_applies_task_ranking_and_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(verification, "_python_module_available", lambda _name: True)

    result = discover_verification_commands(
        tmp_path,
        task="Fix lint failures",
    )

    assert [command.id for command in result.commands] == [
        "python:ruff",
        "python:pytest",
    ]
    assert result.commands[0].reason == "task mentions lint"


def test_ranking_does_not_mutate_original_commands(tmp_path: Path) -> None:
    commands = _commands(tmp_path)

    ranked = rank_verification_commands(commands, task="lint")

    assert all(command.reason is None for command in commands)
    assert ranked[0].reason == "task mentions lint"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"task": 123}, "task must be a string"),
        ({"task": "", "after_edit": "yes"}, "after_edit must be a boolean"),
        (
            {"task": "", "failed_command_id": ""},
            "failed_command_id must be a non-empty string",
        ),
    ],
)
def test_ranking_validates_inputs(
    tmp_path: Path,
    arguments: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        rank_verification_commands(
            _commands(tmp_path),
            **arguments,
        )

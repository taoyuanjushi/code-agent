"""Tests for root-level TypeScript verification command discovery."""

import json
import shutil
from pathlib import Path

import pytest

import coding_agent.verification as verification
from coding_agent.verification import (
    VerificationDiscoveryResult,
    create_verification_command,
    discover_typescript_verification_commands,
    run_verification_command,
)

FIXTURE = Path(__file__).parent / "fixtures" / "m3_typescript_project"


def _set_available_executables(
    monkeypatch: pytest.MonkeyPatch,
    *executable_names: str,
) -> None:
    available = frozenset(executable_names)
    monkeypatch.setattr(
        verification,
        "_executable_available",
        lambda executable: executable in available,
    )


def _write_package(workspace: Path, package: object) -> None:
    (workspace / "package.json").write_text(
        json.dumps(package),
        encoding="utf-8",
    )


def test_discovers_fixture_scripts_in_stable_kind_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_available_executables(monkeypatch, "npm")

    result = discover_typescript_verification_commands(FIXTURE)

    assert [command.id for command in result.commands] == [
        "node:test",
        "node:lint",
        "node:typecheck",
        "node:build",
    ]
    assert [command.kind for command in result.commands] == [
        "test",
        "lint",
        "typecheck",
        "build",
    ]
    assert [command.argv for command in result.commands] == [
        ("npm", "run", "test"),
        ("npm", "run", "lint"),
        ("npm", "run", "typecheck"),
        ("npm", "run", "build"),
    ]
    assert [command.source for command in result.commands] == [
        "package.json#scripts.test",
        "package.json#scripts.lint",
        "package.json#scripts.typecheck",
        "package.json#scripts.build",
    ]
    assert all(command.available for command in result.commands)
    assert result.workspace == str(FIXTURE.resolve())
    assert result.warnings == ()
    assert result.errors == ()


def test_forbidden_lifecycle_scripts_are_not_discovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_available_executables(monkeypatch, "npm")

    result = discover_typescript_verification_commands(FIXTURE)

    assert {command.id for command in result.commands} == {
        "node:test",
        "node:lint",
        "node:typecheck",
        "node:build",
    }
    assert all(
        forbidden not in command.source
        for command in result.commands
        for forbidden in ("install", "publish", "deploy")
    )


def test_package_manager_field_overrides_lockfiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(
        tmp_path,
        {
            "packageManager": "pnpm@9.12.0",
            "scripts": {"test": "vitest run"},
        },
    )
    (tmp_path / "yarn.lock").touch()
    (tmp_path / "package-lock.json").touch()
    _set_available_executables(monkeypatch, "pnpm")

    result = discover_typescript_verification_commands(tmp_path)

    assert result.commands[0].argv == ("pnpm", "run", "test")
    assert result.commands[0].available is True
    assert result.warnings == ()


@pytest.mark.parametrize(
    ("lockfile", "expected_manager"),
    [
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("package-lock.json", "npm"),
    ],
)
def test_lockfiles_select_package_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lockfile: str,
    expected_manager: str,
) -> None:
    _write_package(tmp_path, {"scripts": {"lint": "eslint ."}})
    (tmp_path / lockfile).touch()
    _set_available_executables(monkeypatch, expected_manager)

    result = discover_typescript_verification_commands(tmp_path)

    assert result.commands[0].argv == (
        expected_manager,
        "run",
        "lint",
    )
    assert result.warnings == ()


def test_lockfile_priority_is_pnpm_then_yarn_then_npm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(tmp_path, {"scripts": {"build": "tsc"}})
    for lockfile in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"):
        (tmp_path / lockfile).touch()
    _set_available_executables(monkeypatch, "pnpm")

    result = discover_typescript_verification_commands(tmp_path)

    assert result.commands[0].argv == ("pnpm", "run", "build")


def test_missing_manager_metadata_falls_back_to_npm_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(tmp_path, {"scripts": {"test": "node --test"}})
    _set_available_executables(monkeypatch, "npm")

    result = discover_typescript_verification_commands(tmp_path)

    assert result.commands[0].argv == ("npm", "run", "test")
    assert result.warnings == (
        "No packageManager field or lockfile found; defaulting to npm.",
    )


def test_type_check_alias_maps_to_typecheck_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(
        tmp_path,
        {
            "packageManager": "yarn@4.5.0",
            "scripts": {"type-check": "tsc --noEmit"},
        },
    )
    _set_available_executables(monkeypatch, "yarn")

    result = discover_typescript_verification_commands(tmp_path)

    command = result.commands[0]
    assert command.id == "node:typecheck"
    assert command.kind == "typecheck"
    assert command.argv == ("yarn", "run", "type-check")
    assert command.source == "package.json#scripts.type-check"


def test_typecheck_name_wins_when_both_aliases_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(
        tmp_path,
        {
            "packageManager": "npm@10",
            "scripts": {
                "typecheck": "tsc --noEmit",
                "type-check": "tsc --noEmit --pretty false",
            },
        },
    )
    _set_available_executables(monkeypatch, "npm")

    result = discover_typescript_verification_commands(tmp_path)

    assert len(result.commands) == 1
    assert result.commands[0].argv == ("npm", "run", "typecheck")


def test_unavailable_package_manager_marks_every_command_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(
        tmp_path,
        {
            "packageManager": "pnpm@9",
            "scripts": {"test": "vitest", "build": "tsc"},
        },
    )
    _set_available_executables(monkeypatch)

    result = discover_typescript_verification_commands(tmp_path)

    assert len(result.commands) == 2
    assert all(not command.available for command in result.commands)
    assert {
        command.unavailable_reason for command in result.commands
    } == {"Package manager 'pnpm' is not installed."}


def test_invalid_package_json_returns_error_instead_of_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "package.json").write_text("{ invalid", encoding="utf-8")
    _set_available_executables(monkeypatch, "npm")

    result = discover_typescript_verification_commands(tmp_path)

    assert result.commands == ()
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Failed to parse package.json:")


def test_declared_workspaces_warn_but_keep_root_scripts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(
        tmp_path,
        {
            "packageManager": "npm@10",
            "workspaces": ["packages/*"],
            "scripts": {"test": "node --test"},
        },
    )
    _set_available_executables(monkeypatch, "npm")

    result = discover_typescript_verification_commands(tmp_path)

    assert [command.id for command in result.commands] == ["node:test"]
    assert result.warnings == (
        "package.json declares workspaces; only root scripts are discovered.",
    )


def test_nested_manifest_readme_and_non_string_scripts_are_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(
        tmp_path,
        {
            "packageManager": "npm@10",
            "scripts": {"test": "", "lint": ["eslint", "."]},
        },
    )
    nested = tmp_path / "packages" / "service"
    nested.mkdir(parents=True)
    _write_package(
        nested,
        {
            "packageManager": "npm@10",
            "scripts": {"build": "tsc"},
        },
    )
    (tmp_path / "README.md").write_text(
        "Run `npm run typecheck`.\n",
        encoding="utf-8",
    )
    _set_available_executables(monkeypatch, "npm")

    result = discover_typescript_verification_commands(tmp_path)

    assert result.commands == ()


def test_missing_root_package_returns_empty_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_available_executables(monkeypatch, "npm")

    result = discover_typescript_verification_commands(tmp_path)

    assert result.commands == ()
    assert result.warnings == ()
    assert result.errors == ()


def test_typescript_discovery_is_identical_across_repeated_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_available_executables(monkeypatch, "npm")

    first = discover_typescript_verification_commands(FIXTURE)
    second = discover_typescript_verification_commands(FIXTURE)

    assert first == second


_LOCAL_NODE_PATH = shutil.which("node")


@pytest.mark.local_node
@pytest.mark.skipif(_LOCAL_NODE_PATH is None, reason="Node.js is not installed")
def test_local_node_controlled_runner_smoke(tmp_path: Path) -> None:
    command = create_verification_command(
        workspace=tmp_path,
        command_id="node:test",
        kind="test",
        argv=(_LOCAL_NODE_PATH or "node", "-e", "console.log('node-smoke-ok')"),
        source="optional local Node.js smoke test",
        available=True,
    )
    discovery = VerificationDiscoveryResult(
        workspace=str(tmp_path.resolve()),
        commands=(command,),
    )

    result = run_verification_command(
        tmp_path,
        command_id="node:test",
        discovery=discovery,
        timeout_ms=10_000,
    )

    assert result.status == "passed"
    assert result.exit_code == 0
    assert "node-smoke-ok" in result.output


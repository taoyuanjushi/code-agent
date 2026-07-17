import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coding_agent.search as search_module
from coding_agent.context import collect_workspace_snapshot
from coding_agent.instructions import discover_agent_instructions
from coding_agent.reader import read_many_files
from coding_agent.security.path_policy import (
    SENSITIVE_PATH_ALLOWED_REASON,
    SENSITIVE_PATH_DENIAL_REASON,
    SENSITIVE_PATH_EXCEPTION_REASON,
    load_sensitive_path_policy,
)
from coding_agent.sessions import SessionStore
from coding_agent.sessions.codec import artifact_ref_to_dict
from coding_agent.sessions.replay import build_session_replay_payload
from coding_agent.tools import execute_tool
from coding_agent.types import AgentConfig


def _config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="workspace-write",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=20,
        context_max_bytes_per_file=4_096,
    )


def _snapshot(workspace: Path, task: str = "inspect workspace"):
    return collect_workspace_snapshot(
        str(workspace),
        task,
        max_inventory_files=100,
        max_sample_files=6,
        max_bytes_per_file=4_096,
        max_total_sample_bytes=64 * 1_024,
    )


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.production",
        "config/.npmrc",
        "config/.pypirc",
        "home/.netrc",
        "credentials",
        "config/credentials.json",
        ".ssh/config",
        "users/alice/.aws/credentials",
        "home/.config/gcloud/application_default_credentials.json",
        "keys/id_rsa",
        "keys/id_ed25519",
        "keys/server.pem",
        "keys/server.key",
        "keys/server.p12",
        "keys/server.pfx",
        ".coding-agent/sessions/session.jsonl",
    ],
)
def test_default_policy_denies_sensitive_paths(
    tmp_path: Path,
    path: str,
) -> None:
    decision = load_sensitive_path_policy(tmp_path).evaluate(
        path,
        operation="read",
    )

    assert decision.allowed is False
    assert decision.rule_id == SENSITIVE_PATH_DENIAL_REASON
    assert decision.reasons[0] == SENSITIVE_PATH_DENIAL_REASON
    assert decision.path == path


@pytest.mark.parametrize("path", [".env.example", ".env.sample"])
def test_safe_environment_examples_are_explicit_exceptions(
    tmp_path: Path,
    path: str,
) -> None:
    decision = load_sensitive_path_policy(tmp_path).evaluate(
        path,
        operation="snapshot",
    )

    assert decision.allowed is True
    assert decision.rule_id == SENSITIVE_PATH_EXCEPTION_REASON


def test_safe_exception_does_not_override_sensitive_parent_directory(
    tmp_path: Path,
) -> None:
    decision = load_sensitive_path_policy(tmp_path).evaluate(
        ".ssh/.env.example",
        operation="read",
    )

    assert decision.allowed is False
    assert decision.rule_id == SENSITIVE_PATH_DENIAL_REASON


def test_policy_normalizes_windows_separators_and_matches_case_insensitively(
    tmp_path: Path,
) -> None:
    policy = load_sensitive_path_policy(tmp_path)

    decision = policy.evaluate(
        r"Packages\Service\.CoNfIg\GcLoUd\Auth.JSON",
        operation="search",
    )
    ordinary = policy.evaluate(r"Packages\Service\settings.json", operation="list")

    assert decision.allowed is False
    assert decision.path == "Packages/Service/.CoNfIg/GcLoUd/Auth.JSON"
    assert ordinary.allowed is True
    assert ordinary.rule_id == SENSITIVE_PATH_ALLOWED_REASON


def test_policy_rejects_workspace_escape_and_unknown_operations(tmp_path: Path) -> None:
    policy = load_sensitive_path_policy(tmp_path)

    with pytest.raises(ValueError, match="escapes workspace"):
        policy.evaluate("../outside", operation="read")
    with pytest.raises(ValueError, match="Unsupported path operation"):
        policy.evaluate("README.md", operation="delete")


def test_gitignore_negation_cannot_expose_sensitive_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "M5_NEGATION_SECRET"
    (tmp_path / ".gitignore").write_text(
        ".env\n!.env\n.coding-agent/\n!.coding-agent/**\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(secret, encoding="utf-8")
    state_dir = tmp_path / ".coding-agent"
    state_dir.mkdir()
    (state_dir / "session.txt").write_text(secret, encoding="utf-8")
    (tmp_path / ".env.example").write_text("SAFE_EXAMPLE=true\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text(f"visible {secret}\n", encoding="utf-8")
    monkeypatch.setattr(search_module.shutil, "which", lambda _name: None)

    config = _config(tmp_path)
    listed = execute_tool(config, "list_files", json.dumps({"path": "."}))
    read = execute_tool(config, "read_file", json.dumps({"path": ".env"}))
    many = read_many_files(str(tmp_path), [".env", ".env.example"])
    matches = search_module.search_text(workspace=str(tmp_path), pattern=secret)
    snapshot = _snapshot(tmp_path, task="inspect .env and visible.txt")

    listed_lines = set(listed.output.splitlines())
    assert "file .env" not in listed_lines
    assert "dir .coding-agent" not in listed_lines
    assert "file .env.example" in listed_lines
    assert read.ok is False
    assert read.data == {
        "reason": SENSITIVE_PATH_DENIAL_REASON,
        "operation": "read",
        "path": ".env",
    }
    assert SENSITIVE_PATH_DENIAL_REASON in read.output
    assert many[0].ok is False
    assert SENSITIVE_PATH_DENIAL_REASON in (many[0].error or "")
    assert many[1].ok is True
    assert [match.path for match in matches] == ["visible.txt"]
    assert {file.path for file in snapshot.files}.isdisjoint(
        {".env", ".coding-agent/session.txt"}
    )
    assert {sample.path for sample in snapshot.samples}.isdisjoint(
        {".env", ".coding-agent/session.txt"}
    )


def test_explicit_list_and_search_requests_return_stable_denial_reason(
    tmp_path: Path,
) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "config").write_text("Host private\n", encoding="utf-8")
    config = _config(tmp_path)

    listed = execute_tool(config, "list_files", json.dumps({"path": ".ssh"}))
    searched = execute_tool(
        config,
        "search_text",
        json.dumps({"path": ".ssh", "pattern": "private"}),
    )

    assert listed.ok is False
    assert searched.ok is False
    assert listed.data and listed.data["reason"] == SENSITIVE_PATH_DENIAL_REASON
    assert searched.data and searched.data["reason"] == SENSITIVE_PATH_DENIAL_REASON


def test_rg_results_are_filtered_by_sensitive_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text("needle secret\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("needle visible\n", encoding="utf-8")

    def event(path: str, line: str) -> dict[str, object]:
        return {
            "type": "match",
            "data": {
                "path": {"text": path},
                "lines": {"text": line},
                "line_number": 1,
                "submatches": [{"start": 0, "end": 6, "match": {"text": "needle"}}],
            },
        }

    events = [event(".env", "needle secret\n"), event("visible.txt", "needle visible\n")]
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(json.dumps(item) for item in events),
            stderr="",
        )

    monkeypatch.setattr(search_module.shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(search_module.subprocess, "run", fake_run)

    matches = search_module.search_text(workspace=str(tmp_path), pattern="needle")

    assert [match.path for match in matches] == ["visible.txt"]
    glob_values = [
        calls[0][index + 1]
        for index, value in enumerate(calls[0])
        if value == "--glob"
    ]
    assert "!**/.env" in glob_values
    assert "!**/.ssh/**" in glob_values
    assert "!**/.coding-agent/**" in glob_values


def test_sensitive_instruction_directories_are_not_loaded(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("safe root instruction", encoding="utf-8")
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "AGENTS.md").write_text("malicious secret instruction", encoding="utf-8")

    instructions = discover_agent_instructions(tmp_path)

    assert [instruction.path for instruction in instructions] == ["AGENTS.md"]
    assert "malicious" not in instructions[0].content


def test_sensitive_symlink_final_target_is_denied(
    tmp_path: Path,
) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    private_key = ssh_dir / "id_rsa"
    private_key.write_text("PRIVATE KEY MATERIAL", encoding="utf-8")
    link = tmp_path / "apparently-safe.txt"
    try:
        link.symlink_to(private_key)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    decision = load_sensitive_path_policy(tmp_path).evaluate(link, operation="read")
    result = execute_tool(
        _config(tmp_path),
        "read_file",
        json.dumps({"path": link.name}),
    )

    assert decision.allowed is False
    assert result.ok is False
    assert SENSITIVE_PATH_DENIAL_REASON in result.output
    assert "PRIVATE KEY MATERIAL" not in result.output


def test_sensitive_read_is_rejected_before_file_bytes_are_opened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_path = tmp_path / ".env"
    secret_path.write_text("SHOULD_NOT_BE_OPENED", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path.name.casefold() == ".env":
            raise AssertionError("sensitive file bytes were opened")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    results = read_many_files(str(tmp_path), [".env"])

    assert results[0].ok is False
    assert SENSITIVE_PATH_DENIAL_REASON in (results[0].error or "")


def test_verbose_artifact_expansion_honors_sensitive_source_path(
    tmp_path: Path,
) -> None:
    writer = SessionStore(tmp_path)
    session_id = writer.create({"task": "artifact policy", "workspace": str(tmp_path)})
    artifact = writer.put_artifact(
        session_id,
        b"artifact must not be expanded",
        "text/plain",
        encoding="utf-8",
    )
    writer.append(
        session_id,
        "context.created",
        {
            "workspace_context": {
                "stored": True,
                "source_path": ".env",
                "artifact": artifact_ref_to_dict(artifact),
            }
        },
    )

    payload = build_session_replay_payload(
        SessionStore(tmp_path, read_only=True),
        session_id,
        verbose=True,
    )
    context_event = next(
        item for item in payload["timeline"] if item["type"] == "context.created"
    )
    artifact_content = context_event["payload"]["workspace_context"][
        "artifact_content"
    ]

    assert artifact_content["available"] is False
    assert artifact_content["reason"] == SENSITIVE_PATH_DENIAL_REASON
    assert "artifact must not be expanded" not in json.dumps(payload)



def test_artifact_expansion_cache_is_scoped_by_source_path(
    tmp_path: Path,
) -> None:
    writer = SessionStore(tmp_path)
    session_id = writer.create({"task": "artifact cache", "workspace": str(tmp_path)})
    artifact = writer.put_artifact(
        session_id,
        b"shared artifact body",
        "text/plain",
        encoding="utf-8",
    )
    reference = artifact_ref_to_dict(artifact)
    writer.append(
        session_id,
        "context.created",
        {
            "safe": {
                "source_path": ".env.example",
                "artifact": reference,
            },
            "denied": {
                "source_path": ".env",
                "artifact": reference,
            },
        },
    )

    payload = build_session_replay_payload(
        SessionStore(tmp_path, read_only=True),
        session_id,
        verbose=True,
    )
    context_event = next(
        item for item in payload["timeline"] if item["type"] == "context.created"
    )

    assert context_event["payload"]["safe"]["artifact_content"]["text"] == (
        "shared artifact body"
    )
    assert context_event["payload"]["denied"]["artifact_content"] == {
        "available": False,
        "reason": SENSITIVE_PATH_DENIAL_REASON,
        "media_type": "text/plain",
        "encoding": "utf-8",
    }

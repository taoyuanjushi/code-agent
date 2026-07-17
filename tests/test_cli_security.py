from __future__ import annotations

from pathlib import Path

import pytest

import coding_agent.cli as cli_module
import coding_agent.tools as tools_module
from coding_agent.approvals import create_approval_decision
from coding_agent.agent import AgentRunReport
from coding_agent.cli import _preflight_sandbox, build_parser, main
from coding_agent.config import load_config
from coding_agent.security.models import SandboxCapability
from coding_agent.tools import execute_tool


IMAGE_DIGEST = "sha256:" + "a" * 64


def _capability(*, available: bool = True) -> SandboxCapability:
    return SandboxCapability(
        backend="docker",
        available=available,
        reason=None if available else "Docker is unavailable.",
        image_reference="python:3.12-slim",
        image_digest=IMAGE_DIGEST if available else None,
    )


def _patch_backend(
    monkeypatch: pytest.MonkeyPatch,
    capability: SandboxCapability,
) -> list[tuple[str, Path]]:
    calls: list[tuple[str, Path]] = []

    class FakeBackend:
        def __init__(self, image_reference: str) -> None:
            self.image_reference = image_reference

        def probe_capability(self, workspace: str | Path) -> SandboxCapability:
            calls.append((self.image_reference, Path(workspace)))
            return capability

    monkeypatch.setattr(cli_module, "DockerSandboxBackend", FakeBackend)
    return calls


def test_full_auto_implies_write_and_automatic_approvals() -> None:
    config = load_config(build_parser().parse_args(["--full-auto", "fix tests"]))

    assert config.permission_mode == "workspace-write"
    assert config.auto_approve_edits is True
    assert config.auto_approve_commands is True
    assert config.sandbox_mode == "auto"
    assert config.full_auto is True


@pytest.mark.parametrize("flag", ["--full-auto", "--auto-approve-commands"])
def test_unattended_commands_reject_disabled_sandbox(flag: str) -> None:
    config = load_config(
        build_parser().parse_args(["--sandbox", "none", flag, "fix tests"])
    )

    with pytest.raises(ValueError, match="require a Docker sandbox"):
        _preflight_sandbox(config)


def test_full_auto_pins_local_image_before_agent_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_backend(monkeypatch, _capability())
    config = load_config(
        build_parser().parse_args(
            ["--workspace", str(tmp_path), "--full-auto", "fix tests"]
        )
    )

    prepared = _preflight_sandbox(config)

    assert calls == [("python:3.12-slim", tmp_path.resolve())]
    assert prepared.sandbox_mode == "docker"
    assert prepared.sandbox_image_digest == IMAGE_DIGEST


def test_unavailable_docker_fails_before_agent_or_api_key_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _patch_backend(monkeypatch, _capability(available=False))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        cli_module,
        "run_agent_with_report",
        lambda *_args, **_kwargs: pytest.fail("agent must not start"),
    )

    exit_code = main(
        ["--workspace", str(tmp_path), "--full-auto", "fix tests"]
    )

    assert exit_code == 1
    assert calls == [("python:3.12-slim", tmp_path.resolve())]
    assert "Docker sandbox preflight failed" in capsys.readouterr().err


def test_successful_preflight_passes_pinned_config_to_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch, _capability())
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = []

    def fake_run(task, config):
        observed.append((task, config))
        return AgentRunReport("done", (), "not_run")

    monkeypatch.setattr(cli_module, "run_agent_with_report", fake_run)

    exit_code = main(
        ["--workspace", str(tmp_path), "--sandbox", "docker", "inspect"]
    )

    assert exit_code == 0
    assert observed[0][0] == "inspect"
    assert observed[0][1].sandbox_mode == "docker"
    assert observed[0][1].sandbox_image_digest == IMAGE_DIGEST


def test_pinned_docker_config_never_falls_back_to_host_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_backend(monkeypatch, _capability())
    config = _preflight_sandbox(
        load_config(
            build_parser().parse_args(
                ["--workspace", str(tmp_path), "--full-auto", "fix tests"]
            )
        )
    )
    docker_calls = []

    def fake_docker_execute(
        received_config,
        command,
        decision,
        **kwargs,
    ):
        docker_calls.append((received_config, command, decision, kwargs))
        return tools_module.ToolResult(
            ok=True,
            output="Python 3.12",
            data={
                "type": "secure_command_result",
                "status": "passed",
                "backend": "docker",
                "sandboxed": True,
                "image_digest": IMAGE_DIGEST,
            },
        )

    monkeypatch.setattr(tools_module, "_run_docker_command", fake_docker_execute)
    monkeypatch.setattr(
        tools_module,
        "_run_argv_command",
        lambda *_args, **_kwargs: pytest.fail("host execution must not run"),
    )

    def approve(request):
        assert request.details["backend"] == "docker"
        assert request.details["sandboxed"] is True
        assert request.details["network_mode"] == "none"
        return create_approval_decision(
            request,
            approved=True,
            source="auto_policy",
        )

    result = execute_tool(
        config,
        "run_command",
        '{"argv":["python","--version"]}',
        approval_handler=approve,
    )

    assert calls == [("python:3.12-slim", tmp_path.resolve())]
    assert len(docker_calls) == 1
    assert result.ok is True
    assert result.data is not None
    assert result.data["status"] == "passed"
    assert result.data["backend"] == "docker"


@pytest.mark.parametrize(
    "options",
    [
        ["--sandbox", "docker"],
        ["--sandbox-image", "custom:latest"],
        ["--full-auto"],
    ],
)
def test_resume_rejects_sandbox_overrides(
    options: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    exit_code = main(["--resume", "latest", *options])

    assert exit_code == 1
    assert "may only be used when starting a new task" in capsys.readouterr().err

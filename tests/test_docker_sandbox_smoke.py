from __future__ import annotations

import os
from pathlib import Path

import pytest

from coding_agent.security.docker_backend import (
    DEFAULT_DOCKER_IMAGE,
    DockerSandboxBackend,
)
from coding_agent.security.models import (
    CommandPolicyDecision,
    CommandSpec,
    ExecutionLimits,
    SandboxExecutionPlan,
)


@pytest.mark.docker
def test_real_docker_sandbox_executes_pinned_local_image(tmp_path: Path) -> None:
    if os.environ.get("CODING_AGENT_RUN_DOCKER_TESTS") != "1":
        pytest.skip("Set CODING_AGENT_RUN_DOCKER_TESTS=1 to run Docker smoke tests.")

    image = os.environ.get("CODING_AGENT_DOCKER_TEST_IMAGE", DEFAULT_DOCKER_IMAGE)
    (tmp_path / "message.txt").write_text("sandbox-ok\n", encoding="utf-8")
    backend = DockerSandboxBackend(image_reference=image)
    capability = backend.probe_capability(tmp_path)
    assert capability.available, capability.reason
    assert capability.image_digest is not None

    command = CommandSpec(
        argv=(
            "python",
            "-c",
            "from pathlib import Path; "
            "Path('generated.txt').write_text(Path('message.txt').read_text()); "
            "print(Path('generated.txt').read_text().strip())",
        ),
        cwd=".",
        source="verification",
        purpose="Run the opt-in Docker sandbox smoke test",
        limits=ExecutionLimits(timeout_ms=30_000),
    )
    decision = CommandPolicyDecision(
        disposition="sandbox_required",
        rule_id="sandbox.smoke-test",
        reasons=("The smoke-test interpreter command requires Docker isolation.",),
        normalized_executable="python",
        requires_approval=False,
        requires_sandbox=True,
    )
    plan = SandboxExecutionPlan(
        command=command,
        decision=decision,
        capability=capability,
        backend="docker",
        sandboxed=True,
        network_mode="none",
        image_digest=capability.image_digest,
    )

    outcome = backend.execute(
        tmp_path,
        plan,
        session_id="docker-smoke",
        call_id="read-message",
    )

    assert outcome.result.status == "passed", outcome.result.to_dict()
    assert outcome.result.output.strip() == "sandbox-ok"
    assert outcome.result.image_digest == capability.image_digest
    assert outcome.snapshot_cleanup_succeeded is True

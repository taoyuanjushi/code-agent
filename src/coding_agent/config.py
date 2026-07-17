import os
from argparse import Namespace
from pathlib import Path

from .types import (
    MAX_FIX_ATTEMPTS,
    AgentConfig,
    PermissionMode,
    ReasoningEffort,
    SandboxMode,
)

VALID_REASONING_EFFORTS: set[str] = {"none", "low", "medium", "high", "xhigh"}
VALID_SANDBOX_MODES: set[str] = {"none", "auto", "docker"}


def load_config(options: Namespace) -> AgentConfig:
    workspace = str(Path(getattr(options, "workspace", None) or os.getcwd()).resolve())
    reasoning_effort = _parse_reasoning_effort(
        getattr(options, "reasoning_effort", None)
        or os.getenv("CODING_AGENT_REASONING_EFFORT")
        or "medium"
    )
    full_auto = bool(getattr(options, "full_auto", False))
    permission_mode: PermissionMode = (
        "workspace-write"
        if getattr(options, "write", False) or full_auto
        else "read-only"
    )
    sandbox_mode = _parse_sandbox_mode(
        getattr(options, "sandbox", None)
        or os.getenv("CODING_AGENT_SANDBOX")
        or "auto"
    )

    return AgentConfig(
        workspace=workspace,
        model=getattr(options, "model", None) or os.getenv("CODING_AGENT_MODEL") or "gpt-5.5",
        reasoning_effort=reasoning_effort,
        max_turns=_parse_positive_int(getattr(options, "max_turns", None), 8, "max turns"),
        permission_mode=permission_mode,
        auto_approve_commands=(
            bool(getattr(options, "auto_approve_commands", False)) or full_auto
        ),
        auto_approve_edits=(
            bool(getattr(options, "auto_approve_edits", False)) or full_auto
        ),
        context_max_files=_parse_positive_int(
            getattr(options, "context_max_files", None),
            6,
            "context max files",
        ),
        context_max_bytes_per_file=_parse_positive_int(
            getattr(options, "context_max_bytes_per_file", None),
            8_000,
            "context max bytes per file",
        ),
        max_fix_attempts=_parse_bounded_positive_int(
            getattr(options, "max_fix_attempts", None)
            or os.getenv("CODING_AGENT_MAX_FIX_ATTEMPTS"),
            3,
            "max fix attempts",
            maximum=MAX_FIX_ATTEMPTS,
        ),
        sandbox_mode=sandbox_mode,
        sandbox_image=(
            getattr(options, "sandbox_image", None)
            or os.getenv("CODING_AGENT_SANDBOX_IMAGE")
            or "python:3.12-slim"
        ),
        full_auto=full_auto,
    )


def _parse_positive_int(value: str | int | None, fallback: int, label: str) -> int:
    if value is None:
        return fallback

    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid {label}: {value}") from exc

    if parsed <= 0:
        raise ValueError(f"Invalid {label}: {value}")

    return parsed


def _parse_bounded_positive_int(
    value: str | int | None,
    fallback: int,
    label: str,
    *,
    maximum: int,
) -> int:
    parsed = _parse_positive_int(value, fallback, label)
    if parsed > maximum:
        raise ValueError(f"Invalid {label}: {parsed} (maximum {maximum})")
    return parsed


def _parse_reasoning_effort(value: str) -> ReasoningEffort:
    if value not in VALID_REASONING_EFFORTS:
        raise ValueError(f"Invalid reasoning effort: {value}")

    return value  # type: ignore[return-value]


def _parse_sandbox_mode(value: str) -> SandboxMode:
    if value not in VALID_SANDBOX_MODES:
        raise ValueError(f"Invalid sandbox mode: {value}")
    return value  # type: ignore[return-value]

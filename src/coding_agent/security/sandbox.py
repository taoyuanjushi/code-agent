from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from .models import SandboxCapability, SandboxExecutionPlan, SecureExecutionResult


class SandboxAuthorizationError(ValueError):
    """Raised when a sandbox plan has not been authorized for execution."""


@dataclass(frozen=True)
class SandboxExecutionOutcome:
    """Command result plus non-sensitive backend and cleanup audit facts."""

    result: SecureExecutionResult
    capability: SandboxCapability
    container_name: str | None = None
    backend_argv: tuple[str, ...] = ()
    snapshot_summary: Mapping[str, object] | None = None
    container_cleanup_attempted: bool = False
    container_cleanup_succeeded: bool | None = None
    container_cleanup_error: str | None = None
    snapshot_cleanup_succeeded: bool | None = None
    snapshot_cleanup_error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.result, SecureExecutionResult):
            raise TypeError("result must be a SecureExecutionResult instance.")
        if not isinstance(self.capability, SandboxCapability):
            raise TypeError("capability must be a SandboxCapability instance.")
        if self.container_name is not None and (
            not isinstance(self.container_name, str) or not self.container_name
        ):
            raise ValueError("container_name must be a non-empty string or null.")
        if not isinstance(self.backend_argv, tuple) or any(
            not isinstance(argument, str) or not argument
            for argument in self.backend_argv
        ):
            raise TypeError("backend_argv must be a tuple of non-empty strings.")

        if self.snapshot_summary is not None:
            if not isinstance(self.snapshot_summary, Mapping):
                raise TypeError("snapshot_summary must be a mapping or null.")
            summary = dict(self.snapshot_summary)
            if any(not isinstance(key, str) for key in summary):
                raise TypeError("snapshot_summary keys must be strings.")
            object.__setattr__(
                self,
                "snapshot_summary",
                MappingProxyType(summary),
            )

        self._validate_cleanup_state(
            label="container",
            attempted=self.container_cleanup_attempted,
            succeeded=self.container_cleanup_succeeded,
            error=self.container_cleanup_error,
        )
        if self.snapshot_cleanup_succeeded is None:
            if self.snapshot_cleanup_error is not None:
                raise ValueError(
                    "snapshot_cleanup_error requires a cleanup result."
                )
        elif not isinstance(self.snapshot_cleanup_succeeded, bool):
            raise TypeError("snapshot_cleanup_succeeded must be a boolean or null.")
        elif (
            self.snapshot_cleanup_succeeded
            and self.snapshot_cleanup_error is not None
        ):
            raise ValueError(
                "successful snapshot cleanup cannot include an error."
            )

    @staticmethod
    def _validate_cleanup_state(
        *,
        label: str,
        attempted: bool,
        succeeded: bool | None,
        error: str | None,
    ) -> None:
        if not isinstance(attempted, bool):
            raise TypeError(f"{label}_cleanup_attempted must be a boolean.")
        if not attempted:
            if succeeded is not None or error is not None:
                raise ValueError(
                    f"{label} cleanup details require attempted=True."
                )
            return
        if not isinstance(succeeded, bool):
            raise TypeError(
                f"{label}_cleanup_succeeded must be a boolean after an attempt."
            )
        if succeeded and error is not None:
            raise ValueError(f"successful {label} cleanup cannot include an error.")
        if error is not None and (not isinstance(error, str) or not error):
            raise ValueError(f"{label}_cleanup_error must be non-empty or null.")

    def to_dict(self) -> dict[str, object]:
        return {
            "result": self.result.to_dict(),
            "capability": self.capability.to_dict(),
            "container_name": self.container_name,
            "backend_argv": list(self.backend_argv),
            "snapshot_summary": (
                dict(self.snapshot_summary)
                if self.snapshot_summary is not None
                else None
            ),
            "container_cleanup_attempted": self.container_cleanup_attempted,
            "container_cleanup_succeeded": self.container_cleanup_succeeded,
            "container_cleanup_error": self.container_cleanup_error,
            "snapshot_cleanup_succeeded": self.snapshot_cleanup_succeeded,
            "snapshot_cleanup_error": self.snapshot_cleanup_error,
        }


@runtime_checkable
class SandboxBackend(Protocol):
    """Capability probing and fail-closed execution contract for sandboxes."""

    def probe_capability(
        self,
        workspace: str | Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> SandboxCapability: ...

    def execute(
        self,
        workspace: str | Path,
        plan: SandboxExecutionPlan,
        *,
        session_id: str,
        call_id: str,
        approval_granted: bool = False,
        environment: Mapping[str, str] | None = None,
    ) -> SandboxExecutionOutcome: ...

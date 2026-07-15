from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

ToolEffect = Literal["read_only", "workspace_write", "process"]
ApprovalGroup = Literal["edits", "commands"]

TOOL_EFFECTS = frozenset({"read_only", "workspace_write", "process"})


@dataclass(frozen=True)
class ToolPolicy:
    """Security and approval metadata for one callable tool."""

    name: str
    effect: ToolEffect
    approval_required: bool
    approval_group: ApprovalGroup | None = None
    exposed: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("tool policy name must be a non-empty string.")
        if self.effect not in TOOL_EFFECTS:
            raise ValueError(f"Unsupported tool effect: {self.effect}")
        if self.approval_group not in {None, "edits", "commands"}:
            raise ValueError(
                f"Unsupported approval group: {self.approval_group}"
            )
        if self.approval_required != (self.approval_group is not None):
            raise ValueError(
                "approval_required must match whether approval_group is set."
            )


_POLICIES = (
    ToolPolicy("read_file", "read_only", False),
    ToolPolicy("read_many_files", "read_only", False),
    ToolPolicy("list_files", "read_only", False),
    ToolPolicy("search_text", "read_only", False),
    ToolPolicy("discover_verification_commands", "read_only", False),
    ToolPolicy("git_status", "read_only", False),
    ToolPolicy("git_diff", "read_only", False),
    ToolPolicy("apply_patch", "workspace_write", True, "edits"),
    ToolPolicy("run_verification", "process", True, "commands"),
    ToolPolicy("run_command", "process", True, "commands"),
    ToolPolicy(
        "write_file",
        "workspace_write",
        True,
        "edits",
        exposed=False,
    ),
)

TOOL_POLICIES: Mapping[str, ToolPolicy] = MappingProxyType(
    {policy.name: policy for policy in _POLICIES}
)



def get_tool_policy(name: str) -> ToolPolicy:
    """Return policy metadata, using a conservative effect for unknown tools."""

    policy = TOOL_POLICIES.get(name)
    if policy is not None:
        return policy
    return ToolPolicy(name=name, effect="process", approval_required=False, exposed=False)



def exposed_tool_names() -> frozenset[str]:
    return frozenset(
        policy.name for policy in TOOL_POLICIES.values() if policy.exposed
    )



def hash_tool_arguments(raw_arguments: str) -> str:
    if not isinstance(raw_arguments, str):
        raise TypeError("tool arguments must be a string.")
    return hashlib.sha256(raw_arguments.encode("utf-8")).hexdigest()



def summarize_tool_arguments(raw_arguments: str) -> str:
    """Build a small audit label without copying large argument values."""

    try:
        parsed = json.loads(raw_arguments) if raw_arguments.strip() else {}
    except json.JSONDecodeError:
        return f"invalid JSON ({len(raw_arguments.encode('utf-8'))} bytes)"
    if not isinstance(parsed, dict):
        return f"non-object JSON ({len(raw_arguments.encode('utf-8'))} bytes)"
    if not parsed:
        return "no arguments"
    keys = ", ".join(sorted(str(key) for key in parsed))
    return f"keys: {keys}; {len(raw_arguments.encode('utf-8'))} bytes"

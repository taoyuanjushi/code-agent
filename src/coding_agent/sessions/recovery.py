from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from ..approvals import ApprovalRequest
from ..path_safety import resolve_workspace_path
from ..tool_outputs import build_persistable_tool_output, thaw_json
from ..tool_policy import TOOL_EFFECTS, ToolEffect, hash_tool_arguments
from ..tools import VerificationToolState
from ..types import ToolResult
from .codec import (
    approval_request_from_dict,
    verification_tool_state_from_dict,
    verification_tool_state_to_dict,
)
from .models import AgentSessionState, JsonObject, PendingToolCall, SessionEvent
from .store import SessionStore

RecoveryDisposition = Literal[
    "reuse_completed",
    "safe_retry",
    "recovered_completed",
    "requires_reapproval",
    "workspace_drift",
]
FileHashMatch = Literal["before", "after", "drift"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ToolRecoveryError(RuntimeError):
    """Raised when an interrupted tool cannot be reconciled safely."""


class WorkspaceDriftError(ToolRecoveryError):
    """Raised when current files match neither audited patch boundary."""

    def __init__(
        self,
        call_id: str,
        observations: tuple["FileHashObservation", ...],
    ) -> None:
        self.call_id = call_id
        self.observations = observations
        drifted = tuple(item for item in observations if item.match == "drift")
        reported = drifted or observations
        details = "; ".join(
            (
                f"{item.path}: before={item.before_sha256}, "
                f"after={item.after_sha256}, current={item.current_sha256}, "
                f"match={item.match}"
            )
            for item in reported
        )
        super().__init__(
            f"Workspace drift prevents recovery of tool call {call_id!r}"
            + (f": {details}" if details else ".")
        )


@dataclass(frozen=True)
class FileHashObservation:
    path: str
    before_sha256: str | None
    after_sha256: str | None
    current_sha256: str | None
    match: FileHashMatch


@dataclass(frozen=True)
class ToolRecoveryPlan:
    call_id: str
    name: str
    effect: ToolEffect
    disposition: RecoveryDisposition
    reason: str
    tool_output: JsonObject | None = None
    recovered_result: ToolResult | None = None
    verification_state: dict[str, object] | None = None
    touched_file_hashes: dict[str, str | None] | None = None
    approval_request: ApprovalRequest | None = None
    file_hashes: tuple[FileHashObservation, ...] = ()

    @property
    def can_continue(self) -> bool:
        return self.disposition != "workspace_drift"

    @property
    def requires_explicit_approval(self) -> bool:
        return self.disposition == "requires_reapproval"

    def raise_for_workspace_drift(self) -> None:
        if self.disposition == "workspace_drift":
            raise WorkspaceDriftError(self.call_id, self.file_hashes)


def find_completed_tool_output(
    events: Sequence[SessionEvent],
    call_id: str,
) -> JsonObject | None:
    """Return the exact persisted function-call output for a completed call ID."""

    _require_non_empty_string(call_id, "call_id")
    matches: list[JsonObject] = []
    for event in events:
        if event.type not in {"tool.finished", "tool.recovered"}:
            continue
        data = _tool_event_payload(event.payload)
        if data.get("call_id") != call_id:
            continue
        if event.type == "tool.recovered" and data.get("completed") is False:
            continue
        output = _extract_function_call_output(data, call_id)
        matches.append(output)

    if not matches:
        return None
    canonical = _canonical_json(matches[0])
    if any(_canonical_json(item) != canonical for item in matches[1:]):
        raise ToolRecoveryError(
            f"Completed tool call {call_id!r} has conflicting persisted outputs."
        )
    return matches[0]


# Backward-friendly descriptive alias for callers that prefer persistence wording.
find_persisted_tool_output = find_completed_tool_output


def find_recovery_reapproval_call_ids(
    events: Sequence[SessionEvent],
    state: AgentSessionState,
) -> frozenset[str]:
    """Return pending calls whose persisted recovery reset requires approval."""

    pending_ids = {call.call_id for call in state.pending_tool_calls}
    required: set[str] = set()
    for event in events:
        if event.type != "tool.recovered":
            continue
        data = _tool_event_payload(event.payload)
        call_id = data.get("call_id")
        if call_id not in pending_ids:
            continue
        if data.get("completed") is False and data.get("requires_reapproval") is True:
            required.add(cast(str, call_id))
    return frozenset(required)

def plan_interrupted_tools(
    workspace: str | Path,
    events: Sequence[SessionEvent],
    state: AgentSessionState | None = None,
) -> tuple[ToolRecoveryPlan, ...]:
    """Plan recovery for every started call that lacks a completion event."""

    effective_state = state or _rebuild(events)
    return tuple(
        plan_tool_recovery(workspace, events, effective_state, call.call_id)
        for call in effective_state.pending_tool_calls
        if call.started
    )


def plan_tool_recovery(
    workspace: str | Path,
    events: Sequence[SessionEvent],
    state: AgentSessionState,
    call_id: str,
) -> ToolRecoveryPlan:
    """Classify one call without executing tools or mutating the workspace."""

    completed_output = find_completed_tool_output(events, call_id)
    if completed_output is not None:
        name, effect = _completed_identity(events, call_id)
        return ToolRecoveryPlan(
            call_id=call_id,
            name=name,
            effect=effect,
            disposition="reuse_completed",
            reason="persisted_output",
            tool_output=completed_output,
        )

    call = _pending_call(state, call_id)
    if not call.started:
        raise ToolRecoveryError(
            f"Tool call {call_id!r} has no interrupted tool.started event."
        )

    if call.effect in {"read_only", "session_only"}:
        return ToolRecoveryPlan(
            call_id=call.call_id,
            name=call.name,
            effect=call.effect,
            disposition="safe_retry",
            reason="safe_retry",
        )

    approval_request = _find_approval_request(events, call)
    if call.name == "apply_patch":
        return _plan_patch_recovery(
            workspace,
            state,
            call,
            approval_request,
        )

    if call.effect == "process":
        return ToolRecoveryPlan(
            call_id=call.call_id,
            name=call.name,
            effect=call.effect,
            disposition="requires_reapproval",
            reason="unknown_process_result",
            approval_request=_recovery_approval_request(
                call,
                approval_request,
                reason="unknown_process_result",
            ),
        )

    return ToolRecoveryPlan(
        call_id=call.call_id,
        name=call.name,
        effect=call.effect,
        disposition="workspace_drift",
        reason="unsupported_workspace_write_recovery",
        approval_request=approval_request,
    )


def build_recovery_event_payload(
    plan: ToolRecoveryPlan,
    *,
    store: SessionStore | None = None,
    session_id: str | None = None,
) -> dict[str, object]:
    """Build a reducer-compatible ``tool.recovered`` event payload."""

    identity: dict[str, object] = {
        "call_id": plan.call_id,
        "name": plan.name,
        "effect": plan.effect,
        "reason": plan.reason,
    }
    if plan.disposition in {"safe_retry", "requires_reapproval"}:
        return {
            **identity,
            "completed": False,
            "requires_reapproval": plan.requires_explicit_approval,
        }
    if plan.disposition == "workspace_drift":
        plan.raise_for_workspace_drift()
    if plan.disposition == "reuse_completed":
        raise ToolRecoveryError(
            "A completed call already has an event; reuse its output without "
            "appending another completion."
        )
    if plan.recovered_result is None:
        raise ToolRecoveryError("Recovered completion is missing its ToolResult.")
    if store is None or session_id is None:
        raise ToolRecoveryError(
            "Recovered completion requires a SessionStore and session_id so its "
            "output follows the normal artifact policy."
        )

    payload: dict[str, object] = {
        **identity,
        "completed": True,
        "tool_output": build_persistable_tool_output(
            store,
            session_id,
            plan.call_id,
            plan.recovered_result,
        ),
    }
    if plan.verification_state is not None:
        payload["verification_state"] = plan.verification_state
    if plan.touched_file_hashes is not None:
        payload["touched_file_hashes"] = plan.touched_file_hashes
    return payload


def _plan_patch_recovery(
    workspace: str | Path,
    state: AgentSessionState,
    call: PendingToolCall,
    approval_request: ApprovalRequest | None,
) -> ToolRecoveryPlan:
    if approval_request is None:
        return ToolRecoveryPlan(
            call_id=call.call_id,
            name=call.name,
            effect=call.effect,
            disposition="workspace_drift",
            reason="missing_patch_recovery_metadata",
        )

    details = approval_request.details
    raw_changes = details.get("file_changes")
    if not isinstance(raw_changes, tuple) or not raw_changes:
        return ToolRecoveryPlan(
            call_id=call.call_id,
            name=call.name,
            effect=call.effect,
            disposition="workspace_drift",
            reason="missing_patch_recovery_metadata",
            approval_request=approval_request,
        )

    observations = tuple(
        _observe_file_hash(workspace, _mapping(item, "file_changes item"))
        for item in raw_changes
    )
    all_after = all(item.match == "after" for item in observations)
    all_before = all(item.match == "before" for item in observations)

    if all_after:
        after_hashes = {item.path: item.after_sha256 for item in observations}
        file_changes = [
            {
                "path": item.path,
                "change_type": _change_type_for_path(raw_changes, item.path),
                "before_sha256": item.before_sha256,
                "after_sha256": item.after_sha256,
            }
            for item in observations
        ]
        data: dict[str, object] = {
            "type": "patch_applied",
            "changed_paths": [item.path for item in observations],
            "diff_sha256": _required_sha256(
                details.get("diff_sha256"),
                "approval details diff_sha256",
            ),
            "file_changes": file_changes,
            "touched_file_hashes": after_hashes,
            "recovered": True,
            "recovery_reason": "patch_after_hash_match",
        }
        verification = verification_tool_state_from_dict(state.verification_state)
        verification.record_patch_applied()
        data.update(
            {
                "edit_generation": verification.edit_generation,
                "failed_command_id": verification.unresolved_failure_command_id,
                "repair_attempts": verification.repair_attempts,
                "max_fix_attempts": verification.max_fix_attempts,
                "repair_limit_reached": verification.repair_limit_reached,
            }
        )
        touched = {
            key: cast(str | None, value)
            for key, value in state.touched_file_hashes.items()
        }
        touched.update(after_hashes)
        return ToolRecoveryPlan(
            call_id=call.call_id,
            name=call.name,
            effect=call.effect,
            disposition="recovered_completed",
            reason="patch_after_hash_match",
            recovered_result=ToolResult(
                ok=True,
                output=f"Applied patch:\n{approval_request.summary}",
                data=data,
            ),
            verification_state=verification_tool_state_to_dict(verification),
            touched_file_hashes=touched,
            approval_request=approval_request,
            file_hashes=observations,
        )

    if all_before:
        return ToolRecoveryPlan(
            call_id=call.call_id,
            name=call.name,
            effect=call.effect,
            disposition="requires_reapproval",
            reason="patch_before_hash_match",
            approval_request=_recovery_approval_request(
                call,
                approval_request,
                reason="patch_before_hash_match",
            ),
            file_hashes=observations,
        )

    return ToolRecoveryPlan(
        call_id=call.call_id,
        name=call.name,
        effect=call.effect,
        disposition="workspace_drift",
        reason="patch_hash_mismatch",
        approval_request=approval_request,
        file_hashes=observations,
    )


def _observe_file_hash(
    workspace: str | Path,
    item: Mapping[str, object],
) -> FileHashObservation:
    path = _required_string(item.get("path"), "file change path")
    before = _optional_sha256(item.get("before_sha256"), "before_sha256")
    after = _optional_sha256(item.get("after_sha256"), "after_sha256")
    absolute = resolve_workspace_path(
        workspace,
        path,
        operation="write",
        allow_missing=True,
    )
    current = _hash_file_or_none(absolute)
    if current == after:
        match: FileHashMatch = "after"
    elif current == before:
        match = "before"
    else:
        match = "drift"
    return FileHashObservation(
        path=path,
        before_sha256=before,
        after_sha256=after,
        current_sha256=current,
        match=match,
    )


def _find_approval_request(
    events: Sequence[SessionEvent],
    call: PendingToolCall,
) -> ApprovalRequest | None:
    for event in reversed(events):
        if event.type != "approval.decided":
            continue
        raw_request = event.payload.get("request")
        if not isinstance(raw_request, Mapping):
            continue
        request = approval_request_from_dict(_mapping(raw_request, "request"))
        if request.call_id != call.call_id:
            continue
        if request.action != call.name:
            raise ToolRecoveryError(
                f"Approval action for {call.call_id!r} does not match its tool."
            )
        if request.arguments_sha256 != hash_tool_arguments(call.arguments):
            raise ToolRecoveryError(
                f"Approval arguments for {call.call_id!r} do not match its tool."
            )
        return request
    return None


def _recovery_approval_request(
    call: PendingToolCall,
    original: ApprovalRequest | None,
    *,
    reason: str,
) -> ApprovalRequest:
    details: dict[str, object]
    if original is None:
        details = {"arguments": call.arguments}
        summary = f"Retry interrupted {call.name} call"
    else:
        details = cast(dict[str, object], thaw_json(original.details))
        summary = original.summary
    details["resume_recovery"] = {
        "reason": reason,
        "prior_result": "unknown",
        "auto_approval_allowed": False,
    }
    return ApprovalRequest(
        call_id=call.call_id,
        action=call.name,
        summary=summary,
        arguments_sha256=hash_tool_arguments(call.arguments),
        details=details,
    )


def _completed_identity(
    events: Sequence[SessionEvent],
    call_id: str,
) -> tuple[str, ToolEffect]:
    for event in reversed(events):
        if event.type not in {"tool.finished", "tool.recovered"}:
            continue
        data = _tool_event_payload(event.payload)
        if data.get("call_id") != call_id:
            continue
        name = _required_string(data.get("name"), "completed tool name")
        effect = data.get("effect")
        if effect not in TOOL_EFFECTS:
            raise ToolRecoveryError(
                f"Completed tool call {call_id!r} has no valid effect metadata."
            )
        return name, cast(ToolEffect, effect)
    raise ToolRecoveryError(f"Completed tool call {call_id!r} has no identity event.")


def _pending_call(state: AgentSessionState, call_id: str) -> PendingToolCall:
    for call in state.pending_tool_calls:
        if call.call_id == call_id:
            return call
    raise ToolRecoveryError(f"Unknown pending tool call ID: {call_id!r}.")


def _tool_event_payload(payload: Mapping[str, object]) -> Mapping[str, object]:
    wrapped = payload.get("call", payload.get("tool_call"))
    if wrapped is None:
        return payload
    data = _mapping(wrapped, "tool call")
    merged = {
        key: value
        for key, value in payload.items()
        if key not in {"call", "tool_call"}
    }
    merged.update(data)
    return merged


def _extract_function_call_output(
    data: Mapping[str, object],
    call_id: str,
) -> JsonObject:
    explicit = data.get("tool_output")
    if explicit is not None:
        output = _mapping(explicit, "tool_output")
    elif "output" in data:
        value = data["output"]
        output = (
            _mapping(value, "output")
            if isinstance(value, Mapping)
            and value.get("type") == "function_call_output"
            else {
                "type": "function_call_output",
                "call_id": call_id,
                "output": value,
            }
        )
    elif "result" in data:
        value = data["result"]
        output = {
            "type": "function_call_output",
            "call_id": call_id,
            "output": value,
        }
    else:
        raise ToolRecoveryError(
            f"Completed tool call {call_id!r} has no persisted output."
        )
    if output.get("call_id") != call_id:
        raise ToolRecoveryError(
            f"Persisted output call ID does not match {call_id!r}."
        )
    return cast(JsonObject, output)


def _change_type_for_path(
    raw_changes: tuple[object, ...],
    path: str,
) -> str:
    for raw in raw_changes:
        item = _mapping(raw, "file_changes item")
        if item.get("path") == path:
            value = item.get("change_type")
            if value not in {"add", "modify", "delete"}:
                raise ToolRecoveryError(
                    f"Invalid patch change_type for {path!r}: {value!r}."
                )
            return cast(str, value)
    raise ToolRecoveryError(f"Missing patch metadata for {path!r}.")


def _hash_file_or_none(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise ToolRecoveryError(f"Recovery target is not a regular file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _optional_sha256(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_sha256(value, label)


def _required_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ToolRecoveryError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ToolRecoveryError(f"{label} must be a non-empty string.")
    return value


def _require_non_empty_string(value: object, label: str) -> None:
    _required_string(value, label)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ToolRecoveryError(f"{label} must be an object with string keys.")
    return cast(Mapping[str, object], value)


def _canonical_json(value: object) -> str:
    return json.dumps(
        thaw_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _rebuild(events: Sequence[SessionEvent]) -> AgentSessionState:
    from .reducer import rebuild_state

    return rebuild_state(tuple(events))

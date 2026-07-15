from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal, Mapping, TypeAlias, cast

from ..approvals import (
    APPROVAL_OUTCOMES,
    APPROVAL_SOURCES,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalSource,
)
from ..tool_policy import TOOL_EFFECTS, ToolEffect

SESSION_SCHEMA_VERSION = 1
SHA256_HEX_LENGTH = 64

SessionStatus = Literal["running", "completed", "failed", "interrupted"]
SessionPhase = Literal[
    "awaiting_initial_model",
    "awaiting_tools",
    "awaiting_model",
    "finalizing",
    "completed",
]
SessionEventType = Literal[
    "session.started",
    "session.resumed",
    "session.completed",
    "session.failed",
    "session.interrupted",
    "context.created",
    "model.requested",
    "model.responded",
    "tool.started",
    "tool.finished",
    "tool.recovered",
    "approval.decided",
    "verification.recorded",
    "checkpoint.saved",
]

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
JsonObject: TypeAlias = Mapping[str, JsonValue]

SESSION_STATUSES = frozenset({"running", "completed", "failed", "interrupted"})
SESSION_PHASES = frozenset(
    {
        "awaiting_initial_model",
        "awaiting_tools",
        "awaiting_model",
        "finalizing",
        "completed",
    }
)
SESSION_CONFIG_FIELDS = frozenset(
    {
        "workspace",
        "model",
        "reasoning_effort",
        "max_turns",
        "permission_mode",
        "auto_approve_commands",
        "auto_approve_edits",
        "context_max_files",
        "context_max_bytes_per_file",
        "max_fix_attempts",
    }
)
SESSION_EVENT_TYPES = frozenset(
    {
        "session.started",
        "session.resumed",
        "session.completed",
        "session.failed",
        "session.interrupted",
        "context.created",
        "model.requested",
        "model.responded",
        "tool.started",
        "tool.finished",
        "tool.recovered",
        "approval.decided",
        "verification.recorded",
        "checkpoint.saved",
    }
)
_SESSION_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8,}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    sha256: str
    byte_count: int
    media_type: str
    encoding: str | None = None

    def __post_init__(self) -> None:
        _validate_relative_posix_path(self.path, "artifact path")
        _validate_sha256(self.sha256, "artifact sha256")
        _validate_non_negative_int(self.byte_count, "artifact byte_count")
        _validate_non_empty_string(self.media_type, "artifact media_type")
        if self.encoding is not None:
            _validate_non_empty_string(self.encoding, "artifact encoding")


@dataclass(frozen=True)
class WorkspaceGuard:
    workspace: str
    git_head: str | None
    touched_file_hashes: JsonObject

    def __post_init__(self) -> None:
        _validate_absolute_path(self.workspace, "workspace")
        if self.git_head is not None:
            _validate_non_empty_string(self.git_head, "git_head")
        hashes = _freeze_json_object(self.touched_file_hashes, "touched_file_hashes")
        for path, value in hashes.items():
            _validate_relative_posix_path(path, "touched file path")
            if value is not None:
                if not isinstance(value, str):
                    raise TypeError("touched file hash must be a string or null.")
                _validate_sha256(value, f"hash for {path}")
        object.__setattr__(self, "touched_file_hashes", hashes)


@dataclass(frozen=True)
class SessionStarted:
    task: str
    workspace: str
    config: JsonObject
    git_head: str | None
    workspace_guard: WorkspaceGuard

    def __post_init__(self) -> None:
        _validate_non_empty_string(self.task, "task")
        _validate_absolute_path(self.workspace, "workspace")
        if self.workspace != self.workspace_guard.workspace:
            raise ValueError("workspace must match workspace_guard.workspace.")
        if self.git_head != self.workspace_guard.git_head:
            raise ValueError("git_head must match workspace_guard.git_head.")
        config = _freeze_json_object(self.config, "session config")
        unsupported = sorted(set(config) - SESSION_CONFIG_FIELDS)
        if unsupported:
            raise ValueError(
                "session config contains unsupported persisted fields: "
                + ", ".join(unsupported)
            )
        object.__setattr__(self, "config", config)


@dataclass(frozen=True)
class ModelFunctionCall:
    call_id: str
    name: str
    arguments: str

    def __post_init__(self) -> None:
        _validate_non_empty_string(self.call_id, "call_id")
        _validate_non_empty_string(self.name, "function name")
        if not isinstance(self.arguments, str):
            raise TypeError("function arguments must be a string.")


@dataclass(frozen=True)
class NormalizedModelResponse:
    response_id: str
    text: str
    reasoning_summary: str
    function_calls: tuple[ModelFunctionCall, ...]

    def __post_init__(self) -> None:
        _validate_non_empty_string(self.response_id, "response_id")
        if not isinstance(self.text, str):
            raise TypeError("model response text must be a string.")
        if not isinstance(self.reasoning_summary, str):
            raise TypeError("reasoning_summary must be a string.")
        calls = _require_tuple(self.function_calls, "function_calls")
        if not all(isinstance(call, ModelFunctionCall) for call in calls):
            raise TypeError("function_calls must contain ModelFunctionCall values.")
        call_ids = [call.call_id for call in calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("function call IDs must be unique within a response.")


@dataclass(frozen=True)
class PendingToolCall:
    call_id: str
    name: str
    arguments: str
    effect: ToolEffect
    started: bool

    def __post_init__(self) -> None:
        _validate_non_empty_string(self.call_id, "call_id")
        _validate_non_empty_string(self.name, "tool name")
        if not isinstance(self.arguments, str):
            raise TypeError("tool arguments must be a string.")
        if self.effect not in TOOL_EFFECTS:
            raise ValueError(f"Unsupported tool effect: {self.effect}")
        if not isinstance(self.started, bool):
            raise TypeError("started must be a boolean.")


@dataclass(frozen=True)
class AgentSessionCheckpoint:
    phase: SessionPhase
    turn_index: int
    previous_response_id: str | None
    pending_tool_calls: tuple[PendingToolCall, ...]
    pending_tool_outputs: tuple[JsonObject, ...]
    completed_call_ids: frozenset[str]
    verification_state: JsonObject
    touched_file_hashes: JsonObject

    def __post_init__(self) -> None:
        if self.phase not in SESSION_PHASES:
            raise ValueError(f"Unsupported session phase: {self.phase}")
        _validate_non_negative_int(self.turn_index, "turn_index")
        if self.previous_response_id is not None:
            _validate_non_empty_string(
                self.previous_response_id,
                "previous_response_id",
            )

        calls = _require_tuple(self.pending_tool_calls, "pending_tool_calls")
        if not all(isinstance(call, PendingToolCall) for call in calls):
            raise TypeError("pending_tool_calls must contain PendingToolCall values.")
        pending_call_ids = [call.call_id for call in calls]
        if len(pending_call_ids) != len(set(pending_call_ids)):
            raise ValueError("pending tool call IDs must be unique.")

        outputs = _require_tuple(self.pending_tool_outputs, "pending_tool_outputs")
        frozen_outputs = tuple(
            _freeze_json_object(output, "pending tool output") for output in outputs
        )
        object.__setattr__(self, "pending_tool_outputs", frozen_outputs)

        if not isinstance(self.completed_call_ids, frozenset):
            raise TypeError("completed_call_ids must be a frozenset.")
        for call_id in self.completed_call_ids:
            _validate_non_empty_string(call_id, "completed call ID")
        if set(pending_call_ids) & self.completed_call_ids:
            raise ValueError("pending and completed tool call IDs must be disjoint.")

        object.__setattr__(
            self,
            "verification_state",
            _freeze_json_object(self.verification_state, "verification_state"),
        )
        hashes = _freeze_json_object(
            self.touched_file_hashes,
            "touched_file_hashes",
        )
        for path, value in hashes.items():
            _validate_relative_posix_path(path, "touched file path")
            if value is not None:
                if not isinstance(value, str):
                    raise TypeError("touched file hash must be a string or null.")
                _validate_sha256(value, f"hash for {path}")
        object.__setattr__(self, "touched_file_hashes", hashes)

        if self.phase == "awaiting_initial_model" and self.previous_response_id is not None:
            raise ValueError(
                "awaiting_initial_model cannot have a previous response ID."
            )
        if self.phase == "completed" and calls:
            raise ValueError("completed checkpoint cannot have pending tool calls.")


@dataclass(frozen=True)
class AgentSessionState:
    """Immutable state rebuilt from an ordered session event stream."""

    session_id: str
    task: str
    phase: SessionPhase
    turn_index: int
    previous_response_id: str | None
    pending_tool_calls: tuple[PendingToolCall, ...]
    pending_tool_outputs: tuple[JsonObject, ...]
    completed_call_ids: frozenset[str]
    verification_state: JsonObject
    touched_file_hashes: JsonObject
    status: SessionStatus = "running"
    approvals: tuple[ApprovalDecision, ...] = ()
    context_created: bool = field(default=False, repr=False, compare=False)
    model_request_pending: bool = field(default=False, repr=False, compare=False)
    last_seq: int = field(default=0, repr=False, compare=False)
    last_event_hash: str | None = field(default=None, repr=False, compare=False)
    last_event_type: SessionEventType | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        _validate_session_id(self.session_id)
        _validate_non_empty_string(self.task, "task")

        checkpoint = AgentSessionCheckpoint(
            phase=self.phase,
            turn_index=self.turn_index,
            previous_response_id=self.previous_response_id,
            pending_tool_calls=self.pending_tool_calls,
            pending_tool_outputs=self.pending_tool_outputs,
            completed_call_ids=self.completed_call_ids,
            verification_state=self.verification_state,
            touched_file_hashes=self.touched_file_hashes,
        )
        object.__setattr__(self, "pending_tool_calls", checkpoint.pending_tool_calls)
        object.__setattr__(
            self,
            "pending_tool_outputs",
            checkpoint.pending_tool_outputs,
        )
        object.__setattr__(
            self,
            "completed_call_ids",
            checkpoint.completed_call_ids,
        )
        object.__setattr__(
            self,
            "verification_state",
            checkpoint.verification_state,
        )
        object.__setattr__(
            self,
            "touched_file_hashes",
            checkpoint.touched_file_hashes,
        )

        if self.status not in SESSION_STATUSES:
            raise ValueError(f"Unsupported session status: {self.status}")
        approvals = _require_tuple(self.approvals, "approvals")
        if not all(isinstance(item, ApprovalDecision) for item in approvals):
            raise TypeError("approvals must contain ApprovalDecision values.")
        approval_ids = [item.approval_id for item in approvals]
        if len(approval_ids) != len(set(approval_ids)):
            raise ValueError("approval IDs must be unique within session state.")
        if not isinstance(self.context_created, bool):
            raise TypeError("context_created must be a boolean.")
        if not isinstance(self.model_request_pending, bool):
            raise TypeError("model_request_pending must be a boolean.")
        _validate_non_negative_int(self.last_seq, "last_seq")
        if self.last_seq == 0:
            if self.last_event_hash is not None or self.last_event_type is not None:
                raise ValueError(
                    "an empty reduction state cannot have last-event metadata."
                )
        else:
            if self.last_event_hash is None or self.last_event_type is None:
                raise ValueError(
                    "a reduced state must include last-event hash and type."
                )
            _validate_sha256(self.last_event_hash, "last_event_hash")
            if self.last_event_type not in SESSION_EVENT_TYPES:
                raise ValueError(
                    f"Unsupported last event type: {self.last_event_type}"
                )
        if self.status == "completed" and self.phase != "completed":
            raise ValueError("completed session status requires completed phase.")
        if self.model_request_pending and self.phase not in {
            "awaiting_initial_model",
            "awaiting_model",
        }:
            raise ValueError(
                "a pending model request requires an awaiting-model phase."
            )

    def to_checkpoint(self) -> AgentSessionCheckpoint:
        """Return the resumable portion of this state."""

        return AgentSessionCheckpoint(
            phase=self.phase,
            turn_index=self.turn_index,
            previous_response_id=self.previous_response_id,
            pending_tool_calls=self.pending_tool_calls,
            pending_tool_outputs=self.pending_tool_outputs,
            completed_call_ids=self.completed_call_ids,
            verification_state=self.verification_state,
            touched_file_hashes=self.touched_file_hashes,
        )


@dataclass(frozen=True)
class SessionEvent:
    schema_version: int
    session_id: str
    seq: int
    event_id: str
    recorded_at: str
    type: SessionEventType
    prev_hash: str | None
    payload: JsonObject
    event_hash: str

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(
            self.schema_version, int
        ):
            raise TypeError("schema_version must be an integer.")
        if self.schema_version != SESSION_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported session schema version: {self.schema_version}"
            )
        _validate_session_id(self.session_id)
        _validate_positive_int(self.seq, "seq")
        if not isinstance(self.event_id, str) or not _EVENT_ID.fullmatch(
            self.event_id
        ):
            raise ValueError("event_id must use the supported identifier format.")
        _validate_utc_timestamp(self.recorded_at, "recorded_at")
        if self.type not in SESSION_EVENT_TYPES:
            raise ValueError(f"Unsupported session event type: {self.type}")
        if self.prev_hash is not None:
            _validate_sha256(self.prev_hash, "prev_hash")
        _validate_sha256(self.event_hash, "event_hash")
        object.__setattr__(
            self,
            "payload",
            _freeze_json_object(self.payload, "event payload"),
        )



def _freeze_json_object(value: object, label: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    frozen: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError(f"{label} keys must be strings.")
        frozen[key] = _freeze_json_value(item, f"{label}.{key}")
    return MappingProxyType(frozen)



def _freeze_json_value(value: object, label: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool)):
        return cast(JsonScalar, value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError(f"{label} must be a finite JSON number.")
        return value
    if isinstance(value, Mapping):
        return _freeze_json_object(value, label)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json_value(item, f"{label}[{index}]")
            for index, item in enumerate(value)
        )
    raise TypeError(f"{label} is not JSON-compatible: {type(value).__name__}.")



def _require_tuple(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{label} must be a tuple.")
    return value



def _validate_non_empty_string(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string.")



def _validate_positive_int(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")



def _validate_non_negative_int(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer.")



def _validate_session_id(value: object) -> None:
    if not isinstance(value, str) or not _SESSION_ID.fullmatch(value):
        raise ValueError("session_id must use YYYYMMDDTHHMMSSZ-<random hex> format.")



def _validate_sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest.")



def _validate_utc_timestamp(value: object, label: str) -> None:
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise ValueError(f"{label} must be an ISO-8601 UTC timestamp ending in Z.")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid ISO-8601 UTC timestamp.") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{label} must be UTC.")



def _validate_relative_posix_path(value: object, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty relative POSIX path.")
    if "\\" in value:
        raise ValueError(f"{label} must use POSIX separators.")
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"{label} must not contain a Windows drive prefix.")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError(f"{label} cannot contain empty, dot, or parent components.")
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("/"):
        raise ValueError(f"{label} must be relative.")
    if path.as_posix() != value:
        raise ValueError(f"{label} must be normalized POSIX syntax.")



def _validate_absolute_path(value: object, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty absolute path.")
    windows_absolute = bool(re.match(r"^[A-Za-z]:[\\/]", value))
    windows_unc = value.startswith("\\\\")
    posix_absolute = value.startswith("/")
    if not windows_absolute and not windows_unc and not posix_absolute:
        raise ValueError(f"{label} must be absolute.")
    normalized_parts = value.replace("\\", "/").split("/")
    if any(part in {".", ".."} for part in normalized_parts):
        raise ValueError(f"{label} must be normalized without dot components.")

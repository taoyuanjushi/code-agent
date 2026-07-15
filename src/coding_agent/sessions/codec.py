from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

from ..tools import VerificationToolState
from ..verification import (
    VerificationCommand,
    VerificationDiscoveryResult,
    VerificationResult,
)
from .models import (
    SESSION_SCHEMA_VERSION,
    AgentSessionCheckpoint,
    ApprovalDecision,
    ApprovalRequest,
    ArtifactRef,
    JsonObject,
    JsonValue,
    ModelFunctionCall,
    NormalizedModelResponse,
    PendingToolCall,
    SessionEvent,
    SessionEventType,
    SessionPhase,
    SessionStarted,
    ToolEffect,
    WorkspaceGuard,
)

_CANONICAL_JSON_OPTIONS: dict[str, object] = {
    "ensure_ascii": False,
    "sort_keys": True,
    "separators": (",", ":"),
    "allow_nan": False,
}


class SessionCodecError(ValueError):
    """Raised when persisted session data cannot be safely decoded."""

    def __init__(
        self,
        message: str,
        *,
        source: str = "<memory>",
        line_number: int = 1,
    ) -> None:
        self.message = message
        self.source = source
        self.line_number = line_number
        super().__init__(f"{source}:{line_number}: {message}")


def canonical_json(value: object) -> str:
    """Return stable UTF-8 JSON text for protocol hashing and persistence."""

    try:
        return json.dumps(value, **_CANONICAL_JSON_OPTIONS)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Value is not canonical JSON-compatible: {exc}") from exc


def canonical_json_bytes(value: object) -> bytes:
    return canonical_json(value).encode("utf-8")


def calculate_event_hash(value: SessionEvent | Mapping[str, object]) -> str:
    data = session_event_to_dict(value) if isinstance(value, SessionEvent) else dict(value)
    data.pop("event_hash", None)
    return hashlib.sha256(canonical_json_bytes(data)).hexdigest()


def create_session_event(
    *,
    session_id: str,
    seq: int,
    event_id: str,
    recorded_at: str,
    event_type: SessionEventType,
    prev_hash: str | None,
    payload: Mapping[str, object],
) -> SessionEvent:
    data: dict[str, object] = {
        "schema_version": SESSION_SCHEMA_VERSION,
        "session_id": session_id,
        "seq": seq,
        "event_id": event_id,
        "recorded_at": recorded_at,
        "type": event_type,
        "prev_hash": prev_hash,
        "payload": dict(payload),
    }
    data["event_hash"] = calculate_event_hash(data)
    return session_event_from_dict(data)


def encode_event(event: SessionEvent) -> bytes:
    """Encode one event without a trailing JSONL newline."""

    return canonical_json_bytes(session_event_to_dict(event))


def decode_event(
    raw: bytes,
    *,
    source: str = "<memory>",
    line_number: int = 1,
) -> SessionEvent:
    if not isinstance(raw, bytes):
        raise TypeError("raw event data must be bytes.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SessionCodecError(
            f"event is not valid UTF-8 at byte {exc.start}",
            source=source,
            line_number=line_number,
        ) from exc

    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SessionCodecError(
            f"invalid JSON: {exc.msg}",
            source=source,
            line_number=line_number + exc.lineno - 1,
        ) from exc
    if not isinstance(decoded, dict):
        raise SessionCodecError(
            "event must be a JSON object",
            source=source,
            line_number=line_number,
        )

    version = decoded.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise SessionCodecError(
            "schema_version must be an integer",
            source=source,
            line_number=line_number,
        )
    if version != SESSION_SCHEMA_VERSION:
        try:
            decoded = migrate_event(decoded, version)
        except SessionCodecError as exc:
            raise SessionCodecError(
                exc.message,
                source=source,
                line_number=line_number,
            ) from exc

    try:
        event = session_event_from_dict(decoded)
    except (TypeError, ValueError) as exc:
        raise SessionCodecError(
            f"invalid session event: {exc}",
            source=source,
            line_number=line_number,
        ) from exc

    expected_hash = calculate_event_hash(event)
    if event.event_hash != expected_hash:
        raise SessionCodecError(
            "event_hash does not match canonical event content",
            source=source,
            line_number=line_number,
        )
    return event


def migrate_event(
    data: dict[str, object],
    from_version: int,
) -> dict[str, object]:
    del data
    raise SessionCodecError(
        f"unsupported session schema version {from_version}; no migration is available"
    )


def verify_event_chain(
    events: Sequence[SessionEvent],
    *,
    source: str = "<memory>",
) -> None:
    previous_hash: str | None = None
    session_id: str | None = None
    for index, event in enumerate(events, start=1):
        if event.seq != index:
            raise SessionCodecError(
                f"expected seq {index}, found {event.seq}",
                source=source,
                line_number=index,
            )
        if session_id is None:
            session_id = event.session_id
        elif event.session_id != session_id:
            raise SessionCodecError(
                "event belongs to a different session",
                source=source,
                line_number=index,
            )
        if event.prev_hash != previous_hash:
            raise SessionCodecError(
                "prev_hash does not match the previous event_hash",
                source=source,
                line_number=index,
            )
        expected_hash = calculate_event_hash(event)
        if event.event_hash != expected_hash:
            raise SessionCodecError(
                "event_hash does not match canonical event content",
                source=source,
                line_number=index,
            )
        previous_hash = event.event_hash


def artifact_ref_to_dict(value: ArtifactRef) -> dict[str, object]:
    return {
        "path": value.path,
        "sha256": value.sha256,
        "byte_count": value.byte_count,
        "media_type": value.media_type,
        "encoding": value.encoding,
    }


def artifact_ref_from_dict(data: Mapping[str, object]) -> ArtifactRef:
    obj = _strict_object(
        data,
        required={"path", "sha256", "byte_count", "media_type", "encoding"},
        label="ArtifactRef",
    )
    return ArtifactRef(
        path=_string(obj, "path"),
        sha256=_string(obj, "sha256"),
        byte_count=_integer(obj, "byte_count"),
        media_type=_string(obj, "media_type"),
        encoding=_optional_string(obj, "encoding"),
    )


def workspace_guard_to_dict(value: WorkspaceGuard) -> dict[str, object]:
    return {
        "workspace": value.workspace,
        "git_head": value.git_head,
        "touched_file_hashes": _thaw_json(value.touched_file_hashes),
    }


def workspace_guard_from_dict(data: Mapping[str, object]) -> WorkspaceGuard:
    obj = _strict_object(
        data,
        required={"workspace", "git_head", "touched_file_hashes"},
        label="WorkspaceGuard",
    )
    return WorkspaceGuard(
        workspace=_string(obj, "workspace"),
        git_head=_optional_string(obj, "git_head"),
        touched_file_hashes=_json_object(obj, "touched_file_hashes"),
    )


def session_started_to_dict(value: SessionStarted) -> dict[str, object]:
    return {
        "task": value.task,
        "workspace": value.workspace,
        "config": _thaw_json(value.config),
        "git_head": value.git_head,
        "workspace_guard": workspace_guard_to_dict(value.workspace_guard),
    }


def session_started_from_dict(data: Mapping[str, object]) -> SessionStarted:
    obj = _strict_object(
        data,
        required={"task", "workspace", "config", "git_head", "workspace_guard"},
        label="SessionStarted",
    )
    return SessionStarted(
        task=_string(obj, "task"),
        workspace=_string(obj, "workspace"),
        config=_json_object(obj, "config"),
        git_head=_optional_string(obj, "git_head"),
        workspace_guard=workspace_guard_from_dict(
            _mapping(obj, "workspace_guard")
        ),
    )


def model_function_call_to_dict(value: ModelFunctionCall) -> dict[str, object]:
    return {
        "call_id": value.call_id,
        "name": value.name,
        "arguments": value.arguments,
    }


def model_function_call_from_dict(data: Mapping[str, object]) -> ModelFunctionCall:
    obj = _strict_object(
        data,
        required={"call_id", "name", "arguments"},
        label="ModelFunctionCall",
    )
    return ModelFunctionCall(
        call_id=_string(obj, "call_id"),
        name=_string(obj, "name"),
        arguments=_string(obj, "arguments", allow_empty=True),
    )


def normalized_model_response_to_dict(
    value: NormalizedModelResponse,
) -> dict[str, object]:
    return {
        "response_id": value.response_id,
        "text": value.text,
        "reasoning_summary": value.reasoning_summary,
        "function_calls": [
            model_function_call_to_dict(call) for call in value.function_calls
        ],
    }


def normalized_model_response_from_dict(
    data: Mapping[str, object],
) -> NormalizedModelResponse:
    obj = _strict_object(
        data,
        required={"response_id", "text", "reasoning_summary", "function_calls"},
        label="NormalizedModelResponse",
    )
    return NormalizedModelResponse(
        response_id=_string(obj, "response_id"),
        text=_string(obj, "text", allow_empty=True),
        reasoning_summary=_string(obj, "reasoning_summary", allow_empty=True),
        function_calls=tuple(
            model_function_call_from_dict(_require_mapping(item, "function call"))
            for item in _list(obj, "function_calls")
        ),
    )


def pending_tool_call_to_dict(value: PendingToolCall) -> dict[str, object]:
    return {
        "call_id": value.call_id,
        "name": value.name,
        "arguments": value.arguments,
        "effect": value.effect,
        "started": value.started,
    }


def pending_tool_call_from_dict(data: Mapping[str, object]) -> PendingToolCall:
    obj = _strict_object(
        data,
        required={"call_id", "name", "arguments", "effect", "started"},
        label="PendingToolCall",
    )
    return PendingToolCall(
        call_id=_string(obj, "call_id"),
        name=_string(obj, "name"),
        arguments=_string(obj, "arguments", allow_empty=True),
        effect=cast(ToolEffect, _string(obj, "effect")),
        started=_boolean(obj, "started"),
    )


def checkpoint_to_dict(value: AgentSessionCheckpoint) -> dict[str, object]:
    return {
        "phase": value.phase,
        "turn_index": value.turn_index,
        "previous_response_id": value.previous_response_id,
        "pending_tool_calls": [
            pending_tool_call_to_dict(call) for call in value.pending_tool_calls
        ],
        "pending_tool_outputs": [
            _thaw_json(output) for output in value.pending_tool_outputs
        ],
        "completed_call_ids": sorted(value.completed_call_ids),
        "verification_state": _thaw_json(value.verification_state),
        "touched_file_hashes": _thaw_json(value.touched_file_hashes),
    }


def checkpoint_from_dict(data: Mapping[str, object]) -> AgentSessionCheckpoint:
    obj = _strict_object(
        data,
        required={
            "phase",
            "turn_index",
            "previous_response_id",
            "pending_tool_calls",
            "pending_tool_outputs",
            "completed_call_ids",
            "verification_state",
            "touched_file_hashes",
        },
        label="AgentSessionCheckpoint",
    )
    return AgentSessionCheckpoint(
        phase=cast(SessionPhase, _string(obj, "phase")),
        turn_index=_integer(obj, "turn_index"),
        previous_response_id=_optional_string(obj, "previous_response_id"),
        pending_tool_calls=tuple(
            pending_tool_call_from_dict(_require_mapping(item, "pending tool call"))
            for item in _list(obj, "pending_tool_calls")
        ),
        pending_tool_outputs=tuple(
            _require_mapping(item, "pending tool output")
            for item in _list(obj, "pending_tool_outputs")
        ),
        completed_call_ids=frozenset(
            _require_string(item, "completed call ID")
            for item in _list(obj, "completed_call_ids")
        ),
        verification_state=_json_object(obj, "verification_state"),
        touched_file_hashes=_json_object(obj, "touched_file_hashes"),
    )


def approval_request_to_dict(value: ApprovalRequest) -> dict[str, object]:
    return {
        "call_id": value.call_id,
        "action": value.action,
        "summary": value.summary,
        "arguments_sha256": value.arguments_sha256,
        "details": _thaw_json(value.details),
    }


def approval_request_from_dict(data: Mapping[str, object]) -> ApprovalRequest:
    obj = _strict_object(
        data,
        required={
            "call_id",
            "action",
            "summary",
            "arguments_sha256",
            "details",
        },
        label="ApprovalRequest",
    )
    return ApprovalRequest(
        call_id=_string(obj, "call_id"),
        action=_string(obj, "action"),
        summary=_string(obj, "summary"),
        arguments_sha256=_string(obj, "arguments_sha256"),
        details=_json_object(obj, "details"),
    )


def approval_decision_to_dict(value: ApprovalDecision) -> dict[str, object]:
    return {
        "approval_id": value.approval_id,
        "call_id": value.call_id,
        "action": value.action,
        "summary": value.summary,
        "outcome": value.outcome,
        "source": value.source,
        "decided_at": value.decided_at,
        "arguments_sha256": value.arguments_sha256,
    }


def approval_decision_from_dict(data: Mapping[str, object]) -> ApprovalDecision:
    obj = _strict_object(
        data,
        required={
            "approval_id",
            "call_id",
            "action",
            "summary",
            "outcome",
            "source",
            "decided_at",
            "arguments_sha256",
        },
        label="ApprovalDecision",
    )
    return ApprovalDecision(
        approval_id=_string(obj, "approval_id"),
        call_id=_string(obj, "call_id"),
        action=_string(obj, "action"),
        summary=_string(obj, "summary"),
        outcome=cast(Any, _string(obj, "outcome")),
        source=cast(Any, _string(obj, "source")),
        decided_at=_string(obj, "decided_at"),
        arguments_sha256=_string(obj, "arguments_sha256"),
    )


def session_event_to_dict(
    value: SessionEvent | Mapping[str, object],
) -> dict[str, object]:
    if isinstance(value, SessionEvent):
        return {
            "schema_version": value.schema_version,
            "session_id": value.session_id,
            "seq": value.seq,
            "event_id": value.event_id,
            "recorded_at": value.recorded_at,
            "type": value.type,
            "prev_hash": value.prev_hash,
            "payload": _thaw_json(value.payload),
            "event_hash": value.event_hash,
        }
    return dict(value)


def session_event_from_dict(data: Mapping[str, object]) -> SessionEvent:
    obj = _strict_object(
        data,
        required={
            "schema_version",
            "session_id",
            "seq",
            "event_id",
            "recorded_at",
            "type",
            "prev_hash",
            "payload",
            "event_hash",
        },
        label="SessionEvent",
    )
    return SessionEvent(
        schema_version=_integer(obj, "schema_version"),
        session_id=_string(obj, "session_id"),
        seq=_integer(obj, "seq"),
        event_id=_string(obj, "event_id"),
        recorded_at=_string(obj, "recorded_at"),
        type=cast(SessionEventType, _string(obj, "type")),
        prev_hash=_optional_string(obj, "prev_hash"),
        payload=_json_object(obj, "payload"),
        event_hash=_string(obj, "event_hash"),
    )


def verification_command_to_dict(value: VerificationCommand) -> dict[str, object]:
    return {
        "id": value.id,
        "kind": value.kind,
        "argv": list(value.argv),
        "cwd": value.cwd,
        "source": value.source,
        "available": value.available,
        "unavailable_reason": value.unavailable_reason,
        "reason": value.reason,
    }


def verification_command_from_dict(
    data: Mapping[str, object],
) -> VerificationCommand:
    obj = _strict_object(
        data,
        required={
            "id",
            "kind",
            "argv",
            "cwd",
            "source",
            "available",
            "unavailable_reason",
            "reason",
        },
        label="VerificationCommand",
    )
    return VerificationCommand(
        id=_string(obj, "id"),
        kind=cast(Any, _string(obj, "kind")),
        argv=tuple(
            _require_string(item, "argv item") for item in _list(obj, "argv")
        ),
        cwd=_string(obj, "cwd"),
        source=_string(obj, "source"),
        available=_boolean(obj, "available"),
        unavailable_reason=_optional_string(obj, "unavailable_reason"),
        reason=_optional_string(obj, "reason"),
    )


def verification_discovery_to_dict(
    value: VerificationDiscoveryResult,
) -> dict[str, object]:
    return {
        "workspace": value.workspace,
        "commands": [
            verification_command_to_dict(command) for command in value.commands
        ],
        "warnings": list(value.warnings),
        "errors": list(value.errors),
    }


def verification_discovery_from_dict(
    data: Mapping[str, object],
) -> VerificationDiscoveryResult:
    obj = _strict_object(
        data,
        required={"workspace", "commands", "warnings", "errors"},
        label="VerificationDiscoveryResult",
    )
    return VerificationDiscoveryResult(
        workspace=_string(obj, "workspace"),
        commands=tuple(
            verification_command_from_dict(
                _require_mapping(item, "verification command")
            )
            for item in _list(obj, "commands")
        ),
        warnings=tuple(
            _require_string(item, "warning") for item in _list(obj, "warnings")
        ),
        errors=tuple(
            _require_string(item, "error") for item in _list(obj, "errors")
        ),
    )


def verification_result_to_dict(value: VerificationResult) -> dict[str, object]:
    return {
        "command_id": value.command_id,
        "kind": value.kind,
        "status": value.status,
        "argv": list(value.argv),
        "cwd": value.cwd,
        "exit_code": value.exit_code,
        "duration_ms": value.duration_ms,
        "output": value.output,
        "truncated": value.truncated,
        "omitted_lines": value.omitted_lines,
        "omitted_bytes": value.omitted_bytes,
        "attempt": value.attempt,
    }


def verification_result_from_dict(
    data: Mapping[str, object],
) -> VerificationResult:
    obj = _strict_object(
        data,
        required={
            "command_id",
            "kind",
            "status",
            "argv",
            "cwd",
            "exit_code",
            "duration_ms",
            "output",
            "truncated",
            "omitted_lines",
            "omitted_bytes",
            "attempt",
        },
        label="VerificationResult",
    )
    exit_code = obj["exit_code"]
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int)
    ):
        raise TypeError("VerificationResult.exit_code must be an integer or null.")
    return VerificationResult(
        command_id=_string(obj, "command_id"),
        kind=cast(Any, _string(obj, "kind")),
        status=cast(Any, _string(obj, "status")),
        argv=tuple(
            _require_string(item, "argv item") for item in _list(obj, "argv")
        ),
        cwd=_string(obj, "cwd"),
        exit_code=exit_code,
        duration_ms=_integer(obj, "duration_ms"),
        output=_string(obj, "output", allow_empty=True),
        truncated=_boolean(obj, "truncated"),
        omitted_lines=_integer(obj, "omitted_lines"),
        omitted_bytes=_integer(obj, "omitted_bytes"),
        attempt=_integer(obj, "attempt"),
    )


def verification_tool_state_to_dict(
    value: VerificationToolState,
) -> dict[str, object]:
    return {
        "task": value.task,
        "max_fix_attempts": value.max_fix_attempts,
        "discovery": (
            verification_discovery_to_dict(value.discovery)
            if value.discovery is not None
            else None
        ),
        "verification_history": [
            verification_result_to_dict(result)
            for result in value.verification_history
        ],
        "unresolved_failure_command_id": value.unresolved_failure_command_id,
        "repair_attempts": value.repair_attempts,
        "after_edit": value.after_edit,
        "edit_generation": value.edit_generation,
        "passed_generations": dict(sorted(value.passed_generations.items())),
    }


def verification_tool_state_from_dict(
    data: Mapping[str, object],
) -> VerificationToolState:
    obj = _strict_object(
        data,
        required={
            "task",
            "max_fix_attempts",
            "discovery",
            "verification_history",
            "unresolved_failure_command_id",
            "repair_attempts",
            "after_edit",
            "edit_generation",
            "passed_generations",
        },
        label="VerificationToolState",
    )
    discovery_data = obj["discovery"]
    discovery = None
    if discovery_data is not None:
        discovery = verification_discovery_from_dict(
            _require_mapping(discovery_data, "discovery")
        )
    history = [
        verification_result_from_dict(
            _require_mapping(item, "verification history item")
        )
        for item in _list(obj, "verification_history")
    ]
    unresolved = _optional_string(obj, "unresolved_failure_command_id")
    repair_attempts = _integer(obj, "repair_attempts")
    edit_generation = _integer(obj, "edit_generation")
    passed_data = _mapping(obj, "passed_generations")
    passed_generations: dict[str, int] = {}
    for command_id, generation in passed_data.items():
        if not isinstance(command_id, str) or not command_id:
            raise ValueError("passed_generations keys must be non-empty strings.")
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise TypeError("passed_generations values must be integers.")
        if generation < 0 or generation > edit_generation:
            raise ValueError(
                "passed generation must be between zero and edit_generation."
            )
        passed_generations[command_id] = generation

    state = VerificationToolState(
        task=_string(obj, "task", allow_empty=True),
        max_fix_attempts=_integer(obj, "max_fix_attempts"),
        discovery=discovery,
        verification_history=history,
        unresolved_failure_command_id=unresolved,
        repair_attempts=repair_attempts,
        after_edit=_boolean(obj, "after_edit"),
        edit_generation=edit_generation,
        passed_generations=passed_generations,
    )
    _validate_verification_tool_state(state)
    return state


def _validate_verification_tool_state(state: VerificationToolState) -> None:
    if state.repair_attempts < 0 or state.repair_attempts > state.max_fix_attempts:
        raise ValueError(
            "repair_attempts must be between zero and max_fix_attempts."
        )
    if state.edit_generation < 0:
        raise ValueError("edit_generation must be non-negative.")
    if state.unresolved_failure_command_id is None and state.repair_attempts != 0:
        raise ValueError(
            "repair_attempts must be zero when no failure is unresolved."
        )
    if state.unresolved_failure_command_id is not None:
        matching = [
            result
            for result in state.verification_history
            if result.command_id == state.unresolved_failure_command_id
        ]
        if not matching or matching[-1].status != "failed":
            raise ValueError(
                "unresolved failure must reference a latest failed verification."
            )


def _strict_object(
    data: Mapping[str, object],
    *,
    required: set[str],
    label: str,
) -> dict[str, object]:
    obj = dict(_require_mapping(data, label))
    missing = sorted(required - set(obj))
    unknown = sorted(set(obj) - required)
    if missing:
        raise ValueError(f"{label} is missing fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {', '.join(unknown)}")
    return obj


def _mapping(data: Mapping[str, object], key: str) -> Mapping[str, object]:
    return _require_mapping(data[key], key)


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object.")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{label} keys must be strings.")
    return cast(Mapping[str, object], value)


def _list(data: Mapping[str, object], key: str) -> list[object]:
    value = data[key]
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{key} must be a JSON array.")
    return list(value)


def _string(
    data: Mapping[str, object],
    key: str,
    *,
    allow_empty: bool = False,
) -> str:
    return _require_string(data[key], key, allow_empty=allow_empty)


def _require_string(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string.")
    if not allow_empty and not value:
        raise ValueError(f"{label} must be non-empty.")
    return value


def _optional_string(data: Mapping[str, object], key: str) -> str | None:
    value = data[key]
    if value is None:
        return None
    return _require_string(value, key)


def _integer(data: Mapping[str, object], key: str) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} must be an integer.")
    return value


def _boolean(data: Mapping[str, object], key: str) -> bool:
    value = data[key]
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a boolean.")
    return value


def _json_object(data: Mapping[str, object], key: str) -> JsonObject:
    return cast(JsonObject, _require_mapping(data[key], key))


def _thaw_json(value: JsonValue | JsonObject) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value



from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from typing import cast

from ..approvals import APPROVAL_OUTCOMES, validate_approval_decision
from ..plans import (
    EMPTY_PLAN,
    PlanState,
    plan_state_from_dict,
    plan_state_to_dict,
)
from ..reviews import ReviewResult, review_result_to_dict
from ..security.path_policy import (
    SENSITIVE_PATH_DENIAL_REASON,
    load_sensitive_path_policy,
)
from .codec import (
    approval_decision_from_dict,
    approval_request_from_dict,
    artifact_ref_from_dict,
    artifact_ref_to_dict,
)
from .models import ArtifactRef, JsonValue, SessionEvent
from .reducer import rebuild_state
from .store import ArtifactNotFoundError, SessionStore

REPLAY_SCHEMA_VERSION = 2
APPROVAL_QUERY_SCHEMA_VERSION = 1

_TERMINAL_EVENT_STATUSES = {
    "session.completed": "completed",
    "session.failed": "failed",
    "session.interrupted": "interrupted",
}
_ARTIFACT_REF_FIELDS = {
    "path",
    "sha256",
    "byte_count",
    "media_type",
    "encoding",
}


def build_session_replay_payload(
    store: SessionStore,
    session_id: str,
    *,
    verbose: bool = False,
) -> dict[str, object]:
    """Build an offline replay projection from one verified session log."""

    _require_read_only(store)
    if not isinstance(verbose, bool):
        raise TypeError("verbose must be a boolean.")

    events = store.load(session_id)
    first = events[0]
    last = events[-1]
    started = _started_payload(first)
    plan = _replay_plan(events)
    review = _replay_review(events)
    approvals = rebuild_approval_projection(events)
    approvals_by_call_id = _approvals_by_call_id(approvals)
    tool_names = _tool_names_by_call_id(events)
    artifact_cache: dict[tuple[str, str, str], dict[str, object]] = {}
    timeline = [
        _timeline_item(
            event,
            approvals_by_call_id=approvals_by_call_id,
            tool_names=tool_names,
            store=store,
            verbose=verbose,
            artifact_cache=artifact_cache,
        )
        for event in events
    ]

    task_value = started.get("task")
    workspace_value = started.get("workspace")
    return {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "kind": "session_replay",
        "workspace": str(store.workspace),
        "session": {
            "session_id": session_id,
            "task": task_value if isinstance(task_value, str) else None,
            "session_workspace": (
                workspace_value if isinstance(workspace_value, str) else None
            ),
            "status": _TERMINAL_EVENT_STATUSES.get(last.type, "running"),
            "final_status": _final_status(events),
            "event_count": len(events),
            "started_at": first.recorded_at,
            "updated_at": last.recorded_at,
            "last_event_type": last.type,
        },
        "plan": plan_state_to_dict(plan),
        "plan_updates": _plan_update_projection(events),
        "review": (
            review_result_to_dict(review) if review is not None else None
        ),
        "terminal": _terminal_projection(events),
        "verbose": verbose,
        "timeline": timeline,
        "verifications": _verification_projection(events),
        "approvals": list(approvals),
    }


def _plan_update_projection(
    events: Sequence[SessionEvent],
) -> list[dict[str, object]]:
    updates: list[dict[str, object]] = []
    for event in events:
        if event.type != "plan.updated":
            continue
        plan_data = _required_mapping(event.payload, "plan")
        plan = plan_state_from_dict(plan_data, allow_empty=False)
        updates.append(
            {
                "seq": event.seq,
                "recorded_at": event.recorded_at,
                "plan": plan_state_to_dict(plan),
            }
        )
    return updates


def _terminal_projection(
    events: Sequence[SessionEvent],
) -> dict[str, object] | None:
    last = events[-1]
    status = _TERMINAL_EVENT_STATUSES.get(last.type)
    if status is None:
        return None
    terminal: dict[str, object] = {
        "status": status,
        "event_type": last.type,
        "seq": last.seq,
        "recorded_at": last.recorded_at,
    }
    reason = _string_value(last.payload.get("reason"))
    if reason is not None:
        terminal["reason"] = reason
    final_status = _report_value(last.payload, "final_status")
    if final_status is not None:
        terminal["final_status"] = final_status
    return terminal


def _replay_plan(events: tuple[SessionEvent, ...]) -> PlanState:
    """Read plans from reducer state while preserving plan-less legacy logs."""

    if not any(event.type == "plan.updated" for event in events):
        return EMPTY_PLAN
    return rebuild_state(events).plan


def _replay_review(events: tuple[SessionEvent, ...]) -> ReviewResult | None:
    if not any(
        event.type == "tool.finished"
        and isinstance(event.payload.get("review"), Mapping)
        for event in events
    ):
        return None
    return rebuild_state(events).review


def rebuild_approval_projection(
    events: Sequence[SessionEvent],
) -> tuple[dict[str, object], ...]:
    """Rebuild the approval projection from session events, the sole truth source."""

    records: list[dict[str, object]] = []
    seen_approval_ids: set[str] = set()
    for event in events:
        if event.type != "approval.decided":
            continue
        request_data = _required_mapping(event.payload, "request")
        decision_data = _required_mapping(event.payload, "decision")
        request = approval_request_from_dict(request_data)
        decision = approval_decision_from_dict(decision_data)
        validate_approval_decision(request, decision)
        if decision.approval_id in seen_approval_ids:
            raise ValueError(
                f"Duplicate approval ID in session events: {decision.approval_id}"
            )
        seen_approval_ids.add(decision.approval_id)
        records.append(
            {
                "session_id": event.session_id,
                "seq": event.seq,
                "recorded_at": event.recorded_at,
                "approval_id": decision.approval_id,
                "call_id": decision.call_id,
                "action": decision.action,
                "summary": decision.summary,
                "outcome": decision.outcome,
                "source": decision.source,
                "decided_at": decision.decided_at,
                "arguments_sha256": decision.arguments_sha256,
                "handler_error": "handler_error" in event.payload,
            }
        )
    return tuple(records)


def build_approval_query_payload(
    store: SessionStore,
    *,
    session_ids: Sequence[str],
    selector: str,
    action: str | None = None,
    outcome: str | None = None,
) -> dict[str, object]:
    """Query approvals by session, action, and outcome without using projections."""

    _require_read_only(store)
    selector = _trimmed_string(selector, "selector")
    if action is not None:
        action = _trimmed_string(action, "action")
    if outcome is not None and outcome not in APPROVAL_OUTCOMES:
        raise ValueError(f"Unsupported approval outcome: {outcome}")

    unique_session_ids: list[str] = []
    seen: set[str] = set()
    for session_id in session_ids:
        normalized = _trimmed_string(session_id, "session_id")
        if normalized not in seen:
            seen.add(normalized)
            unique_session_ids.append(normalized)

    approvals: list[dict[str, object]] = []
    for session_id in unique_session_ids:
        approvals.extend(rebuild_approval_projection(store.load(session_id)))
    if action is not None:
        approvals = [item for item in approvals if item["action"] == action]
    if outcome is not None:
        approvals = [item for item in approvals if item["outcome"] == outcome]
    approvals.sort(
        key=lambda item: (
            cast(str, item["recorded_at"]),
            cast(str, item["session_id"]),
            cast(int, item["seq"]),
            cast(str, item["approval_id"]),
        )
    )

    return {
        "schema_version": APPROVAL_QUERY_SCHEMA_VERSION,
        "kind": "approval_list",
        "workspace": str(store.workspace),
        "filters": {
            "session": selector,
            "action": action,
            "outcome": outcome,
        },
        "session_count": len(unique_session_ids),
        "approvals": approvals,
    }


def _timeline_item(
    event: SessionEvent,
    *,
    approvals_by_call_id: Mapping[str, tuple[dict[str, object], ...]],
    tool_names: Mapping[str, str],
    store: SessionStore,
    verbose: bool,
    artifact_cache: dict[tuple[str, str, str], dict[str, object]],
) -> dict[str, object]:
    summary, details = _event_summary(
        event,
        approvals_by_call_id=approvals_by_call_id,
        tool_names=tool_names,
    )
    item: dict[str, object] = {
        "seq": event.seq,
        "recorded_at": event.recorded_at,
        "type": event.type,
        "summary": summary,
    }
    if details:
        item["details"] = details
    if verbose:
        item["payload"] = _expand_artifacts(
            event.payload,
            store=store,
            session_id=event.session_id,
            cache=artifact_cache,
        )
    return item


def _event_summary(
    event: SessionEvent,
    *,
    approvals_by_call_id: Mapping[str, tuple[dict[str, object], ...]],
    tool_names: Mapping[str, str],
) -> tuple[str, dict[str, object]]:
    payload = event.payload
    if event.type == "session.started":
        return "session started", {}
    if event.type == "session.resumed":
        reason = _string_value(payload.get("reason"))
        return _with_suffix("session resumed", reason), _details(reason=reason)
    if event.type == "session.completed":
        final_status = _report_value(payload, "final_status")
        return _with_suffix("session completed", _arrow_value("final_status", final_status)), _details(final_status=final_status)
    if event.type in {"session.failed", "session.interrupted"}:
        label = "session failed" if event.type == "session.failed" else "session interrupted"
        reason = _string_value(payload.get("reason"))
        return _with_suffix(label, reason), _details(reason=reason)
    if event.type == "context.created":
        total = _int_value(payload.get("total_file_count"))
        sampled = _sequence_length(payload.get("samples"))
        details = _details(total_file_count=total, sampled_file_count=sampled)
        if total is not None and sampled is not None:
            return f"context created ({total} files, {sampled} sampled)", details
        return "context created", details
    if event.type == "model.requested":
        kind = _string_value(payload.get("request_kind"))
        turn = _int_value(payload.get("turn_index"))
        qualifiers = ", ".join(
            value for value in (kind, f"turn {turn}" if turn is not None else None) if value
        )
        summary = f"model requested ({qualifiers})" if qualifiers else "model requested"
        return summary, _details(request_kind=kind, turn_index=turn)
    if event.type == "model.responded":
        response = _optional_mapping(payload.get("response")) or payload
        response_id = _string_value(response.get("response_id"))
        tool_count = _sequence_length(response.get("function_calls"))
        summary = "model responded"
        if response_id:
            summary += f" {response_id}"
        if tool_count:
            summary += f" ({tool_count} tool calls)"
        return summary, _details(response_id=response_id, tool_call_count=tool_count)
    if event.type == "tool.started":
        call_id = _string_value(payload.get("call_id"))
        name = _tool_name(payload, call_id, tool_names)
        return f"tool {name} started", _details(call_id=call_id, name=name)
    if event.type == "approval.decided":
        decision = _optional_mapping(payload.get("decision")) or payload
        action = _string_value(decision.get("action")) or "unknown"
        outcome = _string_value(decision.get("outcome")) or "unknown"
        source = _string_value(decision.get("source")) or "unknown"
        return (
            f"approval {action} -> {outcome}({source})",
            _details(
                call_id=_string_value(decision.get("call_id")),
                action=action,
                outcome=outcome,
                source=source,
            ),
        )
    if event.type in {"tool.finished", "tool.recovered"}:
        call_id = _string_value(payload.get("call_id"))
        name = _tool_name(payload, call_id, tool_names)
        status = _tool_status(payload, recovered=event.type == "tool.recovered")
        approval = _last_approval(approvals_by_call_id, call_id)
        summary = f"tool {name}"
        if approval is not None:
            summary += (
                f" -> {approval['outcome']}({approval['source']})"
            )
        summary += f" -> {status}"
        return summary, _details(call_id=call_id, name=name, status=status)
    if event.type == "plan.updated":
        plan_data = _required_mapping(payload, "plan")
        plan = plan_state_from_dict(plan_data, allow_empty=False)
        completed = sum(item.status == "completed" for item in plan.items)
        in_progress = sum(item.status == "in_progress" for item in plan.items)
        pending = sum(item.status == "pending" for item in plan.items)
        return (
            f"plan updated ({len(plan.items)} items, {completed} completed)",
            {
                "item_count": len(plan.items),
                "completed_count": completed,
                "in_progress_count": in_progress,
                "pending_count": pending,
            },
        )
    if event.type == "verification.recorded":
        result = _optional_mapping(payload.get("result")) or payload
        command_id = _string_value(result.get("command_id")) or "unknown"
        status = _string_value(result.get("status")) or "unknown"
        attempt = _int_value(result.get("attempt"))
        summary = f"verification {command_id} -> {status}"
        if attempt is not None:
            summary += f" (attempt {attempt})"
        return summary, _details(command_id=command_id, status=status, attempt=attempt)
    if event.type == "security.policy_evaluated":
        policy = _optional_mapping(payload.get("policy")) or {}
        rule_id = _string_value(policy.get("rule_id")) or "unknown"
        disposition = _string_value(policy.get("disposition")) or "unknown"
        return (
            f"policy {rule_id} -> {disposition}",
            _details(rule_id=rule_id, disposition=disposition),
        )
    if event.type == "sandbox.capability_checked":
        capability = _optional_mapping(payload.get("capability")) or {}
        available = capability.get("available")
        digest = _string_value(capability.get("image_digest"))
        status = "available" if available is True else "unavailable"
        return (
            f"sandbox capability -> {status}",
            _details(status=status, image_digest=digest),
        )
    if event.type == "sandbox.snapshot_created":
        snapshot = _optional_mapping(payload.get("snapshot")) or {}
        files = _int_value(snapshot.get("file_count"))
        total_bytes = _int_value(snapshot.get("total_bytes"))
        return (
            f"sandbox snapshot created ({files or 0} files)",
            _details(file_count=files, total_bytes=total_bytes),
        )
    if event.type == "sandbox.started":
        container = _string_value(payload.get("container_name"))
        return "sandbox started", _details(container_name=container)
    if event.type == "sandbox.finished":
        result = _optional_mapping(payload.get("result")) or {}
        status = _string_value(result.get("status")) or "unknown"
        return f"sandbox finished -> {status}", _details(status=status)
    if event.type == "sandbox.cleanup_failed":
        kind = _string_value(payload.get("cleanup_kind")) or "unknown"
        reason = _string_value(payload.get("reason"))
        return (
            f"sandbox {kind} cleanup failed",
            _details(cleanup_kind=kind, reason=reason),
        )
    if event.type == "checkpoint.saved":
        return "checkpoint saved", {}
    return event.type, {}


def _verification_projection(
    events: Sequence[SessionEvent],
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    fields = (
        "command_id",
        "kind",
        "status",
        "argv",
        "cwd",
        "exit_code",
        "duration_ms",
        "truncated",
        "omitted_lines",
        "omitted_bytes",
        "attempt",
    )
    for event in events:
        if event.type != "verification.recorded":
            continue
        result = _optional_mapping(event.payload.get("result")) or event.payload
        item: dict[str, object] = {
            "seq": event.seq,
            "recorded_at": event.recorded_at,
        }
        for field in fields:
            value = result.get(field)
            if isinstance(value, tuple):
                item[field] = list(value)
            elif isinstance(value, (str, int, bool)) or value is None:
                if field in result:
                    item[field] = value
        results.append(item)
    return results


def _approvals_by_call_id(
    approvals: Sequence[dict[str, object]],
) -> dict[str, tuple[dict[str, object], ...]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for approval in approvals:
        call_id = cast(str, approval["call_id"])
        grouped.setdefault(call_id, []).append(approval)
    return {key: tuple(value) for key, value in grouped.items()}


def _tool_names_by_call_id(events: Sequence[SessionEvent]) -> dict[str, str]:
    names: dict[str, str] = {}
    for event in events:
        if event.type not in {"tool.started", "tool.finished", "tool.recovered"}:
            continue
        call_id = _string_value(event.payload.get("call_id"))
        name = _string_value(event.payload.get("name"))
        if call_id and name:
            names[call_id] = name
    return names


def _tool_name(
    payload: Mapping[str, JsonValue],
    call_id: str | None,
    tool_names: Mapping[str, str],
) -> str:
    return (
        _string_value(payload.get("name"))
        or (tool_names.get(call_id) if call_id is not None else None)
        or "unknown"
    )


def _tool_status(payload: Mapping[str, JsonValue], *, recovered: bool) -> str:
    if recovered:
        completed = payload.get("completed")
        if completed is False:
            if payload.get("requires_reapproval") is True:
                return "pending reapproval"
            return "scheduled for retry"
    tool_output = _optional_mapping(payload.get("tool_output"))
    if tool_output is not None:
        output = tool_output.get("output")
        if isinstance(output, str):
            try:
                decoded = json.loads(output)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                ok = decoded.get("ok")
                if ok is True:
                    return "ok"
                if ok is False:
                    return "failed"
    ok = payload.get("ok")
    if ok is True:
        return "ok"
    if ok is False:
        return "failed"
    return "recovered" if recovered else "unknown"


def _last_approval(
    approvals_by_call_id: Mapping[str, tuple[dict[str, object], ...]],
    call_id: str | None,
) -> dict[str, object] | None:
    if call_id is None:
        return None
    matches = approvals_by_call_id.get(call_id, ())
    return matches[-1] if matches else None


def _final_status(events: Sequence[SessionEvent]) -> str | None:
    for event in reversed(events):
        if event.type != "session.completed":
            continue
        value = _report_value(event.payload, "final_status")
        return value
    return None


def _report_value(payload: Mapping[str, JsonValue], key: str) -> str | None:
    report = _optional_mapping(payload.get("report"))
    if report is None:
        return _string_value(payload.get(key))
    return _string_value(report.get(key))


def _expand_artifacts(
    value: object,
    *,
    store: SessionStore,
    session_id: str,
    cache: dict[tuple[str, str, str], dict[str, object]],
) -> object:
    if isinstance(value, Mapping):
        direct_ref = _artifact_ref(value)
        if direct_ref is not None:
            return {
                "artifact": artifact_ref_to_dict(direct_ref),
                "artifact_content": _artifact_content(
                    store,
                    session_id,
                    direct_ref,
                    cache,
                    source_path=None,
                ),
            }
        result = {
            str(key): _expand_artifacts(
                item,
                store=store,
                session_id=session_id,
                cache=cache,
            )
            for key, item in value.items()
        }
        nested_ref = _artifact_ref(value.get("artifact"))
        if nested_ref is not None:
            result["artifact"] = artifact_ref_to_dict(nested_ref)
            result["artifact_content"] = _artifact_content(
                store,
                session_id,
                nested_ref,
                cache,
                source_path=_artifact_source_path(value),
            )
        return result
    if isinstance(value, tuple):
        return [
            _expand_artifacts(
                item,
                store=store,
                session_id=session_id,
                cache=cache,
            )
            for item in value
        ]
    return value


def _artifact_content(
    store: SessionStore,
    session_id: str,
    ref: ArtifactRef,
    cache: dict[tuple[str, str, str], dict[str, object]],
    *,
    source_path: str | None,
) -> dict[str, object]:
    cache_key = (ref.sha256, ref.path, source_path or "")
    cached = cache.get(cache_key)
    if cached is not None:
        return dict(cached)

    if source_path is not None:
        try:
            source_decision = load_sensitive_path_policy(store.workspace).evaluate(
                source_path,
                operation="artifact_expand",
            )
        except (TypeError, ValueError):
            source_decision = None
        if source_decision is None or not source_decision.allowed:
            result = {
                "available": False,
                "reason": SENSITIVE_PATH_DENIAL_REASON,
                "media_type": ref.media_type,
                "encoding": ref.encoding,
            }
            cache[cache_key] = result
            return dict(result)

    if not store.artifacts_dir.is_dir():
        result = {
            "available": False,
            "reason": "artifact_missing",
            "media_type": ref.media_type,
            "encoding": ref.encoding,
        }
        cache[cache_key] = result
        return dict(result)

    artifact_decision = load_sensitive_path_policy(store.artifacts_dir).evaluate(
        ref.path,
        operation="artifact_expand",
    )
    if not artifact_decision.allowed:
        result = {
            "available": False,
            "reason": SENSITIVE_PATH_DENIAL_REASON,
            "media_type": ref.media_type,
            "encoding": ref.encoding,
        }
        cache[cache_key] = result
        return dict(result)

    try:
        content = store.get_artifact(session_id, ref)
    except ArtifactNotFoundError:
        result: dict[str, object] = {
            "available": False,
            "reason": "artifact_missing",
            "media_type": ref.media_type,
            "encoding": ref.encoding,
        }
        cache[cache_key] = result
        return dict(result)

    result = {
        "available": True,
        "media_type": ref.media_type,
        "encoding": ref.encoding,
    }
    if ref.encoding is not None:
        text = content.decode(ref.encoding)
        if ref.media_type == "application/json":
            try:
                result["json"] = json.loads(text)
            except json.JSONDecodeError:
                result["text"] = text
        else:
            result["text"] = text
    else:
        result["base64"] = base64.b64encode(content).decode("ascii")
    cache[cache_key] = result
    return dict(result)


def _artifact_source_path(value: Mapping[object, object]) -> str | None:
    source_path = value.get("source_path")
    return source_path if isinstance(source_path, str) else None


def _artifact_ref(value: object) -> ArtifactRef | None:
    if not isinstance(value, Mapping) or set(value) != _ARTIFACT_REF_FIELDS:
        return None
    if not all(isinstance(key, str) for key in value):
        return None
    return artifact_ref_from_dict(cast(Mapping[str, object], value))


def _started_payload(event: SessionEvent) -> Mapping[str, JsonValue]:
    nested = event.payload.get("session")
    return nested if isinstance(nested, Mapping) else event.payload


def _required_mapping(
    payload: Mapping[str, JsonValue],
    key: str,
) -> Mapping[str, object]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"event field {key!r} must be an object.")
    return cast(Mapping[str, object], value)


def _optional_mapping(value: object) -> Mapping[str, JsonValue] | None:
    if not isinstance(value, Mapping):
        return None
    return cast(Mapping[str, JsonValue], value)


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_value(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _sequence_length(value: object) -> int | None:
    if isinstance(value, tuple):
        return len(value)
    if isinstance(value, list):
        return len(value)
    return None


def _details(**values: object) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}


def _with_suffix(label: str, suffix: str | None) -> str:
    return f"{label} -> {suffix}" if suffix else label


def _arrow_value(label: str, value: str | None) -> str | None:
    return f"{label}={value}" if value else None


def _trimmed_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be a non-empty trimmed string.")
    return value


def _require_read_only(store: SessionStore) -> None:
    if not isinstance(store, SessionStore):
        raise TypeError("store must be a SessionStore.")
    if not store.read_only:
        raise ValueError("replay and approval queries require a read-only SessionStore.")

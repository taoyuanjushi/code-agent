from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from .sessions.codec import artifact_ref_to_dict
from .sessions.models import JsonObject
from .sessions.store import SessionStore
from .types import ToolResult


def build_persistable_tool_output(
    store: SessionStore,
    session_id: str,
    call_id: str,
    result: ToolResult,
) -> dict[str, Any]:
    """Build the durable model output used by normal and recovered tool calls."""

    payload: dict[str, object] = {
        "ok": result.ok,
        "output": result.output,
        "data": result.data,
    }
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) > store.privacy_policy.inline_max_bytes:
        artifact = store.put_artifact(
            session_id,
            encoded,
            "application/json",
            encoding="utf-8",
        )
        payload = {
            "ok": result.ok,
            "output": (
                "Tool result stored as a session artifact because it exceeded "
                "the inline limit."
            ),
            "data": {"artifact": artifact_ref_to_dict(artifact)},
        }

    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(payload, ensure_ascii=False),
    }


def pending_outputs_for_model(
    outputs: tuple[JsonObject, ...],
) -> list[dict[str, Any]]:
    """Thaw persisted function-call outputs without changing their JSON values."""

    result: list[dict[str, Any]] = []
    for output in outputs:
        thawed = thaw_json(output)
        if not isinstance(thawed, dict):
            raise RuntimeError("Persisted tool output is not an object.")
        call_id = thawed.get("call_id")
        output_text = thawed.get("output")
        if not isinstance(call_id, str) or not isinstance(output_text, str):
            raise RuntimeError("Persisted tool output is not model-compatible.")
        result.append(cast(dict[str, Any], thawed))
    return result


def thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value

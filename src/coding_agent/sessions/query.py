from __future__ import annotations

from .replay import build_session_replay_payload
from .store import SessionNotFoundError, SessionStore, SessionSummary

SESSION_QUERY_SCHEMA_VERSION = 1


def resolve_session_selector(store: SessionStore, selector: str) -> str:
    """Resolve an explicit session ID or the stable workspace-local latest value."""

    if not isinstance(selector, str) or not selector or selector != selector.strip():
        raise ValueError("session selector must be a non-empty trimmed string.")
    if selector != "latest":
        return selector

    summaries = store.list_sessions()
    if not summaries:
        raise SessionNotFoundError(
            f"No sessions exist in workspace: {store.workspace}"
        )
    return max(
        summaries,
        key=lambda summary: (summary.updated_at, summary.session_id),
    ).session_id


def build_session_list_payload(store: SessionStore) -> dict[str, object]:
    """Build the stable JSON-compatible payload used by the list command."""

    return {
        "schema_version": SESSION_QUERY_SCHEMA_VERSION,
        "kind": "session_list",
        "workspace": str(store.workspace),
        "sessions": [
            _summary_payload(summary) for summary in store.list_sessions()
        ],
    }


def _summary_payload(summary: SessionSummary) -> dict[str, object]:
    return {
        "session_id": summary.session_id,
        "task": summary.task,
        "status": summary.status,
        "event_count": summary.event_count,
        "started_at": summary.started_at,
        "updated_at": summary.updated_at,
        "last_event_type": summary.last_event_type,
    }


__all__ = [
    "SESSION_QUERY_SCHEMA_VERSION",
    "build_session_list_payload",
    "build_session_replay_payload",
    "resolve_session_selector",
]

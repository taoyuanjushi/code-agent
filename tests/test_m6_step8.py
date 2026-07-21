from __future__ import annotations

import io
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, cast

import pytest

from coding_agent.agent import resume_agent_with_report, run_agent_with_report
from coding_agent.plans import (
    EMPTY_PLAN,
    PLAN_MAX_EXPLANATION_CHARS,
    PLAN_MAX_ITEMS,
    PLAN_MAX_STEP_CHARS,
    PlanItem,
    PlanState,
    parse_plan_update,
    plan_state_to_dict,
)
from coding_agent.sessions.codec import (
    checkpoint_from_dict,
    checkpoint_to_dict,
    create_session_event,
    session_started_to_dict,
)
from coding_agent.sessions.models import (
    AgentSessionCheckpoint,
    SessionEvent,
    SessionEventType,
    SessionStarted,
    WorkspaceGuard,
)
from coding_agent.sessions.recovery import plan_interrupted_tools
from coding_agent.sessions.reducer import (
    SessionReductionError,
    rebuild_state,
    reduce_event,
)
from coding_agent.sessions.replay import build_session_replay_payload
from coding_agent.sessions.store import SessionStore
from coding_agent.tool_policy import get_tool_policy
from coding_agent.tools import TOOL_DEFINITIONS, execute_tool
from coding_agent.types import AgentConfig
from coding_agent.ui import JsonlRenderer, TerminalRenderer, UiEmitter, UiEvent


def _config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="read-only",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=10,
        context_max_bytes_per_file=4_000,
    )


def _plan_arguments(*, completed: bool = False) -> dict[str, object]:
    return {
        "explanation": "Keep the resumable work explicit",
        "items": [
            {
                "step": "inspect parser",
                "status": "completed" if completed else "in_progress",
            },
            {
                "step": "add regression test",
                "status": "completed" if completed else "pending",
            },
        ],
    }


class _PlanThenFinalClient:
    def __init__(self, *, completed: bool = False) -> None:
        self.completed = completed
        self.initial_count = 0
        self.continuation_count = 0
        self.received_tool_outputs: list[dict[str, Any]] | None = None

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        self.initial_count += 1
        return {
            "id": "response-plan",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-plan",
                    "name": "update_plan",
                    "arguments": json.dumps(
                        _plan_arguments(completed=self.completed)
                    ),
                }
            ],
        }

    def create_tool_response(
        self,
        *,
        tool_outputs: list[dict[str, Any]],
        **_kwargs: object,
    ) -> dict[str, object]:
        self.continuation_count += 1
        self.received_tool_outputs = tool_outputs
        return {
            "id": "response-final",
            "output": [],
            "output_text": "done",
        }


def _interrupt_at(target: str):
    def inject(point: str) -> None:
        if point == target:
            raise KeyboardInterrupt(target)

    return inject


def _started_event(workspace: Path) -> SessionEvent:
    resolved = str(workspace.resolve())
    guard = WorkspaceGuard(
        workspace=resolved,
        git_head=None,
        touched_file_hashes={},
    )
    started = SessionStarted(
        task="persist a plan",
        workspace=resolved,
        config={
            "workspace": resolved,
            "model": "fake-model",
            "max_turns": 4,
            "permission_mode": "read-only",
            "max_fix_attempts": 3,
        },
        git_head=None,
        workspace_guard=guard,
    )
    return create_session_event(
        session_id="20260720T010203Z-a1b2c3d4",
        seq=1,
        event_id="event-0001",
        recorded_at="2026-07-20T01:02:03.000Z",
        event_type="session.started",
        prev_hash=None,
        payload=session_started_to_dict(started),
    )


def _next_event(
    previous: SessionEvent,
    event_type: str,
    payload: dict[str, object],
) -> SessionEvent:
    return create_session_event(
        session_id=previous.session_id,
        seq=previous.seq + 1,
        event_id=f"event-{previous.seq + 1:04d}",
        recorded_at="2026-07-20T01:02:04.000Z",
        event_type=cast(SessionEventType, event_type),
        prev_hash=previous.event_hash,
        payload=payload,
    )


def test_plan_state_is_normalized_deeply_immutable_and_strict() -> None:
    plan = parse_plan_update(
        {
            "explanation": "  explain why  ",
            "items": [
                {"step": "  inspect parser  ", "status": "in_progress"},
                {"step": "add tests", "status": "pending"},
            ],
        }
    )

    assert plan.explanation == "explain why"
    assert plan.items[0].step == "inspect parser"
    assert isinstance(plan.items, tuple)
    with pytest.raises(FrozenInstanceError):
        plan.explanation = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.items[0].status = "completed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "arguments",
    [
        {"items": [], "extra": True},
        {"items": [{"step": "one", "status": "pending", "extra": True}]},
        {"items": []},
        {
            "items": [
                {"step": str(index), "status": "pending"}
                for index in range(PLAN_MAX_ITEMS + 1)
            ]
        },
        {"items": [{"step": "   ", "status": "pending"}]},
        {
            "items": [
                {"step": "x" * (PLAN_MAX_STEP_CHARS + 1), "status": "pending"}
            ]
        },
        {
            "explanation": "x" * (PLAN_MAX_EXPLANATION_CHARS + 1),
            "items": [{"step": "one", "status": "pending"}],
        },
        {"items": [{"step": "one", "status": "blocked"}]},
        {
            "items": [
                {"step": "one", "status": "in_progress"},
                {"step": "two", "status": "in_progress"},
            ]
        },
        {
            "items": [
                {"step": "same", "status": "pending"},
                {"step": " same ", "status": "completed"},
            ]
        },
    ],
)
def test_update_plan_rejects_budget_shape_and_semantic_violations(
    tmp_path: Path,
    arguments: dict[str, object],
) -> None:
    result = execute_tool(
        _config(tmp_path),
        "update_plan",
        json.dumps(arguments),
    )

    assert result.ok is False
    assert result.output


def test_update_plan_schema_has_fixed_budgets_and_unknown_field_rejection() -> None:
    definitions = {item["name"]: item for item in TOOL_DEFINITIONS}
    assert list(item["name"] for item in TOOL_DEFINITIONS).count("update_plan") == 1
    schema = definitions["update_plan"]["parameters"]
    items = schema["properties"]["items"]
    item_schema = items["items"]

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["items"]
    assert schema["properties"]["explanation"]["maxLength"] == 500
    assert items["minItems"] == 1
    assert items["maxItems"] == 20
    assert item_schema["additionalProperties"] is False
    assert item_schema["properties"]["step"]["maxLength"] == 200
    assert set(item_schema["properties"]["status"]["enum"]) == {
        "pending",
        "in_progress",
        "completed",
    }


def test_update_plan_is_session_only_read_only_safe_and_never_requests_approval(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "keep.txt"
    marker.write_text("unchanged\n", encoding="utf-8")
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    def forbidden_approval(_request: object) -> object:
        raise AssertionError("update_plan must not request approval")

    result = execute_tool(
        _config(tmp_path),
        "update_plan",
        json.dumps(_plan_arguments()),
        approval_handler=forbidden_approval,  # type: ignore[arg-type]
    )
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    policy = get_tool_policy("update_plan")

    assert result.ok is True
    assert result.data == {
        "type": "plan_update",
        "plan": _plan_arguments(),
    }
    assert policy.effect == "session_only"
    assert policy.approval_required is False
    assert before == after


def test_completed_plan_cannot_reopen_in_tool_or_reducer(tmp_path: Path) -> None:
    completed = parse_plan_update(_plan_arguments(completed=True))
    tool_result = execute_tool(
        _config(tmp_path),
        "update_plan",
        json.dumps(_plan_arguments()),
        plan_state=completed,
    )

    started = _started_event(tmp_path)
    state = reduce_event(None, started)
    completed_event = _next_event(
        started,
        "plan.updated",
        {"plan": plan_state_to_dict(completed)},
    )
    state = reduce_event(state, completed_event)
    reopened_event = _next_event(
        completed_event,
        "plan.updated",
        {"plan": _plan_arguments()},
    )

    assert tool_result.ok is False
    assert "cannot return" in tool_result.output
    with pytest.raises(SessionReductionError, match="cannot return"):
        reduce_event(state, reopened_event)


def test_checkpoint_round_trip_persists_plan_and_legacy_checkpoint_defaults_empty(
    tmp_path: Path,
) -> None:
    started = _started_event(tmp_path)
    state = reduce_event(None, started)
    update = _next_event(started, "plan.updated", {"plan": _plan_arguments()})
    state = reduce_event(state, update)
    encoded = checkpoint_to_dict(state.to_checkpoint())

    restored = checkpoint_from_dict(encoded)
    legacy = dict(encoded)
    legacy.pop("plan")
    restored_legacy = checkpoint_from_dict(legacy)

    assert restored.plan == state.plan
    assert encoded["plan"] == _plan_arguments()
    assert restored_legacy.plan == EMPTY_PLAN


def test_reducer_rebuilds_latest_plan_without_sidecar_state(tmp_path: Path) -> None:
    started = _started_event(tmp_path)
    first = _next_event(started, "plan.updated", {"plan": _plan_arguments()})
    second_plan = {
        "explanation": "parser inspected",
        "items": [
            {"step": "inspect parser", "status": "completed"},
            {"step": "add regression test", "status": "in_progress"},
        ],
    }
    second = _next_event(first, "plan.updated", {"plan": second_plan})

    state = rebuild_state((started, first, second))

    assert plan_state_to_dict(state.plan) == second_plan
    assert not any(path.name.lower().startswith("plan") for path in tmp_path.iterdir())


def test_agent_persists_plan_before_ui_projection_and_tool_completion(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    client = _PlanThenFinalClient()
    ui_events: list[UiEvent] = []
    durable_types_seen_at_projection: list[str] = []
    def handle_ui(event: UiEvent) -> None:
        ui_events.append(event)
        if event.type == "plan.updated":
            session_id = store.list_sessions()[0].session_id
            durable_types_seen_at_projection.extend(
                item.type for item in store.load(session_id)
            )

    report = run_agent_with_report(
        "make a plan",
        _config(tmp_path),
        model_client=client,
        session_store=store,
        approval_handler=lambda _request: pytest.fail(
            "update_plan must not request approval"
        ),
        ui_emitter=UiEmitter(handle_ui),
        stream=False,
    )
    events = store.load(report.session_id or "")
    relevant = [
        event.type
        for event in events
        if event.type in {"tool.started", "plan.updated", "tool.finished"}
    ]
    output = json.loads((client.received_tool_outputs or [])[0]["output"])

    assert report.answer == "done"
    assert relevant == ["tool.started", "plan.updated", "tool.finished"]
    assert durable_types_seen_at_projection[-1] == "plan.updated"
    assert [event.type for event in ui_events].count("plan.updated") == 1
    assert output["ok"] is True
    assert output["data"]["type"] == "plan_update"
    assert client.continuation_count == 1


def test_resume_restores_unfinished_plan_and_does_not_reexecute_completed_tool(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    client = _PlanThenFinalClient()
    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "make a resumable plan",
            _config(tmp_path),
            model_client=client,
            session_store=store,
            fault_injector=_interrupt_at("after_tool_finished"),  # type: ignore[arg-type]
            stream=False,
        )
    session_id = store.list_sessions()[0].session_id
    before_resume = rebuild_state(store.load(session_id))
    resume_ui: list[UiEvent] = []

    report = resume_agent_with_report(
        session_id,
        tmp_path,
        model_client=client,
        session_store=store,
        ui_emitter=UiEmitter(resume_ui.append),
        stream=False,
    )
    after_resume = rebuild_state(store.load(session_id))

    assert before_resume.plan.items[0].status == "in_progress"
    assert after_resume.plan == before_resume.plan
    assert client.initial_count == 1
    assert client.continuation_count == 1
    assert report.answer == "done"
    plan_event = next(event for event in resume_ui if event.type == "plan.updated")
    assert plan_event.payload["items"][0]["status"] == "in_progress"  # type: ignore[index]


def test_interrupted_session_only_plan_tool_is_safe_to_retry(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    client = _PlanThenFinalClient()
    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "interrupt a plan update",
            _config(tmp_path),
            model_client=client,
            session_store=store,
            fault_injector=_interrupt_at("after_tool_side_effect"),  # type: ignore[arg-type]
            stream=False,
        )
    session_id = store.list_sessions()[0].session_id
    events = store.load(session_id)
    state = rebuild_state(events)

    recovery = plan_interrupted_tools(tmp_path, events, state)

    assert state.plan == EMPTY_PLAN
    assert len(recovery) == 1
    assert recovery[0].effect == "session_only"
    assert recovery[0].disposition == "safe_retry"
    assert recovery[0].requires_explicit_approval is False


def test_replay_projects_latest_reducer_plan_and_legacy_empty_plan(
    tmp_path: Path,
) -> None:
    valid_workspace = tmp_path / "valid"
    valid_workspace.mkdir()
    store = SessionStore(valid_workspace)
    client = _PlanThenFinalClient()
    report = run_agent_with_report(
        "replay a plan",
        _config(valid_workspace),
        model_client=client,
        session_store=store,
        stream=False,
    )
    replay = build_session_replay_payload(
        SessionStore(valid_workspace, read_only=True),
        report.session_id or "",
    )
    plan_timeline = next(
        item for item in replay["timeline"] if item["type"] == "plan.updated"
    )

    legacy_workspace = tmp_path / "legacy"
    legacy_workspace.mkdir()
    legacy_store = SessionStore(legacy_workspace)
    legacy_id = legacy_store.create(
        {"task": "legacy", "workspace": str(legacy_workspace)}
    )
    legacy_replay = build_session_replay_payload(
        SessionStore(legacy_workspace, read_only=True),
        legacy_id,
    )

    assert replay["plan"] == _plan_arguments()
    assert plan_timeline["summary"] == "plan updated (2 items, 0 completed)"
    assert plan_timeline["details"] == {
        "item_count": 2,
        "completed_count": 0,
        "in_progress_count": 1,
        "pending_count": 1,
    }
    assert legacy_replay["plan"] == {"explanation": "", "items": []}


def test_plan_ui_has_stable_terminal_markers_and_structured_jsonl() -> None:
    event = UiEvent(1, 1, "plan.updated", _plan_arguments())
    terminal_output = io.StringIO()
    TerminalRenderer(
        stdout=terminal_output,
        stderr=io.StringIO(),
        is_tty=True,
        color_enabled=False,
    )(event)
    jsonl_output = io.StringIO()
    JsonlRenderer(stdout=jsonl_output, stderr=io.StringIO())(event)
    payload = json.loads(jsonl_output.getvalue())

    rendered = terminal_output.getvalue()
    assert "plan: Keep the resumable work explicit" in rendered
    assert "[>] inspect parser" in rendered
    assert "[ ] add regression test" in rendered
    assert payload["type"] == "plan.updated"
    assert payload["payload"] == _plan_arguments()
    assert "\x1b" not in terminal_output.getvalue()
    assert "\x1b" not in jsonl_output.getvalue()

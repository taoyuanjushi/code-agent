import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coding_agent.agent import run_agent_with_report
from coding_agent.model_client import normalize_model_response
from coding_agent.sessions.codec import artifact_ref_from_dict
from coding_agent.sessions.privacy import SessionPrivacyPolicy
from coding_agent.sessions.reducer import rebuild_state
from coding_agent.sessions.store import SessionStore
from coding_agent.types import AgentConfig


def _config(tmp_path: Path, *, max_turns: int = 4) -> AgentConfig:
    return AgentConfig(
        workspace=str(tmp_path),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=max_turns,
        permission_mode="read-only",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=6,
        context_max_bytes_per_file=4_000,
    )


def test_normalize_model_response_accepts_sdk_objects_and_mappings() -> None:
    response = SimpleNamespace(
        id="response-1",
        output_text="final answer",
        output=[
            SimpleNamespace(
                type="reasoning",
                summary=[SimpleNamespace(text="inspect first")],
            ),
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "fallback text"}],
            },
            {
                "type": "function_call",
                "name": "search_text",
                "arguments": '{"pattern":"target"}',
                "call_id": "call-1",
            },
        ],
    )

    normalized = normalize_model_response(response)

    assert normalized.response_id == "response-1"
    assert normalized.text == "final answer"
    assert normalized.reasoning_summary == "inspect first"
    assert len(normalized.function_calls) == 1
    assert normalized.function_calls[0].call_id == "call-1"
    assert normalized.function_calls[0].name == "search_text"


class _PersistedToolClient:
    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.continuation_checked = False

    def create_initial_response(
        self,
        *,
        config: AgentConfig,
        instructions: str,
        input_text: str,
    ) -> dict[str, Any]:
        del config, instructions, input_text
        session = self.store.list_sessions()[0]
        assert self.store.load(session.session_id)[-1].type == "model.requested"
        return {
            "id": "response-1",
            "output": [
                {
                    "type": "function_call",
                    "name": "search_text",
                    "arguments": json.dumps({"pattern": "hello"}),
                    "call_id": "call-1",
                }
            ],
        }

    def create_tool_response(
        self,
        *,
        config: AgentConfig,
        previous_response_id: str,
        tool_outputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        del config
        session = self.store.list_sessions()[0]
        events = self.store.load(session.session_id)
        event_types = [event.type for event in events]
        assert event_types[-1] == "model.requested"
        assert event_types[-3:-1] == ["tool.finished", "checkpoint.saved"]
        assert previous_response_id == "response-1"
        assert tool_outputs[0]["call_id"] == "call-1"
        assert json.loads(tool_outputs[0]["output"])["ok"] is True
        self.continuation_checked = True
        return {
            "id": "response-2",
            "output_text": "done",
            "output": [],
        }


def test_agent_persists_model_tool_checkpoint_and_completion_events(
    tmp_path: Path,
) -> None:
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    store = SessionStore(tmp_path)
    client = _PersistedToolClient(store)

    report = run_agent_with_report(
        "find hello",
        _config(tmp_path),
        model_client=client,
        session_store=store,
    )

    assert client.continuation_checked is True
    assert report.answer == "done"
    assert report.session_id is not None
    events = store.load(report.session_id)
    assert [event.type for event in events] == [
        "session.started",
        "context.created",
        "model.requested",
        "model.responded",
        "checkpoint.saved",
        "tool.started",
        "tool.finished",
        "checkpoint.saved",
        "model.requested",
        "model.responded",
        "checkpoint.saved",
        "session.completed",
    ]
    assert rebuild_state(events).status == "completed"

    completed_payload = events[-1].payload
    report_ref = artifact_ref_from_dict(completed_payload["report_artifact"])
    persisted_report = json.loads(
        store.get_artifact(report.session_id, report_ref).decode("utf-8")
    )
    assert persisted_report["answer"] == "done"
    assert persisted_report["session_id"] == report.session_id


class _InitialFailureClient:
    def create_initial_response(self, **_kwargs: object) -> object:
        raise RuntimeError("remote unavailable")

    def create_tool_response(self, **_kwargs: object) -> object:
        raise AssertionError("continuation should not be called")


def test_model_failure_is_recorded_without_masking_original_error(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)

    with pytest.raises(RuntimeError, match="remote unavailable"):
        run_agent_with_report(
            "fail once",
            _config(tmp_path),
            model_client=_InitialFailureClient(),
            session_store=store,
        )

    events = store.load(store.list_sessions()[0].session_id)
    assert [event.type for event in events][-2:] == [
        "model.requested",
        "session.failed",
    ]
    assert events[-1].payload["reason"] == "exception"
    assert events[-1].payload["model_request_may_have_succeeded"] is True
    assert rebuild_state(events).status == "failed"


class _InterruptClient:
    def create_initial_response(self, **_kwargs: object) -> object:
        raise KeyboardInterrupt()

    def create_tool_response(self, **_kwargs: object) -> object:
        raise AssertionError("continuation should not be called")


def test_keyboard_interrupt_is_persisted_and_reraised(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        run_agent_with_report(
            "interrupt",
            _config(tmp_path),
            model_client=_InterruptClient(),
            session_store=store,
        )

    events = store.load(store.list_sessions()[0].session_id)
    assert events[-1].type == "session.interrupted"
    assert events[-1].payload["reason"] == "keyboard_interrupt"
    assert rebuild_state(events).status == "interrupted"


class _TurnLimitClient:
    def create_initial_response(self, **_kwargs: object) -> dict[str, Any]:
        return _tool_response("response-1", "call-1")

    def create_tool_response(self, **_kwargs: object) -> dict[str, Any]:
        return _tool_response("response-2", "call-2")


def test_turn_limit_is_recorded_as_session_failure(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("value\n", encoding="utf-8")
    store = SessionStore(tmp_path)

    with pytest.raises(RuntimeError, match="max turn limit"):
        run_agent_with_report(
            "list files",
            _config(tmp_path, max_turns=1),
            model_client=_TurnLimitClient(),
            session_store=store,
        )

    events = store.load(store.list_sessions()[0].session_id)
    assert events[-1].type == "session.failed"
    assert events[-1].payload["reason"] == "turn_limit"
    state = rebuild_state(events)
    assert state.status == "failed"
    assert state.turn_index == 2


def test_large_context_is_artifact_backed_by_the_store_policy(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("x" * 4_000, encoding="utf-8")
    store = SessionStore(
        tmp_path,
        privacy_policy=SessionPrivacyPolicy(
            inline_max_bytes=2_048,
            artifact_max_bytes=32_768,
        ),
    )

    report = run_agent_with_report(
        "summarize",
        _config(tmp_path),
        model_client=_FinalClient(),
        session_store=store,
    )

    assert report.session_id is not None
    context_event = next(
        event
        for event in store.load(report.session_id)
        if event.type == "context.created"
    )
    formatted_context = context_event.payload["formatted_context"]
    assert formatted_context["stored"] is True
    assert "artifact" in formatted_context


class _FinalClient:
    def create_initial_response(self, **_kwargs: object) -> dict[str, Any]:
        return {"id": "response-final", "output_text": "summary", "output": []}

    def create_tool_response(self, **_kwargs: object) -> object:
        raise AssertionError("continuation should not be called")


def _tool_response(response_id: str, call_id: str) -> dict[str, Any]:
    return {
        "id": response_id,
        "output": [
            {
                "type": "function_call",
                "name": "list_files",
                "arguments": json.dumps({"path": "."}),
                "call_id": call_id,
            }
        ],
    }


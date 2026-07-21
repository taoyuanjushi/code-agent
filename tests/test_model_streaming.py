from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from coding_agent.agent import run_agent_with_report
from coding_agent.model_client import (
    OpenAIResponsesClient,
    normalize_model_response,
)
from coding_agent.sessions.store import SessionStore
from coding_agent.types import AgentConfig
from coding_agent.ui import UiEmitter, UiEvent


class FakeStream:
    def __init__(
        self,
        events: list[dict[str, object]],
        error: BaseException | None = None,
    ) -> None:
        self.events = events
        self.error = error
        self.closed = False

    def __iter__(self) -> Iterator[dict[str, object]]:
        yield from self.events
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


class FakeResponses:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def create(self, **request: object) -> object:
        self.calls.append(request)
        return self.result


class FakeOpenAI:
    def __init__(self, responses: FakeResponses) -> None:
        self.responses = responses


def _config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="read-only",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=6,
        context_max_bytes_per_file=8_000,
    )


def _completed_response(text: str = "流式 ✅") -> dict[str, object]:
    return {
        "id": "response-streamed",
        "output_text": text,
        "output": [
            {
                "type": "reasoning",
                "summary": [{"text": "Visible reasoning summary."}],
            },
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "read_file",
                "arguments": '{"path":"README.md"}',
            },
        ],
    }


def _completed_final_response(text: str) -> dict[str, object]:
    return {
        "id": "response-final",
        "output_text": text,
        "output": [
            {
                "type": "reasoning",
                "summary": [{"text": "Visible final summary."}],
            }
        ],
    }


def test_stream_emits_visible_unicode_deltas_and_returns_completed_response(
    tmp_path: Path,
) -> None:
    completed = _completed_response()
    stream = FakeStream(
        [
            {"type": "response.output_text.delta", "delta": "流"},
            {"type": "response.output_text.delta", "delta": ""},
            {"type": "response.reasoning_summary_text.delta", "delta": "hidden"},
            {"type": "response.output_text.delta", "delta": "式 ✅"},
            {"type": "response.completed", "response": completed},
        ]
    )
    responses = FakeResponses(stream)
    events: list[UiEvent] = []
    client = OpenAIResponsesClient(
        FakeOpenAI(responses),  # type: ignore[arg-type]
        ui_emitter=UiEmitter(events.append),
    )

    raw_response = client.create_initial_response(
        config=_config(tmp_path),
        instructions="safe instructions",
        input_text="task",
    )
    normalized = normalize_model_response(raw_response)

    assert [event.payload["text"] for event in events] == ["流", "式 ✅"]
    assert "".join(event.payload["text"] for event in events) == normalized.text
    assert normalized.text == "流式 ✅"
    assert normalized.reasoning_summary == "Visible reasoning summary."
    assert normalized.function_calls[0].name == "read_file"
    assert responses.calls[0]["stream"] is True
    assert stream.closed is True


def test_unicode_graphemes_survive_every_delta_boundary(tmp_path: Path) -> None:
    text = "Cafe\u0301 ?? ??\u200d?? ?"

    for split_at in range(len(text) + 1):
        completed = _completed_final_response(text)
        stream = FakeStream(
            [
                {
                    "type": "response.output_text.delta",
                    "delta": text[:split_at],
                },
                {
                    "type": "response.output_text.delta",
                    "delta": text[split_at:],
                },
                {"type": "response.completed", "response": completed},
            ]
        )
        events: list[UiEvent] = []
        client = OpenAIResponsesClient(
            FakeOpenAI(FakeResponses(stream)),  # type: ignore[arg-type]
            ui_emitter=UiEmitter(events.append),
        )

        normalized = normalize_model_response(
            client.create_initial_response(
                config=_config(tmp_path),
                instructions="safe instructions",
                input_text="task",
            )
        )

        deltas = [
            event.payload["text"]
            for event in events
            if event.type == "model.output.delta"
        ]
        assert "".join(deltas) == text
        assert normalized.text == text
        assert stream.closed is True


def test_no_stream_uses_the_existing_complete_response_path(tmp_path: Path) -> None:
    completed = _completed_response("done")
    responses = FakeResponses(completed)
    client = OpenAIResponsesClient(
        FakeOpenAI(responses),  # type: ignore[arg-type]
        stream=False,
    )

    raw_response = client.create_tool_response(
        config=_config(tmp_path),
        previous_response_id="response-previous",
        tool_outputs=[{"type": "function_call_output", "call_id": "call-1"}],
    )

    assert raw_response is completed
    assert "stream" not in responses.calls[0]
    assert responses.calls[0]["previous_response_id"] == "response-previous"


def test_successful_stream_persists_only_the_completed_response(
    tmp_path: Path,
) -> None:
    stream = FakeStream(
        [
            {"type": "response.output_text.delta", "delta": "你"},
            {"type": "response.output_text.delta", "delta": "好"},
            {
                "type": "response.completed",
                "response": _completed_final_response("你好"),
            },
        ]
    )
    responses = FakeResponses(stream)
    ui_events: list[UiEvent] = []
    emitter = UiEmitter(ui_events.append)
    client = OpenAIResponsesClient(
        FakeOpenAI(responses),  # type: ignore[arg-type]
        ui_emitter=emitter,
    )
    store = SessionStore(tmp_path)

    report = run_agent_with_report(
        "stream a response",
        _config(tmp_path),
        model_client=client,
        session_store=store,
        ui_emitter=emitter,
    )

    session_events = store.load(report.session_id or "")
    model_events = [
        event for event in session_events if event.type == "model.responded"
    ]
    assert report.answer == "你好"
    assert [
        event.payload["text"]
        for event in ui_events
        if event.type == "model.output.delta"
    ] == ["你", "好"]
    assert len(model_events) == 1
    assert model_events[0].payload["response"]["text"] == "你好"
    assert "model.output.delta" not in [
        event.type for event in session_events
    ]
    assert stream.closed is True


@pytest.mark.parametrize(
    ("events", "message"),
    [
        ([{"type": "error", "message": "stream API error"}], "stream API error"),
        (
            [
                {
                    "type": "response.failed",
                    "response": {"error": {"message": "model failed"}},
                }
            ],
            "model failed",
        ),
        ([], "without a response.completed"),
    ],
)
def test_stream_requires_one_successful_completion(
    tmp_path: Path,
    events: list[dict[str, object]],
    message: str,
) -> None:
    stream = FakeStream(events)
    client = OpenAIResponsesClient(
        FakeOpenAI(FakeResponses(stream)),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match=message):
        client.create_initial_response(
            config=_config(tmp_path),
            instructions="safe instructions",
            input_text="task",
        )

    assert stream.closed is True


@pytest.mark.parametrize(
    ("error", "terminal_event"),
    [
        (RuntimeError("connection dropped"), "session.failed"),
        (KeyboardInterrupt("cancelled"), "session.interrupted"),
    ],
)
def test_partial_stream_failure_is_persisted_without_retry(
    tmp_path: Path,
    error: BaseException,
    terminal_event: str,
) -> None:
    stream = FakeStream(
        [{"type": "response.output_text.delta", "delta": "partial"}],
        error=error,
    )
    responses = FakeResponses(stream)
    ui_events: list[UiEvent] = []
    emitter = UiEmitter(ui_events.append)
    client = OpenAIResponsesClient(
        FakeOpenAI(responses),  # type: ignore[arg-type]
        ui_emitter=emitter,
    )
    store = SessionStore(tmp_path)

    with pytest.raises(type(error), match=str(error)):
        run_agent_with_report(
            "stream a response",
            _config(tmp_path),
            model_client=client,
            session_store=store,
            ui_emitter=emitter,
        )

    session_id = store.list_sessions()[0].session_id
    session_events = store.load(session_id)
    assert responses.calls[0]["stream"] is True
    assert len(responses.calls) == 1
    assert any(event.type == "model.output.delta" for event in ui_events)
    assert terminal_event in [event.type for event in session_events]
    assert "model.responded" not in [event.type for event in session_events]
    assert stream.closed is True

from __future__ import annotations

import io
import json
from collections.abc import Mapping

import pytest

from coding_agent.sessions.privacy import INLINE_PAYLOAD_MAX_BYTES, REDACTION_MARKER
from coding_agent.ui import (
    FORBIDDEN_UI_PAYLOAD_FIELDS,
    TOOL_EVENT_PAYLOAD_FIELDS,
    UI_EVENT_FIELDS,
    UI_EVENT_TYPES,
    UI_SCHEMA_VERSION,
    JsonlRenderer,
    TerminalRenderer,
    UiEmitter,
    UiEvent,
    UiHandlerError,
    truncate_for_console,
)


class TrackingStringIO(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


class BrokenOutput:
    def write(self, _value: str) -> int:
        raise BrokenPipeError

    def flush(self) -> None:
        raise BrokenPipeError


def test_ui_contract_matches_the_frozen_m6_schema() -> None:
    assert UI_SCHEMA_VERSION == 1
    assert UI_EVENT_FIELDS == {"schema_version", "seq", "type", "payload"}
    assert UI_EVENT_TYPES == {
        "run.started",
        "model.started",
        "model.output.delta",
        "model.finished",
        "tool.started",
        "tool.finished",
        "approval.requested",
        "approval.decided",
        "verification.finished",
        "plan.updated",
        "run.finished",
        "run.interrupted",
        "run.failed",
    }


def test_ui_event_round_trips_through_strict_versioned_dict() -> None:
    event = UiEvent(
        schema_version=UI_SCHEMA_VERSION,
        seq=3,
        type="run.finished",
        payload={"status": "completed", "answer": "done"},
    )

    restored = UiEvent.from_dict(event.to_dict())

    assert restored == event
    assert json.loads(json.dumps(event.to_dict())) == event.to_dict()


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"schema_version": 2}, "schema version"),
        ({"schema_version": True}, "schema version"),
        ({"seq": 0}, "positive integer"),
        ({"seq": True}, "seq must be an integer"),
        ({"type": "unknown.event"}, "event type"),
        ({"payload": {"value": float("inf")}}, "finite JSON number"),
        ({"payload": {"value": object()}}, "not JSON-compatible"),
    ],
)
def test_ui_event_rejects_invalid_domain_values(
    changes: dict[str, object],
    error: str,
) -> None:
    values: dict[str, object] = {
        "schema_version": UI_SCHEMA_VERSION,
        "seq": 1,
        "type": "run.started",
        "payload": {},
        **changes,
    }

    with pytest.raises((TypeError, ValueError), match=error):
        UiEvent(**values)  # type: ignore[arg-type]


def test_ui_event_from_dict_rejects_missing_unknown_and_mistyped_fields() -> None:
    event = UiEvent(1, 1, "run.started", {}).to_dict()

    missing = dict(event)
    missing.pop("payload")
    unknown = {**event, "extra": True}
    mistyped = {**event, "seq": "1"}
    non_string_key = {**event, 1: True}

    with pytest.raises(ValueError, match="missing fields: payload"):
        UiEvent.from_dict(missing)
    with pytest.raises(ValueError, match="unknown fields: extra"):
        UiEvent.from_dict(unknown)
    with pytest.raises(TypeError, match="seq must be an integer"):
        UiEvent.from_dict(mistyped)
    with pytest.raises(TypeError, match="field names must be strings"):
        UiEvent.from_dict(non_string_key)  # type: ignore[arg-type]


def test_ui_event_payload_is_detached_and_deeply_immutable() -> None:
    source = {"nested": {"items": ["first"]}}
    event = UiEvent(1, 1, "run.started", source)
    source["nested"]["items"].append("later")  # type: ignore[index,union-attr]

    nested = event.payload["nested"]
    assert isinstance(nested, Mapping)
    assert nested["items"] == ("first",)
    with pytest.raises(TypeError):
        event.payload["new"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        nested["new"] = True  # type: ignore[index]


def test_emitter_assigns_contiguous_sequences_and_calls_handler_synchronously() -> None:
    received: list[UiEvent] = []
    emitter = UiEmitter(received.append)

    first = emitter.emit("run.started", {"mode": "run"})
    second = emitter.emit("model.started", {})

    assert received == [first, second]
    assert [event.seq for event in received] == [1, 2]
    assert emitter.next_seq == 3


def test_default_emitter_is_a_noop_handler() -> None:
    event = UiEmitter().emit("run.started", {})

    assert event.seq == 1
    assert event.type == "run.started"


def test_emitter_redacts_known_secret_values_and_sensitive_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-m6-ui-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    event = UiEmitter().emit(
        "run.failed",
        {
            "message": f"request failed for {secret}",
            "authorization": f"Bearer {secret}",
            "environment": {"OPENAI_API_KEY": secret},
        },
    )
    encoded = json.dumps(event.to_dict())

    assert secret not in encoded
    assert REDACTION_MARKER in encoded
    assert "authorization" not in event.payload
    assert "environment" not in event.payload
    assert event.payload["_privacy"] == {
        "redacted": True,
        "secret_field_count": 1,
        "process_context_count": 1,
    }


def test_large_ui_payload_is_omitted_without_creating_an_artifact() -> None:
    event = UiEmitter().emit(
        "run.failed",
        {"traceback": "x" * (INLINE_PAYLOAD_MAX_BYTES + 1)},
    )

    traceback = event.payload["traceback"]
    assert isinstance(traceback, Mapping)
    assert traceback["stored"] is False
    assert traceback["reason"] == "artifact_writer_unavailable"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": 1},
        {"text": "visible", "reasoning": "hidden"},
    ],
)
def test_model_delta_accepts_only_visible_text(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="only string field 'text'"):
        UiEmitter().emit("model.output.delta", payload)

    visible = UiEmitter().emit("model.output.delta", {"text": "visible"})
    assert visible.payload == {"text": "visible"}


def test_ui_events_reject_raw_model_and_prompt_fields() -> None:
    assert FORBIDDEN_UI_PAYLOAD_FIELDS == {
        "raw_sdk_response",
        "prompt",
        "instructions",
        "chain_of_thought",
    }
    for field in FORBIDDEN_UI_PAYLOAD_FIELDS:
        with pytest.raises(ValueError, match="forbidden fields"):
            UiEmitter().emit("run.failed", {field: "not allowed"})


def test_tool_events_have_a_small_fixed_payload_surface() -> None:
    assert TOOL_EVENT_PAYLOAD_FIELDS == {
        "call_id",
        "name",
        "status",
        "duration_ms",
        "backend",
        "sandboxed",
        "summary",
        "output_truncated",
    }
    started = UiEmitter().emit(
        "tool.started",
        {"call_id": "call-1", "name": "read_file"},
    )
    finished = UiEmitter().emit(
        "tool.finished",
        {
            "call_id": "call-1",
            "name": "read_file",
            "status": "passed",
            "duration_ms": 4,
        },
    )

    assert started.payload["name"] == "read_file"
    assert finished.payload["duration_ms"] == 4
    with pytest.raises(ValueError, match="unsupported fields: environment"):
        UiEmitter().emit(
            "tool.started",
            {"call_id": "call-1", "name": "read_file", "environment": {}},
        )
    with pytest.raises(ValueError, match="missing fields: duration_ms, status"):
        UiEmitter().emit(
            "tool.finished",
            {"call_id": "call-1", "name": "read_file"},
        )
    with pytest.raises(TypeError, match="sandboxed must be a boolean"):
        UiEmitter().emit(
            "tool.started",
            {"call_id": "call-1", "name": "read_file", "sandboxed": "yes"},
        )


def test_handler_errors_are_wrapped_with_event_identity() -> None:
    error = RuntimeError("broken renderer")

    def broken_handler(_event: UiEvent) -> None:
        raise error

    emitter = UiEmitter(broken_handler)
    with pytest.raises(UiHandlerError, match="run.started seq 1") as caught:
        emitter.emit("run.started", {})

    assert caught.value.__cause__ is error
    assert emitter.next_seq == 2


def test_emitter_rejects_non_callable_handler() -> None:
    with pytest.raises(TypeError, match="callable or null"):
        UiEmitter(handler=object())  # type: ignore[arg-type]


def test_non_tty_terminal_renderer_emits_one_plain_line_per_event() -> None:
    stdout = TrackingStringIO()
    stderr = TrackingStringIO()
    renderer = TerminalRenderer(
        stdout=stdout,
        stderr=stderr,
        stdin=io.StringIO(),
        is_tty=False,
    )
    emitter = UiEmitter(renderer)

    emitter.emit("run.started", {"workspace": "D:/项目"})
    emitter.emit("model.output.delta", {"text": "first\nsecond"})
    emitter.emit("run.failed", {"message": "no\rrewrite"})

    stdout_lines = stdout.getvalue().splitlines()
    stderr_lines = stderr.getvalue().splitlines()
    assert len(stdout_lines) == 2
    assert len(stderr_lines) == 1
    assert "[1] run.started" in stdout_lines[0]
    assert "first\\nsecond" in stdout_lines[1]
    assert "[3] run.failed" in stderr_lines[0]
    assert all(
        token not in stdout.getvalue() + stderr.getvalue()
        for token in ("\x1b", "\r", "\b")
    )
    assert stdout.flush_count == 2
    assert stderr.flush_count == 1


def test_tty_terminal_renderer_streams_deltas_without_repeating_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    stdout = TrackingStringIO()
    renderer = TerminalRenderer(
        stdout=stdout,
        stderr=io.StringIO(),
        stdin=io.StringIO(),
        is_tty=True,
        color_enabled=True,
    )
    emitter = UiEmitter(renderer)

    emitter.emit("model.started", {})
    emitter.emit("model.output.delta", {"text": "你"})
    emitter.emit("model.output.delta", {"text": "好\r\b"})
    emitter.emit("model.finished", {"text": "你好"})
    emitter.emit("run.finished", {"status": "completed", "answer": "你好"})

    rendered = stdout.getvalue()
    assert rendered.count("你好") == 1
    assert "好\n\\x08\n" in rendered
    assert "\x1b[" in rendered
    assert "\r" not in rendered
    assert "\b" not in rendered
    assert "run.finished" in rendered


@pytest.mark.parametrize("color_enabled", [True, False])
def test_terminal_renderer_honors_no_color_inputs(
    color_enabled: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if color_enabled:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
    stdout = io.StringIO()
    renderer = TerminalRenderer(
        stdout=stdout,
        stderr=io.StringIO(),
        stdin=io.StringIO(),
        is_tty=True,
        color_enabled=color_enabled,
    )

    renderer(UiEmitter().emit("run.started", {}))

    assert "\x1b" not in stdout.getvalue()


def test_terminal_line_boundary_finishes_and_flushes_partial_delta() -> None:
    stdout = TrackingStringIO()
    renderer = TerminalRenderer(
        stdout=stdout,
        stderr=io.StringIO(),
        stdin=io.StringIO(),
        is_tty=True,
        color_enabled=False,
    )

    renderer(UiEmitter().emit("model.output.delta", {"text": "partial"}))
    renderer.ensure_line_boundary()
    renderer.ensure_line_boundary()

    assert stdout.getvalue() == "partial\n"
    assert stdout.flush_count == 3


def test_empty_tty_delta_does_not_hide_the_final_answer() -> None:
    stdout = io.StringIO()
    renderer = TerminalRenderer(
        stdout=stdout,
        stderr=io.StringIO(),
        stdin=io.StringIO(),
        is_tty=True,
        color_enabled=False,
    )
    emitter = UiEmitter(renderer)

    emitter.emit("model.output.delta", {"text": ""})
    emitter.emit("run.finished", {"status": "completed", "answer": "done"})

    assert stdout.getvalue() == (
        "done\nrun.finished: status=\"completed\"\n"
    )


def test_terminal_renderer_reuses_the_shared_console_budget() -> None:
    stdout = io.StringIO()
    renderer = TerminalRenderer(
        stdout=stdout,
        stderr=io.StringIO(),
        stdin=io.StringIO(),
        is_tty=False,
    )

    renderer(UiEmitter().emit("run.started", {"message": "x" * 2100}))

    assert truncate_for_console("x" * 2100).endswith(
        "\n[console output truncated]"
    )
    assert stdout.getvalue().count("\n") == 1
    assert "\\n[console output truncated]" in stdout.getvalue()


def test_non_tty_approval_request_is_not_console_truncated() -> None:
    stdout = io.StringIO()
    renderer = TerminalRenderer(
        stdout=stdout,
        stderr=io.StringIO(),
        stdin=io.StringIO(),
        is_tty=False,
    )
    message = "review:" + "x" * 2100

    renderer(
        UiEmitter().emit(
            "approval.requested",
            {
                "call_id": "call-1",
                "action": "apply_patch",
                "message": message,
            },
        )
    )

    assert message in stdout.getvalue()
    assert "console output truncated" not in stdout.getvalue()


def test_jsonl_renderer_emits_compact_unicode_events_and_flushes() -> None:
    stdout = TrackingStringIO()
    stderr = TrackingStringIO()
    renderer = JsonlRenderer(stdout=stdout, stderr=stderr)
    emitter = UiEmitter(renderer)

    first = emitter.emit("run.started", {"workspace": "D:/项目"})
    second = emitter.emit("model.output.delta", {"text": "你好"})

    lines = stdout.getvalue().splitlines()
    assert [json.loads(line) for line in lines] == [
        first.to_dict(),
        second.to_dict(),
    ]
    assert len(lines) == 2
    assert "你好" in lines[1]
    assert '"seq":1' in lines[0]
    assert ": " not in lines[0]
    assert "\x1b" not in stdout.getvalue()
    assert stdout.flush_count == 2
    assert stderr.getvalue() == ""


def test_jsonl_diagnostics_use_only_stderr() -> None:
    stdout = io.StringIO()
    stderr = TrackingStringIO()
    renderer = JsonlRenderer(stdout=stdout, stderr=stderr)

    renderer.diagnostic("invalid configuration")

    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "invalid configuration\n"
    assert stderr.flush_count == 1


def test_jsonl_serialization_errors_are_reported_only_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    renderer = JsonlRenderer(stdout=stdout, stderr=stderr)
    event = UiEmitter().emit("run.started", {})

    def fail_serialization(*_args: object, **_kwargs: object) -> str:
        raise TypeError("cannot encode")

    monkeypatch.setattr("coding_agent.ui.json.dumps", fail_serialization)

    with pytest.raises(TypeError, match="cannot encode"):
        renderer(event)

    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "JSONL serialization failed: cannot encode\n"


@pytest.mark.parametrize("renderer_type", [TerminalRenderer, JsonlRenderer])
def test_renderers_stop_silently_after_broken_pipe(renderer_type: type) -> None:
    if renderer_type is TerminalRenderer:
        renderer = renderer_type(
            stdout=BrokenOutput(),
            stderr=io.StringIO(),
            stdin=io.StringIO(),
            is_tty=False,
        )
    else:
        renderer = renderer_type(stdout=BrokenOutput(), stderr=io.StringIO())
    event = UiEmitter().emit("run.started", {})

    renderer(event)
    renderer(event)

    assert renderer.closed is True


def test_renderers_validate_streams_and_color_options() -> None:
    with pytest.raises(TypeError, match="stdout"):
        JsonlRenderer(stdout=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="stdin"):
        TerminalRenderer(stdin=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="is_tty"):
        TerminalRenderer(is_tty="yes")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="color_enabled"):
        TerminalRenderer(color_enabled=1)  # type: ignore[arg-type]

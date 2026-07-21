from __future__ import annotations

import errno
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, TextIO, TypeAlias, cast

from .plans import plan_state_from_dict
from .reviews import (
    ReviewResult,
    review_result_from_dict,
    review_result_to_dict,
    sorted_review_findings,
)
from .sessions.models import JsonObject, JsonValue, freeze_json_object
from .sessions.privacy import SessionPrivacyPolicy
from .tool_outputs import thaw_json

UI_SCHEMA_VERSION = 1

UiEventType = Literal[
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
]
UI_EVENT_TYPES = frozenset(
    {
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
)
UI_EVENT_FIELDS = frozenset({"schema_version", "seq", "type", "payload"})
FORBIDDEN_UI_PAYLOAD_FIELDS = frozenset(
    {"raw_sdk_response", "prompt", "instructions", "chain_of_thought"}
)

TOOL_EVENT_PAYLOAD_FIELDS = frozenset(
    {
        "call_id",
        "name",
        "status",
        "duration_ms",
        "backend",
        "sandboxed",
        "summary",
        "output_truncated",
    }
)

UiEventHandler: TypeAlias = Callable[["UiEvent"], None]
_UI_PRIVACY_POLICY = SessionPrivacyPolicy()
_ERROR_EVENT_TYPES = frozenset({"run.failed", "run.interrupted"})
_JSON_DUMPS_OPTIONS = {
    "ensure_ascii": False,
    "sort_keys": True,
    "separators": (",", ":"),
}

_ANSI_RESET = "\x1b[0m"
_ANSI_CYAN = "\x1b[36m"
_ANSI_GREEN = "\x1b[32m"
_ANSI_YELLOW = "\x1b[33m"
_ANSI_RED = "\x1b[31m"
_CONSOLE_OUTPUT_LIMIT = 2000


class UiHandlerError(RuntimeError):
    """Raised when the configured UI event handler cannot consume an event."""


class TerminalRenderer:
    """Render UI events as small, line-oriented terminal updates."""

    def __init__(
        self,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        stdin: TextIO | None = None,
        is_tty: bool | None = None,
        color_enabled: bool = True,
    ) -> None:
        self.stdout = _output_stream(stdout, sys.stdout, "stdout")
        self.stderr = _output_stream(stderr, sys.stderr, "stderr")
        self.stdin = _input_stream(stdin, sys.stdin)
        if is_tty is not None and not isinstance(is_tty, bool):
            raise TypeError("is_tty must be a boolean or null.")
        if not isinstance(color_enabled, bool):
            raise TypeError("color_enabled must be a boolean.")
        if is_tty is None:
            isatty = getattr(self.stdout, "isatty", None)
            is_tty = bool(isatty()) if callable(isatty) else False
        self.is_tty = is_tty
        self.color_enabled = (
            is_tty and color_enabled and "NO_COLOR" not in os.environ
        )
        self.closed = False
        self.stdout_closed = False
        self._line_open = False
        self._saw_model_delta = False
        self._saw_model_output = False

    def __call__(self, event: UiEvent) -> None:
        if not isinstance(event, UiEvent):
            raise TypeError("TerminalRenderer expects a UiEvent.")
        if self.closed:
            return
        if not self.is_tty:
            self._render_plain(event)
            return
        if event.type == "model.output.delta":
            text = event.payload["text"]
            if isinstance(text, str):
                safe_text = _safe_terminal_text(text)
                if not safe_text:
                    self._flush(self.stdout)
                    return
                self._saw_model_delta = True
                if self._write(self.stdout, safe_text, flush=True):
                    self._line_open = not safe_text.endswith("\n")
            else:
                self.ensure_line_boundary()
                self._write_line(self.stdout, "model.output.delta: text omitted")
            return

        self.ensure_line_boundary()
        if event.type == "plan.updated":
            self._write_line(
                self.stdout,
                truncate_for_console(
                    _safe_terminal_text(_format_plan(event.payload, multiline=True))
                ),
            )
            return
        if event.type == "model.finished":
            reasoning = event.payload.get("reasoning_summary")
            if isinstance(reasoning, str) and reasoning:
                self._write_line(
                    self.stdout,
                    f"reasoning summary:\n{_safe_terminal_text(reasoning)}",
                )
            text = event.payload.get("text")
            if isinstance(text, str) and text and not self._saw_model_delta:
                self._saw_model_output = True
                self._write_line(self.stdout, _safe_terminal_text(text))
            return
        if event.type == "approval.requested":
            message = event.payload.get("message")
            if isinstance(message, str) and message:
                self._write_line(
                    self.stdout,
                    self._style(_safe_terminal_text(message), event),
                )
                return
        if (
            event.type == "run.finished"
            and not self._saw_model_delta
            and not self._saw_model_output
        ):
            answer = event.payload.get("answer")
            if isinstance(answer, str) and answer:
                self._write_line(
                    self.stdout,
                    truncate_for_console(_safe_terminal_text(answer)),
                )
        if event.type == "run.finished":
            review = _review_from_payload(event.payload)
            if review is not None:
                for line in _format_review_lines(review):
                    self._write_line(
                        self.stdout,
                        truncate_for_console(_safe_terminal_text(line)),
                    )
        stream = self.stderr if event.type in _ERROR_EVENT_TYPES else self.stdout
        summary = truncate_for_console(self._summary(event))
        self._write_line(stream, self._style(summary, event))
        if event.type == "tool.finished":
            output = event.payload.get("summary")
            if isinstance(output, str) and output:
                self._write_line(
                    stream,
                    truncate_for_console(_safe_terminal_text(output)),
                )

    def ensure_line_boundary(self) -> None:
        """Finish a partial streaming line and flush before interactive input."""
        if self.closed:
            return
        if self._line_open:
            if not self._write(self.stdout, "\n", flush=True):
                return
            self._line_open = False
        else:
            self._flush(self.stdout)

    def _render_plain(self, event: UiEvent) -> None:
        stream = self.stderr if event.type in _ERROR_EVENT_TYPES else self.stdout
        if event.type == "plan.updated":
            line = (
                f"[{event.seq}] plan.updated "
                f"{_format_plan(event.payload, multiline=False)}"
            )
            self._write_line(stream, truncate_for_console(line))
            return
        event_payload = event.to_dict()["payload"]
        if event.type == "run.finished":
            review = _review_from_payload(event.payload)
            if review is not None and isinstance(event_payload, dict):
                event_payload["review"] = _sorted_review_to_dict(review)
        payload = json.dumps(event_payload, **_JSON_DUMPS_OPTIONS)
        line = f"[{event.seq}] {event.type} {payload}"
        if event.type == "approval.requested":
            self._write_line(stream, line)
            return
        self._write_line(
            stream,
            truncate_for_console(line).replace("\n", "\\n"),
        )

    def _summary(self, event: UiEvent) -> str:
        fields = (
            "name",
            "action",
            "call_id",
            "status",
            "outcome",
            "source",
            "duration_ms",
            "backend",
            "sandboxed",
            "message",
            "decision",
            "mode",
            "task_mode",
            "permission",
            "model",
            "sandbox",
            "previous_phase",
            "previous_status",
            "workspace",
            "session_id",
            "request_kind",
            "turn_index",
            "final_status",
            "command_id",
            "kind",
            "exit_code",
            "attempt",
        )
        details = [
            f"{key}={json.dumps(event.payload[key], **_JSON_DUMPS_OPTIONS)}"
            for key in fields
            if key in event.payload
            and isinstance(event.payload[key], (str, int, bool))
        ]
        progress = event.payload.get("plan_progress")
        if isinstance(progress, Mapping):
            completed = progress.get("completed")
            total = progress.get("total")
            in_progress = progress.get("in_progress")
            pending = progress.get("pending")
            if all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in (completed, total, in_progress, pending)
            ):
                details.append(
                    "plan_progress="
                    f"{completed}/{total} completed,"
                    f"{in_progress} in_progress,{pending} pending"
                )
        if details:
            return f"{event.type}: {' '.join(details)}"
        return event.type

    def _style(self, text: str, event: UiEvent) -> str:
        if not self.color_enabled:
            return text
        status = event.payload.get("status")
        if event.type == "run.failed" or status == "failed":
            color = _ANSI_RED
        elif event.type in {"run.interrupted", "approval.requested"}:
            color = _ANSI_YELLOW
        elif event.type.endswith(".finished"):
            color = _ANSI_GREEN
        else:
            color = _ANSI_CYAN
        return f"{color}{text}{_ANSI_RESET}"

    def _write_line(self, stream: TextIO, text: str) -> None:
        self._write(stream, f"{text}\n", flush=True)

    def _write(self, stream: TextIO, text: str, *, flush: bool) -> bool:
        try:
            stream.write(text)
            if flush:
                stream.flush()
        except OSError as exc:
            if not _is_broken_pipe(exc):
                raise
            self.closed = True
            if stream is self.stdout:
                self.stdout_closed = True
            return False
        return True

    def _flush(self, stream: TextIO) -> None:
        try:
            stream.flush()
        except OSError as exc:
            if not _is_broken_pipe(exc):
                raise
            self.closed = True
            if stream is self.stdout:
                self.stdout_closed = True


class JsonlRenderer:
    """Render each UI event as one compact JSON object on stdout."""

    def __init__(
        self,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self.stdout = _output_stream(stdout, sys.stdout, "stdout")
        self.stderr = _output_stream(stderr, sys.stderr, "stderr")
        self.closed = False
        self.stdout_closed = False

    def __call__(self, event: UiEvent) -> None:
        if not isinstance(event, UiEvent):
            raise TypeError("JsonlRenderer expects a UiEvent.")
        if self.closed:
            return
        try:
            line = json.dumps(event.to_dict(), **_JSON_DUMPS_OPTIONS)
        except (TypeError, ValueError) as exc:
            self.diagnostic(f"JSONL serialization failed: {exc}")
            raise
        try:
            self.stdout.write(f"{line}\n")
            self.stdout.flush()
        except OSError as exc:
            if not _is_broken_pipe(exc):
                raise
            self.closed = True
            self.stdout_closed = True

    def diagnostic(self, message: str) -> None:
        if not isinstance(message, str):
            raise TypeError("JSONL diagnostic must be a string.")
        if self.closed:
            return
        try:
            self.stderr.write(f"{message}\n")
            self.stderr.flush()
        except OSError as exc:
            if not _is_broken_pipe(exc):
                raise
            self.closed = True


def truncate_for_console(value: str) -> str:
    """Apply the shared human-console output budget."""
    if len(value) <= _CONSOLE_OUTPUT_LIMIT:
        return value
    return f"{value[:_CONSOLE_OUTPUT_LIMIT]}\n[console output truncated]"


@dataclass(frozen=True)
class UiEvent:
    schema_version: int
    seq: int
    type: UiEventType
    payload: JsonObject

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(
            self.schema_version,
            int,
        ):
            raise TypeError("UI schema version must be an integer.")
        if self.schema_version != UI_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported UI schema version: {self.schema_version}"
            )
        if isinstance(self.seq, bool) or not isinstance(self.seq, int):
            raise TypeError("UI event seq must be an integer.")
        if self.seq <= 0:
            raise ValueError("UI event seq must be a positive integer.")
        if not isinstance(self.type, str):
            raise TypeError("UI event type must be a string.")
        if self.type not in UI_EVENT_TYPES:
            raise ValueError(f"Unsupported UI event type: {self.type}")

        payload = freeze_json_object(self.payload, "UI event payload")
        _validate_event_payload(self.type, payload)
        sanitized = _UI_PRIVACY_POLICY.sanitize_payload(payload)
        if not isinstance(sanitized, Mapping):
            raise RuntimeError("Sanitized UI event payload must be a mapping.")
        object.__setattr__(
            self,
            "payload",
            freeze_json_object(sanitized, "sanitized UI event payload"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "seq": self.seq,
            "type": self.type,
            "payload": thaw_json(self.payload),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> UiEvent:
        if not isinstance(data, Mapping):
            raise TypeError("UI event must be a mapping.")
        if not all(isinstance(key, str) for key in data):
            raise TypeError("UI event field names must be strings.")
        fields = set(data)
        if fields != UI_EVENT_FIELDS:
            missing = sorted(UI_EVENT_FIELDS - fields)
            unknown = sorted(fields - UI_EVENT_FIELDS)
            details = []
            if missing:
                details.append(f"missing fields: {', '.join(missing)}")
            if unknown:
                details.append(f"unknown fields: {', '.join(unknown)}")
            raise ValueError(f"Invalid UI event fields ({'; '.join(details)}).")

        event_type = data["type"]
        if not isinstance(event_type, str):
            raise TypeError("UI event type must be a string.")
        return cls(
            schema_version=_integer(data["schema_version"], "schema_version"),
            seq=_integer(data["seq"], "seq"),
            type=cast(UiEventType, event_type),
            payload=cast(JsonObject, data["payload"]),
        )


@dataclass
class UiEmitter:
    handler: UiEventHandler | None = None
    _next_seq: int = field(default=1, init=False, repr=False)
    _last_event_type: UiEventType | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _output_closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.handler is not None and not callable(self.handler):
            raise TypeError("UI event handler must be callable or null.")

    @property
    def next_seq(self) -> int:
        return self._next_seq

    @property
    def terminal_event_emitted(self) -> bool:
        return self._last_event_type in {
            "run.finished",
            "run.failed",
            "run.interrupted",
        }

    @property
    def output_closed(self) -> bool:
        if self._output_closed:
            return True
        return bool(getattr(self.handler, "stdout_closed", False))

    def emit(
        self,
        event_type: UiEventType,
        payload: Mapping[str, object],
    ) -> UiEvent:
        event = UiEvent(
            schema_version=UI_SCHEMA_VERSION,
            seq=self._next_seq,
            type=event_type,
            payload=cast(JsonObject, payload),
        )
        self._next_seq += 1
        if self.handler is None or self.output_closed:
            self._last_event_type = event.type
            return event
        try:
            self.handler(event)
        except OSError as exc:
            if _is_broken_pipe(exc):
                self._output_closed = True
            else:
                raise UiHandlerError(
                    f"UI event handler failed for {event.type} seq {event.seq}."
                ) from exc
        except Exception as exc:
            raise UiHandlerError(
                f"UI event handler failed for {event.type} seq {event.seq}."
            ) from exc
        self._last_event_type = event.type
        return event


def _validate_event_payload(
    event_type: UiEventType,
    payload: Mapping[str, JsonValue],
) -> None:
    forbidden = FORBIDDEN_UI_PAYLOAD_FIELDS & set(payload)
    if forbidden:
        raise ValueError(
            "UI event contains forbidden fields: "
            + ", ".join(sorted(forbidden))
        )
    if event_type == "model.output.delta":
        if set(payload) != {"text"} or not isinstance(payload["text"], str):
            raise ValueError(
                "model.output.delta payload must contain only string field 'text'."
            )
        return
    if event_type == "plan.updated":
        plan_state_from_dict(
            cast(Mapping[str, object], payload),
            allow_empty=False,
        )
        return

    if event_type == "run.finished":
        review = payload.get("review")
        if review is not None:
            if not isinstance(review, Mapping):
                raise TypeError("run.finished review must be an object or null.")
            review_result_from_dict(cast(Mapping[str, object], review))
        return
    if event_type not in {"tool.started", "tool.finished"}:
        return
    unknown = set(payload) - TOOL_EVENT_PAYLOAD_FIELDS
    if unknown:
        raise ValueError(
            "Tool UI event contains unsupported fields: "
            + ", ".join(sorted(unknown))
        )
    required = {"call_id", "name"}
    if event_type == "tool.finished":
        required.update({"status", "duration_ms"})
    missing = required - set(payload)
    if missing:
        raise ValueError(
            "Tool UI event is missing fields: " + ", ".join(sorted(missing))
        )
    for key in {"call_id", "name", "status", "summary"} & set(payload):
        if not isinstance(payload[key], str) or not payload[key]:
            raise ValueError(
                f"Tool UI event {key} must be a non-empty string."
            )
    if "backend" in payload and payload["backend"] is not None:
        backend = payload["backend"]
        if not isinstance(backend, str) or not backend:
            raise ValueError(
                "Tool UI event backend must be a non-empty string or null."
            )
    for key in {"sandboxed", "output_truncated"} & set(payload):
        if not isinstance(payload[key], bool):
            raise TypeError(f"Tool UI event {key} must be a boolean.")
    if "duration_ms" in payload:
        duration = payload["duration_ms"]
        if (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or duration < 0
        ):
            raise ValueError(
                "Tool UI event duration_ms must be a non-negative integer."
            )


def _review_from_payload(
    payload: Mapping[str, JsonValue],
) -> ReviewResult | None:
    value = payload.get("review")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("run.finished review must be an object or null.")
    return review_result_from_dict(cast(Mapping[str, object], value))


def _sorted_review_to_dict(value: ReviewResult) -> dict[str, object]:
    return review_result_to_dict(
        ReviewResult(
            summary=value.summary,
            findings=sorted_review_findings(value),
        )
    )


def _format_review_lines(value: ReviewResult) -> tuple[str, ...]:
    lines = [f"review: {value.summary}"]
    for finding in sorted_review_findings(value):
        lines.append(
            f"[{finding.severity}] {finding.path}:{finding.line} "
            f"{finding.title}"
        )
        lines.append(f"  {finding.detail}")
    return tuple(lines)


def _format_plan(
    payload: Mapping[str, JsonValue],
    *,
    multiline: bool,
) -> str:
    plan = plan_state_from_dict(
        cast(Mapping[str, object], payload),
        allow_empty=False,
    )
    markers = {
        "pending": "[ ]",
        "in_progress": "[>]",
        "completed": "[x]",
    }
    header = "plan"
    if plan.explanation:
        header += f": {plan.explanation}"
    else:
        header += ":"
    entries = [f"{markers[item.status]} {item.step}" for item in plan.items]
    if multiline:
        return "\n".join((header, *(f"  {entry}" for entry in entries)))
    return " | ".join((header, *entries))


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"UI event {label} must be an integer.")
    return value


def _output_stream(
    stream: TextIO | None,
    default: TextIO,
    label: str,
) -> TextIO:
    selected = default if stream is None else stream
    if not callable(getattr(selected, "write", None)) or not callable(
        getattr(selected, "flush", None)
    ):
        raise TypeError(f"{label} must provide write() and flush().")
    return selected


def _input_stream(stream: TextIO | None, default: TextIO) -> TextIO:
    selected = default if stream is None else stream
    if not callable(getattr(selected, "readline", None)):
        raise TypeError("stdin must provide readline().")
    return selected


def _is_broken_pipe(exc: OSError) -> bool:
    return isinstance(exc, BrokenPipeError) or exc.errno == errno.EPIPE


def _safe_terminal_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        character
        if character in {"\n", "\t"} or 32 <= ord(character) < 127
        or ord(character) >= 160
        else f"\\x{ord(character):02x}"
        for character in normalized
    )

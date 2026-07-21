from __future__ import annotations

import io
import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, cast

import pytest

from coding_agent.agent import (
    AgentRunReport,
    resume_agent_with_report,
    run_agent_with_report,
)
from coding_agent.prompts import build_system_prompt
from coding_agent.reviews import (
    REVIEW_MAX_DETAIL_CHARS,
    REVIEW_MAX_FINDINGS,
    REVIEW_MAX_SUMMARY_CHARS,
    REVIEW_MAX_TITLE_CHARS,
    ReviewFinding,
    ReviewResult,
    review_result_from_dict,
    review_result_to_dict,
)
from coding_agent.sessions.codec import (
    checkpoint_from_dict,
    checkpoint_to_dict,
)
from coding_agent.sessions.reducer import rebuild_state
from coding_agent.sessions.replay import build_session_replay_payload
from coding_agent.sessions.store import SessionStore
from coding_agent.task_modes import (
    READ_ONLY_TASK_TOOL_NAMES,
    TASK_MODE_PROFILES,
    filter_tool_definitions,
    task_mode_prompt_fragment,
)
from coding_agent.tool_policy import get_tool_policy
from coding_agent.tools import TOOL_DEFINITIONS, execute_tool
from coding_agent.types import AgentConfig
from coding_agent.ui import JsonlRenderer, TerminalRenderer, UiEmitter, UiEvent


def _config(workspace: Path, *, task_mode: str = "review") -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace.resolve()),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="read-only",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=10,
        context_max_bytes_per_file=8_000,
        sandbox_mode="none",
        full_auto=False,
        task_mode=cast(Any, task_mode),
    )


def _submission(
    *,
    summary: str = "Reviewed the current Git diff.",
    findings: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "summary": summary,
        "findings": findings if findings is not None else [],
    }


def _finding(
    *,
    severity: str = "high",
    path: str = "example.py",
    line: int = 2,
    title: str = "Unchecked operation",
    detail: str = "Validate the operation before applying it.",
) -> dict[str, object]:
    return {
        "severity": severity,
        "path": path,
        "line": line,
        "title": title,
        "detail": detail,
    }


def _tool_names(definitions: list[dict[str, Any]]) -> set[str]:
    return {cast(str, definition["name"]) for definition in definitions}


class _ReviewClient:
    def __init__(self, submission: dict[str, object]) -> None:
        self.submission = submission
        self.initial_calls = 0
        self.continuation_calls = 0
        self.tool_outputs: list[dict[str, Any]] | None = None

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        self.initial_calls += 1
        return {
            "id": "response-review",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-review",
                    "name": "submit_review",
                    "arguments": json.dumps(self.submission),
                }
            ],
        }

    def create_tool_response(
        self,
        *,
        tool_outputs: list[dict[str, Any]],
        **_kwargs: object,
    ) -> dict[str, object]:
        self.continuation_calls += 1
        self.tool_outputs = tool_outputs
        return {
            "id": "response-final",
            "output": [],
            "output_text": "Structured review submitted.",
        }


class _ContinuationOnlyClient:
    def __init__(self) -> None:
        self.initial_calls = 0
        self.continuation_calls = 0

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        self.initial_calls += 1
        raise AssertionError("resume must not repeat the initial model request")

    def create_tool_response(self, **_kwargs: object) -> dict[str, object]:
        self.continuation_calls += 1
        return {
            "id": "response-resumed-final",
            "output": [],
            "output_text": "Resumed structured review.",
        }


class _FinalOnlyClient:
    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        return {
            "id": "response-final",
            "output": [],
            "output_text": "free-form review only",
        }

    def create_tool_response(self, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("no tool continuation expected")


def test_review_models_are_strict_immutable_and_deduplicate() -> None:
    result = review_result_from_dict(
        _submission(
            summary="  reviewed  ",
            findings=[
                _finding(path="src\\example.py", title="  duplicate  "),
                _finding(path="src/example.py", title="duplicate"),
            ],
        )
    )

    assert result.summary == "reviewed"
    assert len(result.findings) == 1
    assert result.findings[0].path == "src/example.py"
    assert result.findings[0].title == "duplicate"
    assert review_result_from_dict(review_result_to_dict(result)) == result
    with pytest.raises(FrozenInstanceError):
        result.summary = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.findings[0].line = 3  # type: ignore[misc]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"summary": "ok", "findings": [], "extra": True}, "unknown"),
        (_submission(summary="   "), "summary"),
        (_submission(findings=[_finding(severity="urgent")]), "severity"),
        (_submission(findings=[_finding(line=0)]), "positive"),
        (_submission(findings=[_finding(path="../outside.py")]), "parent"),
        (
            _submission(findings=[_finding(title="x" * (REVIEW_MAX_TITLE_CHARS + 1))]),
            "title",
        ),
        (
            _submission(findings=[_finding(detail="x" * (REVIEW_MAX_DETAIL_CHARS + 1))]),
            "detail",
        ),
        (
            _submission(summary="x" * (REVIEW_MAX_SUMMARY_CHARS + 1)),
            "summary",
        ),
        (
            _submission(
                findings=[_finding(title=f"finding {index}") for index in range(REVIEW_MAX_FINDINGS + 1)]
            ),
            "at most",
        ),
    ],
)
def test_review_models_reject_invalid_payloads(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        review_result_from_dict(payload)


def test_submit_review_schema_and_policy_are_review_only() -> None:
    schema = next(
        item for item in TOOL_DEFINITIONS if item["name"] == "submit_review"
    )
    parameters = schema["parameters"]
    assert parameters["additionalProperties"] is False
    assert parameters["required"] == ["summary", "findings"]
    assert parameters["properties"]["findings"]["maxItems"] == 50

    assert TASK_MODE_PROFILES["review"].allowed_tools == (
        READ_ONLY_TASK_TOOL_NAMES | {"submit_review"}
    )
    assert "submit_review" not in TASK_MODE_PROFILES["run"].allowed_tools
    assert "submit_review" not in TASK_MODE_PROFILES["explain"].allowed_tools
    assert "submit_review" in _tool_names(
        filter_tool_definitions("review", TOOL_DEFINITIONS)
    )
    assert "submit_review" not in _tool_names(
        filter_tool_definitions("run", TOOL_DEFINITIONS)
    )
    policy = get_tool_policy("submit_review")
    assert policy.effect == "session_only"
    assert policy.approval_required is False


@pytest.mark.parametrize("task_mode", ["run", "explain"])
def test_submit_review_dispatch_is_hard_rejected_outside_review(
    tmp_path: Path,
    task_mode: str,
) -> None:
    result = execute_tool(
        _config(tmp_path, task_mode=task_mode),
        "submit_review",
        json.dumps(_submission()),
    )

    assert result.ok is False
    assert f"not allowed in {task_mode} task mode" in result.output


def test_submit_review_validates_locations_and_returns_no_file_text(
    tmp_path: Path,
) -> None:
    secret_marker = "file-body-must-not-be-returned"
    (tmp_path / "example.py").write_text(
        f"first\n{secret_marker}\nthird\n",
        encoding="utf-8",
    )
    payload = _submission(
        findings=[
            _finding(path=".\\example.py", line=2),
            _finding(path="example.py", line=2),
        ]
    )

    result = execute_tool(
        _config(tmp_path),
        "submit_review",
        json.dumps(payload),
    )

    assert result.ok is True
    assert result.data is not None
    review = review_result_from_dict(cast(Any, result.data["review"]))
    assert len(review.findings) == 1
    assert review.findings[0].path == "example.py"
    assert secret_marker not in result.output
    assert secret_marker not in json.dumps(result.data)

    duplicate = execute_tool(
        _config(tmp_path),
        "submit_review",
        json.dumps(_submission()),
        review_result=review,
    )
    assert duplicate.ok is False
    assert "already" in duplicate.output


@pytest.mark.parametrize(
    ("path", "content", "binary", "line", "message"),
    [
        ("missing.py", None, False, 1, "does not exist"),
        ("folder", None, False, 1, "existing file"),
        ("image.png", b"png", True, 1, "text file"),
        ("invalid.txt", b"\xff\xfe", True, 1, "UTF-8"),
        ("short.py", "one\n", False, 2, "exceeds"),
        (".env", "SECRET=value\n", False, 1, "sensitive"),
    ],
)
def test_submit_review_rejects_invalid_target_files(
    tmp_path: Path,
    path: str,
    content: str | bytes | None,
    binary: bool,
    line: int,
    message: str,
) -> None:
    target = tmp_path / path
    if path == "folder":
        target.mkdir()
    elif content is not None:
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")

    result = execute_tool(
        _config(tmp_path),
        "submit_review",
        json.dumps(_submission(findings=[_finding(path=path, line=line)])),
    )

    assert result.ok is False
    assert message.lower() in result.output.lower()


def test_submit_review_rejects_symlink_escape_when_supported(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    link = tmp_path / "link.py"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    result = execute_tool(
        _config(tmp_path),
        "submit_review",
        json.dumps(_submission(findings=[_finding(path="link.py", line=1)])),
    )

    assert result.ok is False
    assert "workspace" in result.output.lower() or "link" in result.output.lower()


def test_empty_review_submission_is_valid(tmp_path: Path) -> None:
    result = execute_tool(
        _config(tmp_path),
        "submit_review",
        json.dumps(_submission(summary="No findings in the current Git diff.")),
    )

    assert result.ok is True
    assert result.data is not None
    review = review_result_from_dict(cast(Any, result.data["review"]))
    assert review.findings == ()


def test_agent_persists_and_emits_structured_review(tmp_path: Path) -> None:
    (tmp_path / "example.py").write_text("one\ntwo\n", encoding="utf-8")
    submission = _submission(findings=[_finding()])
    client = _ReviewClient(submission)
    store = SessionStore(tmp_path)
    events: list[UiEvent] = []

    report = run_agent_with_report(
        "review changes",
        _config(tmp_path),
        model_client=client,
        session_store=store,
        ui_emitter=UiEmitter(events.append),
        stream=False,
    )

    assert isinstance(report, AgentRunReport)
    assert report.review is not None
    assert report.review.findings[0].path == "example.py"
    assert report.session_id is not None
    persisted = store.load(report.session_id)
    state = rebuild_state(persisted)
    assert state.review == report.review
    tool_finished = next(event for event in persisted if event.type == "tool.finished")
    assert review_result_from_dict(
        cast(Any, tool_finished.payload["review"])
    ) == report.review
    completed = next(event for event in persisted if event.type == "session.completed")
    assert review_result_from_dict(
        cast(Any, completed.payload["report"]["review"])
    ) == report.review
    run_finished = next(event for event in events if event.type == "run.finished")
    assert review_result_from_dict(
        cast(Any, run_finished.payload["review"])
    ) == report.review
    checkpoint = checkpoint_to_dict(state.to_checkpoint())
    assert checkpoint_from_dict(checkpoint).review == report.review
    legacy = dict(checkpoint)
    legacy.pop("review")
    assert checkpoint_from_dict(legacy).review is None


def test_review_mode_rejects_free_text_without_submit_review(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)

    with pytest.raises(RuntimeError, match="requires one successful submit_review"):
        run_agent_with_report(
            "review",
            _config(tmp_path),
            model_client=_FinalOnlyClient(),
            session_store=store,
            stream=False,
        )

    session_id = store.list_sessions()[0].session_id
    assert store.load(session_id)[-1].type == "session.failed"
    assert rebuild_state(store.load(session_id)).review is None


@pytest.mark.parametrize(
    ("fault_point", "expected_initial_calls"),
    [
        ("after_tool_side_effect", 0),
        ("after_tool_finished", 0),
    ],
)
def test_review_resume_safely_retries_or_reuses_durable_submission(
    tmp_path: Path,
    fault_point: str,
    expected_initial_calls: int,
) -> None:
    (tmp_path / "example.py").write_text("one\ntwo\n", encoding="utf-8")
    store = SessionStore(tmp_path)
    initial_client = _ReviewClient(_submission(findings=[_finding()]))

    def fail(point: str) -> None:
        if point == fault_point:
            raise RuntimeError(f"crash at {point}")

    with pytest.raises(RuntimeError, match="crash at"):
        run_agent_with_report(
            "review",
            _config(tmp_path),
            model_client=initial_client,
            session_store=store,
            fault_injector=cast(Any, fail),
            stream=False,
        )

    session_id = store.list_sessions()[0].session_id
    before = rebuild_state(store.load(session_id))
    assert (before.review is None) is (fault_point == "after_tool_side_effect")

    resumed_client = _ContinuationOnlyClient()
    report = resume_agent_with_report(
        session_id,
        tmp_path,
        model_client=resumed_client,
        session_store=store,
        stream=False,
    )

    assert report.review is not None
    assert resumed_client.initial_calls == expected_initial_calls
    assert resumed_client.continuation_calls == 1
    successful_submissions = [
        event
        for event in store.load(session_id)
        if event.type == "tool.finished" and "review" in event.payload
    ]
    assert len(successful_submissions) == 1


def test_replay_projects_review_from_reducer_state(tmp_path: Path) -> None:
    (tmp_path / "example.py").write_text("one\ntwo\n", encoding="utf-8")
    store = SessionStore(tmp_path)
    report = run_agent_with_report(
        "review",
        _config(tmp_path),
        model_client=_ReviewClient(_submission(findings=[_finding()])),
        session_store=store,
        stream=False,
    )

    read_only = SessionStore(tmp_path, read_only=True)
    replay = build_session_replay_payload(read_only, report.session_id or "")

    assert replay["review"] == review_result_to_dict(cast(ReviewResult, report.review))


def test_jsonl_run_finished_contains_one_line_structured_review() -> None:
    stdout = io.StringIO()
    renderer = JsonlRenderer(stdout=stdout, stderr=io.StringIO())
    review = ReviewResult(
        summary="summary",
        findings=(
            ReviewFinding(
                severity="high",
                path="b.py",
                line=2,
                title="B",
                detail="detail B",
            ),
        ),
    )
    event = UiEvent(
        schema_version=1,
        seq=1,
        type="run.finished",
        payload={
            "status": "completed",
            "final_status": "not_run",
            "session_id": "session",
            "answer": "done",
            "review": review_result_to_dict(review),
        },
    )

    renderer(event)

    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    decoded = json.loads(lines[0])
    assert decoded["payload"]["review"] == review_result_to_dict(review)


def test_terminal_renderer_sorts_findings_by_severity_path_and_line() -> None:
    stdout = io.StringIO()
    renderer = TerminalRenderer(
        stdout=stdout,
        stderr=io.StringIO(),
        is_tty=True,
        color_enabled=False,
    )
    review = ReviewResult(
        summary="summary",
        findings=(
            ReviewFinding("low", "z.py", 9, "low", "low detail"),
            ReviewFinding("high", "b.py", 3, "high-b", "high b detail"),
            ReviewFinding("critical", "z.py", 7, "critical", "critical detail"),
            ReviewFinding("high", "a.py", 5, "high-a-5", "high a5 detail"),
            ReviewFinding("high", "a.py", 2, "high-a-2", "high a2 detail"),
        ),
    )
    event = UiEvent(
        schema_version=1,
        seq=1,
        type="run.finished",
        payload={
            "status": "completed",
            "final_status": "not_run",
            "answer": "",
            "review": review_result_to_dict(review),
        },
    )

    renderer(event)

    output = stdout.getvalue()
    markers = [
        "[critical] z.py:7",
        "[high] a.py:2",
        "[high] a.py:5",
        "[high] b.py:3",
        "[low] z.py:9",
    ]
    positions = [output.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_review_prompt_defines_git_scope_and_single_structured_submit(
    tmp_path: Path,
) -> None:
    fragment = task_mode_prompt_fragment("review")
    prompt = build_system_prompt(_config(tmp_path))

    assert fragment in prompt
    assert "current Git diff" in fragment
    assert "Without Git" in fragment
    assert "submit_review exactly once" in fragment
    assert "findings=[]" in fragment
    assert "free-form text" in fragment

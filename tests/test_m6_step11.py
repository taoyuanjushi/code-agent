from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, cast

import pytest

import coding_agent.tools as tools_module
from coding_agent.agent import (
    AgentRunReport,
    resume_agent_with_report,
    run_agent_with_report,
)
from coding_agent.explanations import (
    ExplanationReadEvidence,
    explanation_read_evidence_from_tool_data,
    explanation_read_evidence_list_from_dict,
    extract_explanation_citations,
    merge_explanation_read_evidence,
    validate_explanation_answer,
)
from coding_agent.prompts import build_system_prompt
from coding_agent.sessions.store import SessionStore
from coding_agent.task_modes import (
    READ_ONLY_TASK_TOOL_NAMES,
    TASK_MODE_PROFILES,
    filter_tool_definitions,
    task_mode_prompt_fragment,
)
from coding_agent.tools import TOOL_DEFINITIONS, execute_tool
from coding_agent.types import AgentConfig, ToolResult
from coding_agent.ui import TerminalRenderer, UiEmitter, UiEvent


def _config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace.resolve()),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="read-only",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=6,
        context_max_bytes_per_file=8_000,
        sandbox_mode="none",
        full_auto=False,
        task_mode="explain",
    )


def _tool_names(definitions: list[dict[str, Any]]) -> set[str]:
    return {cast(str, definition["name"]) for definition in definitions}


class _ToolThenAnswerClient:
    def __init__(
        self,
        *,
        name: str,
        arguments: dict[str, object],
        answer: str,
        reasoning_summary: str = "",
    ) -> None:
        self.name = name
        self.arguments = arguments
        self.answer = answer
        self.reasoning_summary = reasoning_summary
        self.initial_calls = 0
        self.continuation_calls = 0
        self.tool_outputs: list[dict[str, Any]] | None = None

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        self.initial_calls += 1
        return {
            "id": "response-tool",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-read",
                    "name": self.name,
                    "arguments": json.dumps(self.arguments),
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
        output: list[dict[str, object]] = []
        if self.reasoning_summary:
            output.append(
                {
                    "type": "reasoning",
                    "summary": [{"text": self.reasoning_summary}],
                }
            )
        return {
            "id": "response-final",
            "output": output,
            "output_text": self.answer,
        }


class _FinalAnswerClient:
    def __init__(self, answer: str, *, reasoning_summary: str = "") -> None:
        self.answer = answer
        self.reasoning_summary = reasoning_summary
        self.initial_calls = 0

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        self.initial_calls += 1
        output: list[dict[str, object]] = []
        if self.reasoning_summary:
            output.append(
                {
                    "type": "reasoning",
                    "summary": [{"text": self.reasoning_summary}],
                }
            )
        return {
            "id": "response-final",
            "output": output,
            "output_text": self.answer,
        }

    def create_tool_response(self, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("no tool continuation expected")


class _ForbiddenToolsClient:
    def __init__(self) -> None:
        self.continuation_calls = 0
        self.tool_outputs: list[dict[str, Any]] | None = None

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        return {
            "id": "response-forbidden",
            "output": [
                {
                    "type": "function_call",
                    "call_id": f"call-{index}",
                    "name": name,
                    "arguments": "{}",
                }
                for index, name in enumerate(
                    ("apply_patch", "run_command", "run_verification"),
                    start=1,
                )
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
            "output_text": "Repository evidence is insufficient to answer safely.",
        }


class _ContinuationOnlyClient:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.initial_calls = 0
        self.continuation_calls = 0

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        self.initial_calls += 1
        raise AssertionError("resume must not repeat the initial request")

    def create_tool_response(self, **_kwargs: object) -> dict[str, object]:
        self.continuation_calls += 1
        return {
            "id": "response-resumed-final",
            "output": [],
            "output_text": self.answer,
        }


def test_explain_profile_and_prompt_define_read_only_evidence_contract(
    tmp_path: Path,
) -> None:
    profile = TASK_MODE_PROFILES["explain"]
    prompt = task_mode_prompt_fragment("explain")
    system_prompt = build_system_prompt(_config(tmp_path))

    assert profile.allowed_tools == READ_ONLY_TASK_TOOL_NAMES
    assert profile.workspace_write_allowed is False
    assert profile.general_processes_allowed is False
    assert "update_plan" in profile.allowed_tools
    assert "submit_review" not in profile.allowed_tools
    assert _tool_names(filter_tool_definitions("explain", TOOL_DEFINITIONS)) == set(
        READ_ONLY_TASK_TOOL_NAMES
    )
    for phrase in (
        "Search and read evidence first",
        "`path:line`",
        "returned by read_file or read_many_files",
        "evidence is insufficient",
        "not a reasoning summary",
        "Do not edit, run processes, or request approvals",
    ):
        assert phrase in prompt
    assert prompt in system_prompt


def test_explanation_evidence_is_strict_merged_and_citation_aware() -> None:
    merged = merge_explanation_read_evidence(
        (
            ExplanationReadEvidence("src\\example.py", 2, True),
            ExplanationReadEvidence("src/example.py", 5, False),
        )
    )

    assert merged == (ExplanationReadEvidence("src/example.py", 5, False),)
    citations = extract_explanation_citations(
        "See `src/example.py:3` and src/example.py:3."
    )
    assert len(citations) == 1
    assert citations[0].path == "src/example.py"
    assert citations[0].line == 3
    assert validate_explanation_answer("See src/example.py:5.", merged) == (
        citations[0].__class__("src/example.py", 5),
    )


@pytest.mark.parametrize("tool_name", ["read_file", "read_many_files"])
def test_read_tools_return_structured_line_bounded_evidence(
    tmp_path: Path,
    tool_name: str,
) -> None:
    (tmp_path / "ok.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    arguments: dict[str, object]
    if tool_name == "read_file":
        arguments = {"path": "ok.py", "max_bytes": 7}
    else:
        arguments = {"paths": ["ok.py", "missing.py"]}

    result = execute_tool(_config(tmp_path), tool_name, json.dumps(arguments))

    assert result.ok is True
    evidence = explanation_read_evidence_from_tool_data(result.data)
    assert [item.path for item in evidence] == ["ok.py"]
    assert evidence[0].max_line == (2 if tool_name == "read_file" else 3)
    assert evidence[0].truncated is (tool_name == "read_file")


def test_explain_agent_persists_read_evidence_and_returns_plain_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")
    answer = "The second line contains the relevant value (`src/example.py:2`)."
    client = _ToolThenAnswerClient(
        name="read_file",
        arguments={"path": "src/example.py"},
        answer=answer,
    )
    store = SessionStore(tmp_path)

    report = run_agent_with_report(
        "explain the value",
        _config(tmp_path),
        model_client=client,
        session_store=store,
        stream=False,
    )

    assert isinstance(report, AgentRunReport)
    assert report.answer == answer
    assert not hasattr(report, "explanation")
    assert report.review is None
    assert report.session_id is not None
    finished = next(
        event
        for event in store.load(report.session_id)
        if event.type == "tool.finished"
    )
    evidence = explanation_read_evidence_list_from_dict(
        finished.payload["read_evidence"]
    )
    assert evidence == (
        ExplanationReadEvidence("src/example.py", 3, False),
    )


def test_read_many_only_persists_successful_files_as_explain_evidence(
    tmp_path: Path,
) -> None:
    (tmp_path / "ok.py").write_text("value = 1\n", encoding="utf-8")
    client = _ToolThenAnswerClient(
        name="read_many_files",
        arguments={"paths": ["ok.py", "missing.py"]},
        answer="The value is assigned directly (`ok.py:1`).",
    )
    store = SessionStore(tmp_path)

    report = run_agent_with_report(
        "explain",
        _config(tmp_path),
        model_client=client,
        session_store=store,
        stream=False,
    )

    assert report.session_id is not None
    finished = next(
        event
        for event in store.load(report.session_id)
        if event.type == "tool.finished"
    )
    evidence = explanation_read_evidence_list_from_dict(
        finished.payload["read_evidence"]
    )
    assert [item.path for item in evidence] == ["ok.py"]


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (
            "The behavior is elsewhere (`unread.py:1`).",
            "was not successfully read",
        ),
        (
            "The behavior is on `example.py:4`.",
            "line exceeds read evidence",
        ),
        (
            "The file defines the behavior.",
            "must cite at least one",
        ),
    ],
)
def test_explain_final_answer_rejects_unverified_file_claims(
    tmp_path: Path,
    answer: str,
    message: str,
) -> None:
    (tmp_path / "example.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    store = SessionStore(tmp_path)

    with pytest.raises(ValueError, match=message):
        run_agent_with_report(
            "explain",
            _config(tmp_path),
            model_client=_ToolThenAnswerClient(
                name="read_file",
                arguments={"path": "example.py"},
                answer=answer,
            ),
            session_store=store,
            stream=False,
        )

    session_id = store.list_sessions()[0].session_id
    assert store.load(session_id)[-1].type == "session.failed"


def test_explain_allows_explicit_insufficient_evidence_without_fake_citation(
    tmp_path: Path,
) -> None:
    answer = "Repository evidence is insufficient to answer without guessing."

    report = run_agent_with_report(
        "explain unavailable facts",
        _config(tmp_path),
        model_client=_FinalAnswerClient(answer),
        session_store=SessionStore(tmp_path),
        stream=False,
    )

    assert report.answer == answer


def test_explain_hides_reasoning_summary_from_ui_but_keeps_durable_response(
    tmp_path: Path,
) -> None:
    answer = "Repository evidence is insufficient to answer safely."
    reasoning = "Internal reasoning summary that must stay out of explain UI."
    stdout = io.StringIO()
    stderr = io.StringIO()
    renderer = TerminalRenderer(
        stdout=stdout,
        stderr=stderr,
        is_tty=False,
        color_enabled=False,
    )
    ui_events: list[UiEvent] = []

    def handle(event: UiEvent) -> None:
        ui_events.append(event)
        renderer(event)

    store = SessionStore(tmp_path)
    report = run_agent_with_report(
        "explain",
        _config(tmp_path),
        model_client=_FinalAnswerClient(
            answer,
            reasoning_summary=reasoning,
        ),
        session_store=store,
        ui_emitter=UiEmitter(handle),
        stream=False,
    )

    model_finished = next(
        event for event in ui_events if event.type == "model.finished"
    )
    assert model_finished.payload["reasoning_summary"] == ""
    assert reasoning not in stdout.getvalue()
    assert "reasoning summary:" not in stdout.getvalue()
    assert answer in stdout.getvalue()
    assert report.session_id is not None
    responded = next(
        event
        for event in store.load(report.session_id)
        if event.type == "model.responded"
    )
    response = cast(Any, responded.payload["response"])
    assert response["reasoning_summary"] == reasoning


def test_explain_forbidden_model_calls_stop_before_implementation_and_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    implementation_calls = {
        "apply_patch": 0,
        "run_command": 0,
        "run_verification": 0,
    }

    def forbidden_implementation(name: str):
        def invoke(*_args: object, **_kwargs: object) -> ToolResult:
            implementation_calls[name] += 1
            return ToolResult(ok=True, output="unexpected")

        return invoke

    monkeypatch.setattr(
        tools_module,
        "_apply_patch_tool",
        forbidden_implementation("apply_patch"),
    )
    monkeypatch.setattr(
        tools_module,
        "_run_command_tool",
        forbidden_implementation("run_command"),
    )
    monkeypatch.setattr(
        tools_module,
        "_run_verification_tool",
        forbidden_implementation("run_verification"),
    )
    approval_calls = 0

    def approval_handler(_request: object) -> object:
        nonlocal approval_calls
        approval_calls += 1
        raise AssertionError("explain mode must not request approval")

    client = _ForbiddenToolsClient()
    report = run_agent_with_report(
        "explain",
        _config(tmp_path),
        model_client=client,
        session_store=SessionStore(tmp_path),
        approval_handler=cast(Any, approval_handler),
        stream=False,
    )

    assert report.answer.startswith("Repository evidence is insufficient")
    assert implementation_calls == {
        "apply_patch": 0,
        "run_command": 0,
        "run_verification": 0,
    }
    assert approval_calls == 0
    assert client.continuation_calls == 1
    assert client.tool_outputs is not None
    assert all(
        "not allowed in explain task mode" in output["output"]
        for output in client.tool_outputs
    )


def test_explain_resume_reuses_durable_read_evidence_for_final_validation(
    tmp_path: Path,
) -> None:
    (tmp_path / "example.py").write_text("one\ntwo\n", encoding="utf-8")
    store = SessionStore(tmp_path)

    def crash_after_finished(point: str) -> None:
        if point == "after_tool_finished":
            raise RuntimeError("crash after durable read")

    with pytest.raises(RuntimeError, match="crash after durable read"):
        run_agent_with_report(
            "explain",
            _config(tmp_path),
            model_client=_ToolThenAnswerClient(
                name="read_file",
                arguments={"path": "example.py"},
                answer="unused",
            ),
            session_store=store,
            fault_injector=cast(Any, crash_after_finished),
            stream=False,
        )

    session_id = store.list_sessions()[0].session_id
    resumed = _ContinuationOnlyClient(
        "The second line is part of the file (`example.py:2`)."
    )
    report = resume_agent_with_report(
        session_id,
        tmp_path,
        model_client=resumed,
        session_store=store,
        stream=False,
    )

    assert report.answer.endswith("(`example.py:2`).")
    assert resumed.initial_calls == 0
    assert resumed.continuation_calls == 1

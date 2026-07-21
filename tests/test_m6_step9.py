from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

import coding_agent.tools as tools_module
from coding_agent.agent import (
    SessionResumeError,
    resume_agent_with_report,
    run_agent_with_report,
)
from coding_agent.cli import CliUsageError, _select_mode, build_parser
from coding_agent.config import load_config
from coding_agent.model_client import OpenAIResponsesClient
from coding_agent.prompts import build_system_prompt
from coding_agent.sessions.codec import session_started_to_dict
from coding_agent.sessions.models import SessionStarted, WorkspaceGuard
from coding_agent.sessions.privacy import SessionPrivacyPolicy
from coding_agent.sessions.store import SessionStore
from coding_agent.sessions.workspace_guard import discover_git_head
from coding_agent.task_modes import (
    READ_ONLY_TASK_TOOL_NAMES,
    TASK_MODES,
    TASK_MODE_PROFILES,
    filter_tool_definitions,
    task_mode_prompt_fragment,
)
from coding_agent.tools import TOOL_DEFINITIONS, execute_tool
from coding_agent.types import AgentConfig


def _config(
    workspace: Path,
    *,
    task_mode: str = "run",
    permission_mode: str = "read-only",
    auto_approve_edits: bool = False,
    auto_approve_commands: bool = False,
    full_auto: bool = False,
) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace.resolve()),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode=cast(Any, permission_mode),
        auto_approve_commands=auto_approve_commands,
        auto_approve_edits=auto_approve_edits,
        context_max_files=6,
        context_max_bytes_per_file=8_000,
        sandbox_mode="none",
        full_auto=full_auto,
        task_mode=cast(Any, task_mode),
    )


def _tool_names(definitions: list[dict[str, Any]]) -> set[str]:
    return {cast(str, definition["name"]) for definition in definitions}


class _FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **request: Any) -> dict[str, object]:
        self.calls.append(request)
        return {"id": f"response-{len(self.calls)}", "output": [], "output_text": "done"}


class _FakeOpenAI:
    def __init__(self, responses: _FakeResponses) -> None:
        self.responses = responses


class _FinalClient:
    def __init__(self) -> None:
        self.configs: list[AgentConfig] = []
        self.initial_calls = 0

    def create_initial_response(
        self,
        *,
        config: AgentConfig,
        **_kwargs: object,
    ) -> dict[str, object]:
        self.initial_calls += 1
        self.configs.append(config)
        if config.task_mode == "review":
            return {
                "id": "response-review",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call-review",
                        "name": "submit_review",
                        "arguments": json.dumps(
                            {
                                "summary": "No findings in the requested scope.",
                                "findings": [],
                            }
                        ),
                    }
                ],
            }
        return {"id": "response-final", "output": [], "output_text": "done"}

    def create_tool_response(self, **_kwargs: object) -> dict[str, object]:
        return {"id": "response-final", "output": [], "output_text": "done"}


class _ForbiddenToolsClient:
    def __init__(self) -> None:
        self.tool_outputs: list[dict[str, Any]] | None = None
        self.continuation_calls = 0

    def create_initial_response(self, **_kwargs: object) -> dict[str, object]:
        names = ("apply_patch", "run_command", "run_verification")
        return {
            "id": "response-forbidden-tools",
            "output": [
                {
                    "type": "function_call",
                    "call_id": f"call-{index}",
                    "name": name,
                    "arguments": "{}",
                }
                for index, name in enumerate(names, start=1)
            ],
        }

    def create_tool_response(
        self,
        *,
        tool_outputs: list[dict[str, Any]],
        **_kwargs: object,
    ) -> dict[str, object]:
        self.continuation_calls += 1
        if self.continuation_calls == 1:
            self.tool_outputs = tool_outputs
            return {
                "id": "response-submit-review",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call-submit-review",
                        "name": "submit_review",
                        "arguments": json.dumps(
                            {
                                "summary": "No findings in the requested scope.",
                                "findings": [],
                            }
                        ),
                    }
                ],
            }
        return {"id": "response-final", "output": [], "output_text": "reviewed"}


def _session_started(
    workspace: Path,
    config: AgentConfig,
    *,
    omit_task_mode: bool = False,
    persisted_task_mode: object | None = None,
) -> SessionStarted:
    resolved = workspace.resolve()
    persisted_config = SessionPrivacyPolicy().sanitize_config(config)
    if omit_task_mode:
        persisted_config.pop("task_mode")
    elif persisted_task_mode is not None:
        persisted_config["task_mode"] = persisted_task_mode
    git_head = discover_git_head(resolved)
    guard = WorkspaceGuard(
        workspace=str(resolved),
        git_head=git_head,
        touched_file_hashes={},
    )
    return SessionStarted(
        task="inspect the repository",
        workspace=str(resolved),
        config=persisted_config,
        git_head=git_head,
        workspace_guard=guard,
    )


def test_task_mode_profiles_are_explicit_and_match_current_tools() -> None:
    all_tools = _tool_names(TOOL_DEFINITIONS)

    assert TASK_MODES == {"run", "review", "explain"}
    assert TASK_MODE_PROFILES["run"].allowed_tools == all_tools - {"submit_review"}
    assert TASK_MODE_PROFILES["run"].workspace_write_allowed is True
    assert TASK_MODE_PROFILES["run"].general_processes_allowed is True
    assert TASK_MODE_PROFILES["review"].allowed_tools == (
        READ_ONLY_TASK_TOOL_NAMES | {"submit_review"}
    )
    assert TASK_MODE_PROFILES["explain"].allowed_tools == READ_ONLY_TASK_TOOL_NAMES
    assert TASK_MODE_PROFILES["review"].workspace_write_allowed is False
    assert TASK_MODE_PROFILES["explain"].general_processes_allowed is False
    assert "submit_review" in TASK_MODE_PROFILES["review"].allowed_tools


@pytest.mark.parametrize("task_mode", ["review", "explain"])
def test_restricted_mode_schemas_expose_only_read_only_profile(
    task_mode: str,
) -> None:
    names = _tool_names(filter_tool_definitions(task_mode, TOOL_DEFINITIONS))

    expected = READ_ONLY_TASK_TOOL_NAMES
    if task_mode == "review":
        expected = expected | {"submit_review"}
    assert names == expected
    assert names.isdisjoint(
        {
            "apply_patch",
            "discover_verification_commands",
            "run_verification",
            "run_command",
        }
    )


def test_model_client_filters_initial_and_continuation_tool_schemas(
    tmp_path: Path,
) -> None:
    responses = _FakeResponses()
    client = OpenAIResponsesClient(
        _FakeOpenAI(responses),  # type: ignore[arg-type]
        stream=False,
    )

    client.create_initial_response(
        config=_config(tmp_path, task_mode="review"),
        instructions="review",
        input_text="task",
    )
    client.create_tool_response(
        config=_config(tmp_path, task_mode="explain"),
        previous_response_id="response-1",
        tool_outputs=[],
    )

    assert _tool_names(responses.calls[0]["tools"]) == (
        READ_ONLY_TASK_TOOL_NAMES | {"submit_review"}
    )
    assert _tool_names(responses.calls[1]["tools"]) == READ_ONLY_TASK_TOOL_NAMES


@pytest.mark.parametrize("task_mode", ["run", "review", "explain"])
def test_each_task_mode_has_a_short_prompt_fragment(
    tmp_path: Path,
    task_mode: str,
) -> None:
    fragment = task_mode_prompt_fragment(task_mode)
    prompt = build_system_prompt(_config(tmp_path, task_mode=task_mode))

    assert fragment in prompt
    assert f"Task mode: {task_mode}." in fragment
    assert len(fragment) < 400
    assert "Command policy is evaluated before approval" in prompt
    if task_mode == "review":
        assert "read-only code review" in fragment
        assert "current Git diff" in fragment
    if task_mode == "explain":
        assert "path:line" in fragment
        assert "evidence is insufficient" in fragment


def test_cli_accepts_task_modes_and_defaults_config_to_run() -> None:
    parser = build_parser()

    assert load_config(parser.parse_args(["inspect"])).task_mode == "run"
    for task_mode in TASK_MODES:
        args = parser.parse_args(["--mode", task_mode, "inspect"])
        assert args.task_mode == task_mode
        assert load_config(args).task_mode == task_mode
        assert _select_mode(args) == "new"


@pytest.mark.parametrize("task_mode", ["review", "explain"])
@pytest.mark.parametrize(
    "flag",
    [
        "--write",
        "--auto-approve-edits",
        "--auto-approve-commands",
        "--full-auto",
    ],
)
def test_restricted_cli_modes_reject_write_and_auto_options(
    task_mode: str,
    flag: str,
) -> None:
    args = build_parser().parse_args(["--mode", task_mode, flag, "inspect"])

    with pytest.raises(CliUsageError, match=f"--mode {task_mode}"):
        _select_mode(args)


def test_resume_rejects_an_explicit_task_mode_override() -> None:
    args = build_parser().parse_args(["--resume", "latest", "--mode", "review"])

    with pytest.raises(CliUsageError, match="--mode"):
        _select_mode(args)


@pytest.mark.parametrize("task_mode", ["run", "review", "explain"])
def test_new_session_persists_task_mode(
    tmp_path: Path,
    task_mode: str,
) -> None:
    store = SessionStore(tmp_path)

    report = run_agent_with_report(
        "inspect",
        _config(tmp_path, task_mode=task_mode),
        model_client=_FinalClient(),
        session_store=store,
        stream=False,
    )

    started = store.load(report.session_id or "")[0]
    assert started.type == "session.started"
    assert started.payload["config"]["task_mode"] == task_mode


@pytest.mark.parametrize(
    ("persisted_mode", "expected_mode"),
    [("review", "review"), ("explain", "explain"), (None, "run")],
)
def test_resume_uses_persisted_mode_and_old_sessions_default_to_run(
    tmp_path: Path,
    persisted_mode: str | None,
    expected_mode: str,
) -> None:
    store = SessionStore(tmp_path)
    config = _config(tmp_path, task_mode=persisted_mode or "run")
    started = _session_started(
        tmp_path,
        config,
        omit_task_mode=persisted_mode is None,
    )
    session_id = store.create(session_started_to_dict(started))
    client = _FinalClient()

    report = resume_agent_with_report(
        session_id,
        tmp_path,
        model_client=client,
        session_store=store,
        stream=False,
    )

    assert report.answer == "done"
    assert client.configs[0].task_mode == expected_mode


def test_resume_rejects_invalid_persisted_task_mode_before_model_call(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    started = _session_started(
        tmp_path,
        _config(tmp_path),
        persisted_task_mode="unsafe",
    )
    session_id = store.create(session_started_to_dict(started))
    client = _FinalClient()

    with pytest.raises(SessionResumeError, match="persisted task mode"):
        resume_agent_with_report(
            session_id,
            tmp_path,
            model_client=client,
            session_store=store,
            stream=False,
        )

    assert client.initial_calls == 0
    events = store.load(session_id)
    assert events[-1].type == "session.failed"
    assert events[-1].payload["reason"] == "preflight_failure"


def test_resume_rejects_restricted_mode_with_persisted_write_permission(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    invalid_config = _config(
        tmp_path,
        task_mode="review",
        permission_mode="workspace-write",
    )
    session_id = store.create(
        session_started_to_dict(_session_started(tmp_path, invalid_config))
    )
    client = _FinalClient()

    with pytest.raises(SessionResumeError, match="task mode configuration"):
        resume_agent_with_report(
            session_id,
            tmp_path,
            model_client=client,
            session_store=store,
            stream=False,
        )

    assert client.initial_calls == 0
    events = store.load(session_id)
    assert events[-1].type == "session.failed"
    assert events[-1].payload["reason"] == "preflight_failure"


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(
            lambda path: _config(
                path,
                task_mode="review",
                permission_mode="workspace-write",
            ),
            id="workspace-write",
        ),
        pytest.param(
            lambda path: _config(
                path,
                task_mode="review",
                auto_approve_edits=True,
            ),
            id="auto-edit",
        ),
        pytest.param(
            lambda path: _config(
                path,
                task_mode="explain",
                auto_approve_commands=True,
            ),
            id="auto-command",
        ),
        pytest.param(
            lambda path: _config(
                path,
                task_mode="explain",
                full_auto=True,
            ),
            id="full-auto",
        ),
    ],
)
def test_programmatic_restricted_config_is_rejected_before_session_or_model(
    tmp_path: Path,
    config: Any,
) -> None:
    store = SessionStore(tmp_path)
    client = _FinalClient()

    with pytest.raises(ValueError, match="Task mode"):
        run_agent_with_report(
            "inspect",
            config(tmp_path),
            model_client=client,
            session_store=store,
            stream=False,
        )

    assert client.initial_calls == 0
    assert store.list_sessions() == ()


@pytest.mark.parametrize("task_mode", ["review", "explain"])
@pytest.mark.parametrize(
    "tool_name",
    [
        "apply_patch",
        "discover_verification_commands",
        "run_verification",
        "run_command",
    ],
)
def test_execute_tool_rejects_profile_violations_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    task_mode: str,
    tool_name: str,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("A restricted tool reached its implementation.")

    monkeypatch.setattr(tools_module, "_apply_patch_tool", fail)
    monkeypatch.setattr(tools_module, "_discover_verification_tool", fail)
    monkeypatch.setattr(tools_module, "_run_verification_tool", fail)
    monkeypatch.setattr(tools_module, "_run_command_tool", fail)
    monkeypatch.setattr(tools_module, "build_default_approval_handler", fail)

    result = execute_tool(
        _config(tmp_path, task_mode=task_mode),
        tool_name,
        "{}",
    )

    assert result.ok is False
    assert task_mode in result.output
    assert "not allowed" in result.output
    assert result.data == {
        "type": "task_mode_policy_rejection",
        "task_mode": task_mode,
        "tool_name": tool_name,
        "status": "denied",
        "disposition": "deny",
        "requires_approval": False,
    }


@pytest.mark.parametrize("task_mode", ["review", "explain"])
def test_update_plan_remains_available_in_restricted_modes(
    tmp_path: Path,
    task_mode: str,
) -> None:
    result = execute_tool(
        _config(tmp_path, task_mode=task_mode),
        "update_plan",
        json.dumps(
            {
                "items": [
                    {"step": "inspect evidence", "status": "in_progress"},
                    {"step": "report result", "status": "pending"},
                ]
            }
        ),
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["type"] == "plan_update"


def test_agent_rejects_fake_forbidden_calls_without_approval_or_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("A restricted tool reached an effectful implementation.")

    monkeypatch.setattr(tools_module, "_apply_patch_tool", fail)
    monkeypatch.setattr(tools_module, "_run_verification_tool", fail)
    monkeypatch.setattr(tools_module, "_run_command_tool", fail)
    approvals = 0

    def approval_handler(_request: object) -> Any:
        nonlocal approvals
        approvals += 1
        raise AssertionError("Restricted tools must not request approval.")

    client = _ForbiddenToolsClient()
    store = SessionStore(tmp_path)
    report = run_agent_with_report(
        "review changes",
        _config(tmp_path, task_mode="review"),
        model_client=client,
        session_store=store,
        approval_handler=approval_handler,
        stream=False,
    )

    assert report.answer == "reviewed"
    assert approvals == 0
    assert client.tool_outputs is not None
    assert len(client.tool_outputs) == 3
    assert all(
        "not allowed in review task mode" in item["output"]
        for item in client.tool_outputs
    )
    assert report.session_id is not None
    finished_events = [
        event
        for event in store.load(report.session_id)
        if event.type == "tool.finished"
    ]
    denied_events = [
        event
        for event in finished_events
        if event.payload.get("execution", {}).get("status") == "denied"
    ]
    assert len(finished_events) == 4
    assert [event.payload["execution"] for event in denied_events] == [
        {
            "task_mode": "review",
            "tool_name": tool_name,
            "status": "denied",
            "disposition": "deny",
            "requires_approval": False,
        }
        for tool_name in ("apply_patch", "run_command", "run_verification")
    ]


def test_write_file_and_unknown_tool_keep_legacy_errors(tmp_path: Path) -> None:
    write_result = execute_tool(
        _config(tmp_path, task_mode="review"),
        "write_file",
        "{}",
    )
    unknown_result = execute_tool(
        _config(tmp_path, task_mode="review"),
        "future_tool",
        "not-json",
    )

    assert write_result.ok is False
    assert "write_file is disabled" in write_result.output
    assert unknown_result.output == "Unknown tool: future_tool"

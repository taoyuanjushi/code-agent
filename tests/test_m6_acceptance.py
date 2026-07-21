"""M6 product-experience acceptance contracts.

This module does not import future UI, plan, task-mode, or editor code. It
freezes the observable contracts that later M6 steps must bind to production.
"""

from __future__ import annotations

import json
from pathlib import Path

from coding_agent.sessions.models import SESSION_EVENT_TYPES
from coding_agent.tool_policy import TOOL_POLICIES

M6_UI_SCHEMA_VERSION = 1
M6_UI_EVENT_FIELDS = frozenset({"schema_version", "seq", "type", "payload"})
M6_UI_EVENT_TYPES = frozenset(
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

M6_OUTPUT_CHANNELS = {
    "human_output": "stdout",
    "jsonl_events": "stdout",
    "diagnostics": "stderr",
    "jsonl_approval_prompt": "stderr",
    "approval_input": "stdin",
}
M6_NON_TTY_FORBIDDEN_SEQUENCES = ("\x1b", "\r", "\b")
M6_COLOR_DISABLE_INPUTS = frozenset({"non_tty", "NO_COLOR", "--no-color"})

M6_STREAMING_PERSISTS_DELTAS = False
M6_FORBIDDEN_DURABLE_STREAM_EVENTS = frozenset(
    {"model.output.delta", "model.reasoning.delta", "ui.delta"}
)
M6_STREAM_FAILURE_AUTO_RETRY = False

M6_FORBIDDEN_UI_FIELDS = frozenset(
    {"environment", "authorization", "request_headers", "raw_sdk_response"}
)
M6_SENSITIVE_VALUE_FIXTURES = frozenset(
    {
        "sk-m6-acceptance-secret",
        "m6-token-value",
        "m6-secret-value",
        "m6-password-value",
    }
)

M6_TASK_MODES = frozenset({"run", "review", "explain"})
M6_READ_ONLY_GIT_TOOLS = frozenset({"git_status", "git_diff"})
M6_RESTRICTED_MODE_TOOLS = frozenset(
    {"apply_patch", "run_command", "run_verification"}
)
M6_RESTRICTED_MODE_EFFECT_LIMITS = {
    "workspace_write_calls": 0,
    "run_command_calls": 0,
    "run_verification_calls": 0,
    "input_calls": 0,
}

M6_PLAN_UPDATE_FIELDS = frozenset({"explanation", "items"})
M6_PLAN_ITEM_FIELDS = frozenset({"step", "status"})
M6_PLAN_STATUSES = frozenset({"pending", "in_progress", "completed"})
M6_PLAN_MIN_ITEMS = 1
M6_PLAN_MAX_ITEMS = 20
M6_PLAN_MAX_STEP_CHARS = 200
M6_PLAN_MAX_EXPLANATION_CHARS = 500
M6_PLAN_MAX_IN_PROGRESS = 1
M6_PLAN_REJECTS_UNKNOWN_FIELDS = True

M6_INTERRUPT_EXIT_CODE = 130
M6_INTERRUPT_UI_EVENT = "run.interrupted"
M6_INTERRUPT_SESSION_EVENT = "session.interrupted"

M6_VSCODE_EXECUTION_API = "ProcessExecution"
M6_VSCODE_USES_ARGV = True
M6_VSCODE_USES_SHELL = False
M6_VSCODE_FORBIDDEN_APIS = frozenset({"sendText", "shell: true"})

M6_DEFAULT_EXTERNAL_REQUIREMENTS = {
    "real_tty": False,
    "openai_api_key": False,
    "live_model": False,
    "docker": False,
    "node": False,
    "vscode": False,
}
M6_OPTIONAL_SMOKE_MARKERS = frozenset({"docker", "live_model", "vscode"})

M6_M1_TO_M5_BASELINE_COUNT = 640
M6_REQUIRED_REGRESSION_TESTS = frozenset(
    {
        "tests/test_integration.py",
        "tests/test_m2_integration.py",
        "tests/test_m3_acceptance.py",
        "tests/test_m3_integration.py",
        "tests/test_m4_acceptance.py",
        "tests/test_m4_integration.py",
        "tests/test_m5_acceptance.py",
        "tests/test_m5_integration.py",
    }
)
M6_REQUIRED_PRODUCT_TESTS = frozenset(
    {
        "tests/test_ui.py",
        "tests/test_model_streaming.py",
        "tests/test_m6_step8.py",
        "tests/test_task_modes.py",
        "tests/test_review_mode.py",
        "tests/test_explain_mode.py",
        "tests/test_m6_integration.py",
        "tests/test_cli_product.py",
        "tests/test_session_replay.py",
        "tests/test_m6_step14.py",
        "tests/test_m6_step15.py",
    }
)


def test_ui_event_schema_and_vocabulary_are_fixed() -> None:
    assert M6_UI_SCHEMA_VERSION == 1
    assert M6_UI_EVENT_FIELDS == {"schema_version", "seq", "type", "payload"}
    assert M6_UI_EVENT_TYPES == {
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
    events = [_reference_event(seq, event_type) for seq, event_type in enumerate(
        sorted(M6_UI_EVENT_TYPES),
        start=1,
    )]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert all(set(event) == M6_UI_EVENT_FIELDS for event in events)


def test_jsonl_is_one_complete_json_object_per_stdout_line() -> None:
    events = [
        _reference_event(1, "run.started", workspace="D:/code/coding"),
        _reference_event(2, "model.output.delta", text="你好"),
        _reference_event(3, "run.finished", status="completed"),
    ]

    rendered = _reference_jsonl(events)
    decoded = [json.loads(line) for line in rendered.splitlines()]

    assert decoded == events
    assert len(rendered.splitlines()) == len(events)
    assert "\x1b" not in rendered
    assert M6_OUTPUT_CHANNELS == {
        "human_output": "stdout",
        "jsonl_events": "stdout",
        "diagnostics": "stderr",
        "jsonl_approval_prompt": "stderr",
        "approval_input": "stdin",
    }


def test_non_tty_and_explicit_color_controls_disable_terminal_sequences() -> None:
    plain_output = "tool: read_file\nstatus: passed\n"

    assert M6_COLOR_DISABLE_INPUTS == {"non_tty", "NO_COLOR", "--no-color"}
    assert all(token not in plain_output for token in M6_NON_TTY_FORBIDDEN_SEQUENCES)


def test_streaming_deltas_reconstruct_the_normalized_final_text() -> None:
    deltas = ("流", "式", " ", "out", "put", " ✅")
    normalized_final_text = "流式 output ✅"

    assert "".join(deltas) == normalized_final_text
    assert M6_STREAM_FAILURE_AUTO_RETRY is False


def test_streaming_deltas_never_become_durable_session_events() -> None:
    assert M6_STREAMING_PERSISTS_DELTAS is False
    assert M6_FORBIDDEN_DURABLE_STREAM_EVENTS.isdisjoint(SESSION_EVENT_TYPES)
    assert "model.responded" in SESSION_EVENT_TYPES


def test_ui_contract_excludes_secret_values_and_raw_sensitive_fields() -> None:
    safe_payload = {
        "tool": "run_command",
        "status": "failed",
        "backend": "docker",
        "message": "credential was redacted",
    }
    encoded = json.dumps(safe_payload, ensure_ascii=False)

    assert M6_FORBIDDEN_UI_FIELDS.isdisjoint(safe_payload)
    assert all(secret not in encoded for secret in M6_SENSITIVE_VALUE_FIXTURES)


def test_review_and_explain_profiles_cannot_gain_write_or_process_tools() -> None:
    assert M6_TASK_MODES == {"run", "review", "explain"}
    assert all(
        TOOL_POLICIES[name].effect == "read_only"
        and TOOL_POLICIES[name].approval_required is False
        for name in M6_READ_ONLY_GIT_TOOLS
    )
    assert {TOOL_POLICIES[name].effect for name in M6_RESTRICTED_MODE_TOOLS} == {
        "workspace_write",
        "process",
    }
    assert all(limit == 0 for limit in M6_RESTRICTED_MODE_EFFECT_LIMITS.values())


def test_plan_contract_has_fixed_schema_vocabularies_and_budgets() -> None:
    assert M6_PLAN_UPDATE_FIELDS == {"explanation", "items"}
    assert M6_PLAN_ITEM_FIELDS == {"step", "status"}
    assert M6_PLAN_STATUSES == {"pending", "in_progress", "completed"}
    assert M6_PLAN_MIN_ITEMS == 1
    assert M6_PLAN_MAX_ITEMS == 20
    assert M6_PLAN_MAX_STEP_CHARS == 200
    assert M6_PLAN_MAX_EXPLANATION_CHARS == 500
    assert M6_PLAN_MAX_IN_PROGRESS == 1
    assert M6_PLAN_REJECTS_UNKNOWN_FIELDS is True


def test_keyboard_interrupt_has_stable_ui_session_and_exit_contracts() -> None:
    assert M6_INTERRUPT_EXIT_CODE == 130
    assert M6_INTERRUPT_UI_EVENT == "run.interrupted"
    assert M6_INTERRUPT_SESSION_EVENT == "session.interrupted"
    assert M6_INTERRUPT_SESSION_EVENT in SESSION_EVENT_TYPES


def test_vscode_contract_requires_process_execution_with_argv() -> None:
    assert M6_VSCODE_EXECUTION_API == "ProcessExecution"
    assert M6_VSCODE_USES_ARGV is True
    assert M6_VSCODE_USES_SHELL is False
    assert M6_VSCODE_FORBIDDEN_APIS == {"sendText", "shell: true"}


def test_default_tests_require_no_external_product_runtime() -> None:
    assert M6_DEFAULT_EXTERNAL_REQUIREMENTS == {
        "real_tty": False,
        "openai_api_key": False,
        "live_model": False,
        "docker": False,
        "node": False,
        "vscode": False,
    }
    assert M6_OPTIONAL_SMOKE_MARKERS == {"docker", "live_model", "vscode"}


def test_m1_to_m5_regressions_remain_part_of_m6_acceptance() -> None:
    repository_root = Path(__file__).resolve().parents[1]

    assert M6_M1_TO_M5_BASELINE_COUNT == 640
    assert all(
        (repository_root / relative_path).is_file()
        for relative_path in M6_REQUIRED_REGRESSION_TESTS
    )


def test_required_product_and_cross_platform_matrix_modules_exist() -> None:
    repository_root = Path(__file__).resolve().parents[1]

    assert all(
        (repository_root / relative_path).is_file()
        for relative_path in M6_REQUIRED_PRODUCT_TESTS
    )


def _reference_event(seq: int, event_type: str, **payload: object) -> dict[str, object]:
    return {
        "schema_version": M6_UI_SCHEMA_VERSION,
        "seq": seq,
        "type": event_type,
        "payload": payload,
    }


def _reference_jsonl(events: list[dict[str, object]]) -> str:
    return "".join(
        f"{json.dumps(event, ensure_ascii=False, sort_keys=True)}\n"
        for event in events
    )

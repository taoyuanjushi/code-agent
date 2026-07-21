"""Product and cross-platform acceptance matrix for M6 step fourteen."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

M6_STEP14_SCENARIOS = frozenset(
    {
        "tty_human_output",
        "redirected_output",
        "color_disabled",
        "live_jsonl",
        "unicode_delta_boundaries",
        "streaming_interrupt_recovery",
        "bounded_tool_output",
        "secret_redaction",
        "streaming_approval_adjacency",
        "invalid_plan_update",
        "resume_unfinished_plan",
        "review_side_effects",
        "review_line_drift",
        "explain_side_effects",
        "ctrl_c_model_and_tool",
        "windows_posix_argv",
        "legacy_session_compatibility",
        "m1_m5_regression",
    }
)

M6_STEP14_MATRIX: dict[str, tuple[str, ...]] = {
    "tty_human_output": (
        "tests/test_m6_integration.py::test_streaming_approval_and_secret_redaction_share_one_event_sequence",
    ),
    "redirected_output": (
        "tests/test_cli_product.py::test_redirected_human_cli_is_line_oriented_and_control_free",
    ),
    "color_disabled": (
        "tests/test_ui.py::test_terminal_renderer_honors_no_color_inputs",
    ),
    "live_jsonl": (
        "tests/test_cli_product.py::test_jsonl_cli_stdout_contains_only_complete_events",
    ),
    "unicode_delta_boundaries": (
        "tests/test_model_streaming.py::test_unicode_graphemes_survive_every_delta_boundary",
    ),
    "streaming_interrupt_recovery": (
        "tests/test_model_streaming.py::test_partial_stream_failure_is_persisted_without_retry",
        "tests/test_m6_integration.py::test_model_interrupt_is_durable_and_immediately_replayable",
    ),
    "bounded_tool_output": (
        "tests/test_m6_step6.py::test_tool_events_wrap_durable_completion_and_apply_console_budget",
    ),
    "secret_redaction": (
        "tests/test_m6_integration.py::test_streaming_approval_and_secret_redaction_share_one_event_sequence",
    ),
    "streaming_approval_adjacency": (
        "tests/test_m6_integration.py::test_streaming_approval_and_secret_redaction_share_one_event_sequence",
        "tests/test_m6_step6.py::test_approval_ui_order_tracks_the_durable_decision",
    ),
    "invalid_plan_update": (
        "tests/test_m6_step8.py::test_completed_plan_cannot_reopen_in_tool_or_reducer",
    ),
    "resume_unfinished_plan": (
        "tests/test_m6_step8.py::test_resume_restores_unfinished_plan_and_does_not_reexecute_completed_tool",
    ),
    "review_side_effects": (
        "tests/test_task_modes.py::test_restricted_product_modes_reject_before_approval_or_side_effect",
    ),
    "review_line_drift": (
        "tests/test_review_mode.py::test_review_submission_revalidates_line_numbers_after_file_drift",
    ),
    "explain_side_effects": (
        "tests/test_explain_mode.py::test_explain_model_write_and_command_requests_have_zero_side_effects",
    ),
    "ctrl_c_model_and_tool": (
        "tests/test_cli_product.py::test_jsonl_keyboard_interrupt_returns_130_with_one_terminal_event",
        "tests/test_m6_step12.py::test_host_process_interrupt_terminates_tree_before_reraising",
    ),
    "windows_posix_argv": (
        "tests/test_m6_integration.py::test_cross_platform_host_runner_preserves_argv_without_shell",
        "tests/test_m6_step13.py::test_vscode_argv_helper_keeps_workspace_and_task_as_separate_argv",
    ),
    "legacy_session_compatibility": (
        "tests/test_cli_product.py::test_legacy_session_without_mode_or_plan_remains_replayable",
        "tests/test_m6_step9.py::test_resume_uses_persisted_mode_and_old_sessions_default_to_run",
    ),
    "m1_m5_regression": (
        "tests/test_m6_acceptance.py::test_m1_to_m5_regressions_remain_part_of_m6_acceptance",
    ),
}


@pytest.mark.parametrize(
    ("scenario", "nodeids"),
    sorted(M6_STEP14_MATRIX.items()),
)
def test_every_step14_scenario_points_to_collected_test_functions(
    scenario: str,
    nodeids: tuple[str, ...],
) -> None:
    repository_root = Path(__file__).resolve().parents[1]

    assert scenario in M6_STEP14_SCENARIOS
    assert nodeids
    for nodeid in nodeids:
        relative_path, separator, test_name = nodeid.partition("::")
        assert separator == "::"
        source_path = repository_root / relative_path
        assert source_path.is_file(), nodeid
        tree = ast.parse(source_path.read_text(encoding="utf-8"), nodeid)
        defined_tests = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        }
        assert test_name in defined_tests, nodeid


def test_step14_matrix_has_exactly_one_row_per_required_scenario() -> None:
    assert set(M6_STEP14_MATRIX) == M6_STEP14_SCENARIOS
    assert len(M6_STEP14_MATRIX) == 18

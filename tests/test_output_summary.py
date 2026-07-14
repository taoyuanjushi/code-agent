"""Tests for error-focused verification output summarization."""

import pytest

from coding_agent.verification import OutputSummary, summarize_command_output


def test_output_summary_handles_empty_streams() -> None:
    summary = summarize_command_output("", "", max_bytes=128, max_lines=10)

    assert summary == OutputSummary(
        output="",
        truncated=False,
        omitted_lines=0,
        omitted_bytes=0,
    )


def test_output_summary_labels_short_stdout_and_stderr() -> None:
    summary = summarize_command_output(
        "2 passed\n",
        "warning\n",
        max_bytes=256,
        max_lines=10,
    )

    assert summary.output == "stdout: 2 passed\nstderr: warning"
    assert summary.truncated is False
    assert summary.omitted_lines == 0
    assert summary.omitted_bytes == 0


def test_output_summary_strips_ansi_and_normalizes_crlf() -> None:
    summary = summarize_command_output(
        "\x1b[32mPASS\x1b[0m\r\nnext\x1b[2K\r\n",
        "",
        max_bytes=256,
        max_lines=10,
    )

    assert summary.output == "stdout: PASS\nstdout: next"
    assert "\x1b" not in summary.output
    assert "\r" not in summary.output


def test_output_summary_keeps_traceback_from_middle_of_long_output() -> None:
    stdout = "\n".join(
        [
            *(f"setup line {index}" for index in range(40)),
            "Traceback (most recent call last):",
            '  File "src/refund.py", line 42, in calculate_refund',
            "    assert total == expected",
            "AssertionError: expected 20 but got 10",
            *(f"cleanup line {index}" for index in range(40)),
        ]
    )

    summary = summarize_command_output(
        stdout,
        "",
        max_bytes=512,
        max_lines=7,
    )

    assert "stdout: Traceback (most recent call last):" in summary.output
    assert 'stdout:   File "src/refund.py", line 42, in calculate_refund' in summary.output
    assert "stdout: AssertionError: expected 20 but got 10" in summary.output
    assert len(summary.output.splitlines()) <= 7
    assert len(summary.output.encode("utf-8")) <= 512
    assert summary.truncated is True
    assert summary.omitted_lines > 0
    assert summary.omitted_bytes > 0


def test_output_summary_keeps_error_and_stderr_tail() -> None:
    stdout = "\n".join(
        [
            "collecting tests",
            "ERROR src/service.py:17 request failed",
            "continuing after diagnostic",
            *(f"stdout noise {index}" for index in range(20)),
        ]
    )
    stderr = "\n".join(
        [
            "plugin warning",
            "more details",
            "final stderr diagnostic",
        ]
    )

    summary = summarize_command_output(
        stdout,
        stderr,
        max_bytes=512,
        max_lines=5,
    )

    assert "stdout: ERROR src/service.py:17 request failed" in summary.output
    assert "stderr: final stderr diagnostic" in summary.output
    assert len(summary.output.splitlines()) <= 5


def test_output_summary_reserves_stderr_tail_with_many_error_lines() -> None:
    summary = summarize_command_output(
        "ERROR first\nERROR second\nERROR third\n",
        "final stderr diagnostic\n",
        max_bytes=256,
        max_lines=2,
    )

    assert summary.output.splitlines() == [
        "stdout: ERROR first",
        "stderr: final stderr diagnostic",
    ]


def test_output_summary_uses_head_and_tail_without_error_keywords() -> None:
    stdout = "\n".join(f"progress {index}" for index in range(100))

    summary = summarize_command_output(
        stdout,
        "",
        max_bytes=256,
        max_lines=4,
    )

    assert "stdout: progress 0" in summary.output
    assert "stdout: progress 99" in summary.output
    assert summary.truncated is True
    assert summary.omitted_lines == 96


def test_output_summary_respects_utf8_byte_budget() -> None:
    stdout = "\n".join(
        [
            "\u666e\u901a\u8f93\u51fa" * 8,
            "\u9519\u8bef: \u9000\u6b3e\u91d1\u989d\u4e0d\u6b63\u786e" * 4,
            "\u7ed3\u675f" * 8,
        ]
    )

    summary = summarize_command_output(
        stdout,
        "",
        max_bytes=64,
        max_lines=3,
    )

    assert len(summary.output.encode("utf-8")) <= 64
    summary.output.encode("utf-8").decode("utf-8")
    assert summary.truncated is True
    assert summary.omitted_bytes > 0


@pytest.mark.parametrize(
    ("arguments", "error_type", "message"),
    [
        ({"stdout": 1, "stderr": "", "max_bytes": 10, "max_lines": 2}, TypeError, "stdout must be a string"),
        ({"stdout": "", "stderr": 1, "max_bytes": 10, "max_lines": 2}, TypeError, "stderr must be a string"),
        ({"stdout": "", "stderr": "", "max_bytes": 0, "max_lines": 2}, ValueError, "max_bytes must be a positive integer"),
        ({"stdout": "", "stderr": "", "max_bytes": 10, "max_lines": 0}, ValueError, "max_lines must be a positive integer"),
    ],
)
def test_output_summary_validates_inputs(
    arguments: dict[str, object],
    error_type: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error_type, match=message):
        summarize_command_output(**arguments)

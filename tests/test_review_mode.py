from __future__ import annotations

import json
from pathlib import Path

from coding_agent.tools import execute_tool
from coding_agent.types import AgentConfig


def _config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        workspace=str(workspace.resolve()),
        model="fake-model",
        reasoning_effort="medium",
        max_turns=4,
        permission_mode="read-only",
        auto_approve_commands=False,
        auto_approve_edits=False,
        context_max_files=8,
        context_max_bytes_per_file=8_000,
        task_mode="review",
    )


def test_review_submission_revalidates_line_numbers_after_file_drift(
    tmp_path: Path,
) -> None:
    target = tmp_path / "example.py"
    target.write_text("first\nsecond\nthird\n", encoding="utf-8")
    submission = {
        "summary": "Reviewed the current file state.",
        "findings": [
            {
                "severity": "high",
                "path": "example.py",
                "line": 3,
                "title": "Stale location",
                "detail": "This finding was prepared before the file changed.",
            }
        ],
    }

    target.write_text("first\n", encoding="utf-8")
    result = execute_tool(
        _config(tmp_path),
        "submit_review",
        json.dumps(submission),
    )

    assert result.ok is False
    assert "line 3" in result.output
    assert "1 lines in example.py" in result.output
    assert result.data is None
    assert target.read_text(encoding="utf-8") == "first\n"

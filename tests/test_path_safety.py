from pathlib import Path

import pytest

from coding_agent.path_safety import resolve_inside_workspace


def test_resolve_inside_workspace_allows_files_inside_workspace(tmp_path: Path) -> None:
    resolved = resolve_inside_workspace(tmp_path, "src/index.py")
    assert resolved == tmp_path / "src" / "index.py"


def test_resolve_inside_workspace_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Path escapes workspace"):
        resolve_inside_workspace(tmp_path, "../secret.txt")

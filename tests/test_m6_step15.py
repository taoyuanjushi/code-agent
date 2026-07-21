"""M6 step 15 release and final-acceptance contracts."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from coding_agent import __version__

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RELEASE_VERSION = "0.5.0"
REQUIRED_WHEEL_MODULES = {
    "coding_agent/ui.py",
    "coding_agent/plans.py",
    "coding_agent/task_modes.py",
    "coding_agent/reviews.py",
    "coding_agent/explanations.py",
}
FORBIDDEN_WHEEL_PATHS = {
    "tests/",
    ".env",
    ".coding-agent/",
    "editors/",
}


def test_release_version_is_consistent_across_project_and_package() -> None:
    pyproject = tomllib.loads(
        (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert pyproject["project"]["version"] == EXPECTED_RELEASE_VERSION
    assert __version__ == EXPECTED_RELEASE_VERSION

    package_init = (REPOSITORY_ROOT / "src" / "coding_agent" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert f'__version__ = "{EXPECTED_RELEASE_VERSION}"' in package_init


def test_m6_is_marked_complete_in_all_release_documents() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    implementation_plan = (
        REPOSITORY_ROOT / "docs" / "implementation-plan.md"
    ).read_text(encoding="utf-8")
    guide = (REPOSITORY_ROOT / "docs" / "m6-implementation-guide.md").read_text(
        encoding="utf-8"
    )

    assert "当前发布与打包版本为 `0.5.0`" in readme
    assert "| M6 | 已完成 |" in readme
    assert "### M6：产品化体验（已完成）" in implementation_plan
    assert "M6 第十五步已完成" in implementation_plan
    assert "包版本从 `0.4.0` 升至 `0.5.0`" in guide
    assert "- [x] README、总计划、版本和 wheel 验收完成。" in guide
    assert "- [ ] README、总计划、版本和 wheel 验收完成。" not in guide


def test_release_documents_cover_default_and_opt_in_boundaries() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    guide = (REPOSITORY_ROOT / "docs" / "m6-implementation-guide.md").read_text(
        encoding="utf-8"
    )

    for marker in ("local_rg", "local_node", "docker", "live_model", "vscode"):
        assert f"`{marker}`" in readme or f"{marker}" in guide
    assert "真实模型与 VS Code Development Host 仍是 opt-in" in readme
    assert "真实模型与 VS Code Development Host smoke 保持 opt-in" in guide
    assert "not docker and not live_model and not vscode" in guide


def test_python_wheel_scope_is_separate_from_vscode_prototype() -> None:
    pyproject = tomllib.loads(
        (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    include = pyproject["tool"]["setuptools"]["packages"]["find"]["include"]

    assert include == ["coding_agent*"]
    assert (REPOSITORY_ROOT / "editors" / "vscode" / "package.json").is_file()
    assert (REPOSITORY_ROOT / "src" / "coding_agent" / "ui.py").is_file()
    assert (REPOSITORY_ROOT / "src" / "coding_agent" / "plans.py").is_file()
    assert (REPOSITORY_ROOT / "src" / "coding_agent" / "task_modes.py").is_file()

    source_files = {
        path.relative_to(REPOSITORY_ROOT / "src").as_posix()
        for path in (REPOSITORY_ROOT / "src" / "coding_agent").rglob("*.py")
    }
    assert REQUIRED_WHEEL_MODULES <= source_files
    assert all(not path.startswith("editors/") for path in source_files)
    assert all(not path.startswith("tests/") for path in source_files)
    assert all(not path == ".env" for path in source_files)
    assert all(not path.startswith(".coding-agent/") for path in source_files)


def test_final_acceptance_commands_are_documented() -> None:
    guide = (REPOSITORY_ROOT / "docs" / "m6-implementation-guide.md").read_text(
        encoding="utf-8"
    )
    required_fragments = (
        "pytest -q --basetemp=.pytest-tmp-m6-full",
        "compileall -q src tests",
        "pip wheel . -w dist",
        "git diff --check",
    )
    assert all(fragment in guide for fragment in required_fragments)
    assert re.search(r"pytest -m live_model", guide)
    assert re.search(r"pytest -m vscode", guide)

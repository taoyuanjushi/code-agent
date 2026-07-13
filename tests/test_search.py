import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coding_agent.search as search_module
from coding_agent.search import format_search_matches, search_text


_NATIVE_RG_PATH = shutil.which("rg")


@pytest.fixture(autouse=True)
def _prefer_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_module.shutil, "which", lambda _name: None)


def _rg_event(
    path: str,
    line: str,
    *,
    line_number: int = 1,
    byte_offset: int = 0,
    match: str = "needle",
) -> dict[str, Any]:
    return {
        "type": "match",
        "data": {
            "path": {"text": path},
            "lines": {"text": line},
            "line_number": line_number,
            "absolute_offset": 0,
            "submatches": [
                {
                    "match": {"text": match},
                    "start": byte_offset,
                    "end": byte_offset + len(match.encode("utf-8")),
                }
            ],
        },
    }


def test_search_text_finds_literal_matches(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "alpha.py").write_text("print('Needle')\nneedle again\n", encoding="utf-8")
    (tmp_path / "src" / "beta.py").write_text("nothing\n", encoding="utf-8")

    matches = search_text(workspace=str(tmp_path), pattern="needle", path="src")

    assert [(match.path, match.line, match.column) for match in matches] == [
        ("src/alpha.py", 1, 8),
        ("src/alpha.py", 2, 1),
    ]
    assert "src/alpha.py:1:8" in format_search_matches(matches)


def test_search_text_honors_case_sensitivity(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("Needle\nneedle\n", encoding="utf-8")

    matches = search_text(
        workspace=str(tmp_path),
        pattern="needle",
        case_sensitive=True,
    )

    assert [(match.line, match.column) for match in matches] == [(2, 1)]


def test_search_text_limits_results_and_ignores_binary_files(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle\nneedle\n", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"needle")

    matches = search_text(workspace=str(tmp_path), pattern="needle", max_results=1)

    assert len(matches) == 1
    assert matches[0].path == "a.txt"


def test_search_text_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Path escapes workspace"):
        search_text(workspace=str(tmp_path), pattern="needle", path="../outside")


def test_search_text_uses_rg_argument_array_and_globs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text("find needle\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(args: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                _rg_event("src/service.py", "find needle\n", byte_offset=5)
            ),
            stderr="",
        )

    monkeypatch.setattr(search_module.shutil, "which", lambda _name: "C:/tools/rg.exe")
    monkeypatch.setattr(search_module.subprocess, "run", fake_run)

    matches = search_text(
        workspace=str(tmp_path),
        pattern="needle",
        path="src",
        glob=["*.py", "!test_*.py"],
    )

    assert [(match.path, match.line, match.column) for match in matches] == [
        ("src/service.py", 1, 6)
    ]
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "C:/tools/rg.exe"
    assert "--json" in args
    assert "--fixed-strings" in args
    assert "--sort" in args and "path" in args
    separator = args.index("--")
    assert args[separator + 1 :] == ["needle", "src"]
    glob_values = [args[index + 1] for index, value in enumerate(args) if value == "--glob"]
    assert "*.py" in glob_values
    assert "!test_*.py" in glob_values
    assert "!**/.git/**" in glob_values
    assert kwargs["cwd"] == tmp_path.resolve()
    assert kwargs["shell"] is False


def test_search_text_rg_return_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "file.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(search_module.shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(
        search_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="",
        ),
    )

    assert search_text(workspace=str(tmp_path), pattern="needle") == []

    monkeypatch.setattr(
        search_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2,
            stdout="",
            stderr="invalid regular expression",
        ),
    )
    with pytest.raises(RuntimeError, match="exit code 2.*invalid regular expression"):
        search_text(workspace=str(tmp_path), pattern="needle")


def test_search_text_falls_back_if_rg_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "file.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(search_module.shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(
        search_module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )

    matches = search_text(workspace=str(tmp_path), pattern="needle")

    assert [(match.path, match.line, match.column) for match in matches] == [
        ("file.txt", 1, 1)
    ]


def test_python_fallback_supports_regex_and_ordered_globs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "feature.py").write_text("Issue-42\n", encoding="utf-8")
    (tmp_path / "src" / "test_feature.py").write_text("Issue-43\n", encoding="utf-8")
    (tmp_path / "src" / "feature.txt").write_text("Issue-44\n", encoding="utf-8")

    matches = search_text(
        workspace=str(tmp_path),
        pattern=r"issue-\d+",
        regex=True,
        glob=["*.py", "!test_*.py"],
    )

    assert [(match.path, match.line, match.column) for match in matches] == [
        ("src/feature.py", 1, 1)
    ]


def test_rg_and_python_use_the_same_unicode_column(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    line = "前缀 refund\n"
    (tmp_path / "service.py").write_text(line, encoding="utf-8")
    byte_offset = len("前缀 ".encode("utf-8"))
    monkeypatch.setattr(search_module.shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(
        search_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                _rg_event(
                    "service.py",
                    line,
                    byte_offset=byte_offset,
                    match="refund",
                )
            ),
            stderr="",
        ),
    )

    rg_matches = search_text(workspace=str(tmp_path), pattern="refund")
    monkeypatch.setattr(search_module.shutil, "which", lambda _name: None)
    python_matches = search_text(workspace=str(tmp_path), pattern="refund")

    assert rg_matches == python_matches
    assert rg_matches[0].column == 4


def test_rg_results_are_filtered_by_shared_ignore_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("needle\n", encoding="utf-8")
    events = [
        _rg_event("ignored.txt", "needle\n"),
        _rg_event("visible.txt", "needle\n"),
    ]
    monkeypatch.setattr(search_module.shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(
        search_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="\n".join(json.dumps(event) for event in events),
            stderr="",
        ),
    )

    matches = search_text(workspace=str(tmp_path), pattern="needle")

    assert [match.path for match in matches] == ["visible.txt"]


def test_search_text_rejects_malformed_rg_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "file.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(search_module.shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(
        search_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="{not-json}\n",
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="rg returned invalid JSON output"):
        search_text(workspace=str(tmp_path), pattern="needle")


@pytest.mark.local_rg
@pytest.mark.skipif(_NATIVE_RG_PATH is None, reason="ripgrep is not installed")
def test_search_text_native_rg_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "native.txt").write_text("native-rg-marker\n", encoding="utf-8")
    monkeypatch.setattr(
        search_module.shutil,
        "which",
        lambda _name: _NATIVE_RG_PATH,
    )

    matches = search_text(
        workspace=str(tmp_path),
        pattern="native-rg-marker",
        case_sensitive=True,
    )

    assert [(match.path, match.line, match.column) for match in matches] == [
        ("native.txt", 1, 1)
    ]


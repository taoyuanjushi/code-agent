import base64
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pathspec.patterns import GitWildMatchPattern

from .ignore import DEFAULT_IGNORES, IgnorePolicy, load_ignore_policy
from .path_safety import resolve_inside_workspace

_RG_ERROR_LIMIT = 2_000
_RG_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class SearchMatch:
    path: str
    line: int
    column: int
    preview: str


@dataclass(frozen=True)
class _GlobMatcher:
    patterns: tuple[str, ...]
    rules: tuple[GitWildMatchPattern, ...]
    includes_by_default: bool

    def matches(self, path: str) -> bool:
        included = self.includes_by_default
        for rule in self.rules:
            if rule.include is not None and rule.match_file(path) is not None:
                included = rule.include
        return included


def search_text(
    *,
    workspace: str,
    pattern: str,
    path: str = ".",
    case_sensitive: bool = False,
    max_results: int = 100,
    max_line_length: int = 240,
    regex: bool = False,
    glob: list[str] | None = None,
) -> list[SearchMatch]:
    if not pattern:
        raise ValueError("Search pattern must not be empty.")
    if not isinstance(case_sensitive, bool):
        raise ValueError("case_sensitive must be a boolean.")
    if not isinstance(regex, bool):
        raise ValueError("regex must be a boolean.")
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results <= 0:
        raise ValueError("max_results must be a positive integer.")
    if (
        isinstance(max_line_length, bool)
        or not isinstance(max_line_length, int)
        or max_line_length <= 0
    ):
        raise ValueError("max_line_length must be a positive integer.")

    root = Path(workspace).resolve()
    start = resolve_inside_workspace(root, path)
    if not start.exists():
        raise ValueError(f"Search path does not exist: {path}")

    ignore_policy = load_ignore_policy(root)
    if ignore_policy.is_ignored(start):
        return []
    if start.is_file() and ignore_policy.is_binary(start):
        return []

    glob_matcher = _compile_glob_matcher(glob)
    rg_path = shutil.which("rg")
    if rg_path is not None:
        try:
            return _search_with_rg(
                root=root,
                start=start,
                pattern=pattern,
                case_sensitive=case_sensitive,
                max_results=max_results,
                max_line_length=max_line_length,
                regex=regex,
                glob_matcher=glob_matcher,
                ignore_policy=ignore_policy,
                rg_path=rg_path,
            )
        except FileNotFoundError:
            # The executable can disappear after shutil.which() succeeds.
            pass

    return _search_with_python(
        root=root,
        start=start,
        pattern=pattern,
        case_sensitive=case_sensitive,
        max_results=max_results,
        max_line_length=max_line_length,
        regex=regex,
        glob_matcher=glob_matcher,
        ignore_policy=ignore_policy,
    )


def format_search_matches(matches: list[SearchMatch]) -> str:
    if not matches:
        return "(no matches)"

    return "\n".join(
        f"{match.path}:{match.line}:{match.column}: {match.preview}" for match in matches
    )


def _search_with_rg(
    *,
    root: Path,
    start: Path,
    pattern: str,
    case_sensitive: bool,
    max_results: int,
    max_line_length: int,
    regex: bool,
    glob_matcher: _GlobMatcher,
    ignore_policy: IgnorePolicy,
    rg_path: str,
) -> list[SearchMatch]:
    search_path = _relative_search_path(root, start)
    args = [
        rg_path,
        "--json",
        "--line-number",
        "--column",
        "--color",
        "never",
        "--hidden",
        "--sort",
        "path",
        "--max-count",
        str(max_results),
        "--no-config",
        "--case-sensitive" if case_sensitive else "--ignore-case",
    ]
    if not regex:
        args.append("--fixed-strings")

    for ignored_name in sorted(DEFAULT_IGNORES):
        args.extend(["--glob", f"!**/{ignored_name}/**"])
    for glob_pattern in glob_matcher.patterns:
        args.extend(["--glob", glob_pattern])

    args.extend(["--", pattern, search_path])
    completed = subprocess.run(
        args,
        cwd=root,
        shell=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=_RG_TIMEOUT_SECONDS,
    )

    if completed.returncode == 1:
        return []
    if completed.returncode != 0:
        stderr = _truncate_error_output(completed.stderr or "")
        detail = stderr or "(no error output)"
        raise RuntimeError(
            f"rg search failed with exit code {completed.returncode}: {detail}"
        )

    stdout = completed.stdout or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    return _parse_rg_matches(
        stdout,
        root=root,
        ignore_policy=ignore_policy,
        glob_matcher=glob_matcher,
        max_results=max_results,
        max_line_length=max_line_length,
    )


def _search_with_python(
    *,
    root: Path,
    start: Path,
    pattern: str,
    case_sensitive: bool,
    max_results: int,
    max_line_length: int,
    regex: bool,
    glob_matcher: _GlobMatcher,
    ignore_policy: IgnorePolicy,
) -> list[SearchMatch]:
    if start.is_file():
        files = [start]
    else:
        files = _iter_search_files(root, start, ignore_policy)

    compiled_pattern: re.Pattern[str] | None = None
    if regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            compiled_pattern = re.compile(pattern, flags=flags)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc

    matches: list[SearchMatch] = []
    needle = pattern if case_sensitive else pattern.lower()

    for file_path in files:
        relative_path = file_path.relative_to(root).as_posix()
        if not glob_matcher.matches(relative_path):
            continue

        text = _read_searchable_text(file_path)
        if text is None:
            continue

        for line_number, line in enumerate(text.splitlines(), start=1):
            if compiled_pattern is not None:
                match = compiled_pattern.search(line)
                column = -1 if match is None else match.start()
            else:
                haystack = line if case_sensitive else line.lower()
                column = haystack.find(needle)

            if column < 0:
                continue

            matches.append(
                SearchMatch(
                    path=relative_path,
                    line=line_number,
                    column=column + 1,
                    preview=_trim_line(line, max_line_length),
                )
            )
            if len(matches) >= max_results:
                return matches

    return matches


def _parse_rg_matches(
    output: str,
    *,
    root: Path,
    ignore_policy: IgnorePolicy,
    glob_matcher: _GlobMatcher,
    max_results: int,
    max_line_length: int,
) -> list[SearchMatch]:
    matches: list[SearchMatch] = []

    for raw_event in output.splitlines():
        if not raw_event.strip():
            continue
        try:
            event = json.loads(raw_event)
        except json.JSONDecodeError as exc:
            raise RuntimeError("rg returned invalid JSON output.") from exc

        if event.get("type") != "match":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue

        submatches = data.get("submatches")
        if not isinstance(submatches, list) or not submatches:
            continue
        first_submatch = submatches[0]
        if not isinstance(first_submatch, dict):
            continue
        byte_offset = first_submatch.get("start")
        line_number = data.get("line_number")
        if not isinstance(byte_offset, int) or not isinstance(line_number, int):
            continue

        path_text, _ = _decode_rg_field(data.get("path"))
        line_text, line_bytes = _decode_rg_field(data.get("lines"))
        if path_text is None or line_text is None or line_bytes is None:
            continue

        candidate = resolve_inside_workspace(root, path_text.replace("\\", "/"))
        relative_path = candidate.relative_to(root).as_posix()
        if (
            ignore_policy.is_ignored(candidate)
            or ignore_policy.is_binary(candidate)
            or not glob_matcher.matches(relative_path)
        ):
            continue

        matches.append(
            SearchMatch(
                path=relative_path,
                line=line_number,
                column=_utf8_column(line_bytes, byte_offset),
                preview=_trim_line(line_text.rstrip("\r\n"), max_line_length),
            )
        )
        if len(matches) >= max_results:
            return matches

    return matches


def _compile_glob_matcher(patterns: list[str] | None) -> _GlobMatcher:
    if patterns is None:
        patterns = []
    if not isinstance(patterns, list) or any(
        not isinstance(pattern, str) or not pattern for pattern in patterns
    ):
        raise ValueError("glob must be a list of non-empty strings.")

    rules = tuple(GitWildMatchPattern(pattern) for pattern in patterns)
    has_positive_pattern = any(rule.include is True for rule in rules)
    return _GlobMatcher(
        patterns=tuple(patterns),
        rules=rules,
        includes_by_default=not has_positive_pattern,
    )


def _iter_search_files(
    root: Path,
    start: Path,
    ignore_policy: IgnorePolicy,
) -> list[Path]:
    files: list[Path] = []
    for path in start.rglob("*"):
        if (
            not path.is_file()
            or ignore_policy.is_ignored(path)
            or ignore_policy.is_binary(path)
        ):
            continue
        files.append(path)
    return sorted(files, key=lambda candidate: candidate.relative_to(root).as_posix())


def _read_searchable_text(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\0" in data[:8000]:
        return None

    return data.decode("utf-8", errors="replace")


def _relative_search_path(root: Path, start: Path) -> str:
    relative = start.relative_to(root).as_posix()
    return "." if relative == "." else relative


def _decode_rg_field(value: Any) -> tuple[str | None, bytes | None]:
    if not isinstance(value, dict):
        return None, None

    text = value.get("text")
    if isinstance(text, str):
        return text, text.encode("utf-8")

    encoded = value.get("bytes")
    if not isinstance(encoded, str):
        return None, None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError:
        return None, None
    return raw.decode("utf-8", errors="replace"), raw


def _utf8_column(line: bytes, byte_offset: int) -> int:
    safe_offset = min(max(byte_offset, 0), len(line))
    return len(line[:safe_offset].decode("utf-8", errors="replace")) + 1


def _truncate_error_output(stderr: str) -> str:
    stripped = stderr.strip()
    if len(stripped) <= _RG_ERROR_LIMIT:
        return stripped
    return f"{stripped[:_RG_ERROR_LIMIT]}... [truncated]"


def _trim_line(line: str, max_length: int) -> str:
    stripped = line.strip()
    if len(stripped) <= max_length:
        return stripped
    return f"{stripped[:max_length]}..."

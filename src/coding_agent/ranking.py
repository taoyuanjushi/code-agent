import re
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath

from .search import SearchMatch
from .types import WorkspaceFile

BASENAME_MENTION_SCORE = 100
PATH_TOKEN_SCORE = 25
SEARCH_HIT_SCORE = 15
MAX_SEARCH_HIT_SCORE = 60
PROJECT_ENTRY_SCORE = 20
RELATED_TEST_SCORE = 30

PROJECT_ENTRY_PATHS = frozenset(
    {
        "readme.md",
        "pyproject.toml",
        "requirements.txt",
        "package.json",
        "cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "settings.gradle",
        "makefile",
        "dockerfile",
        "setup.py",
        "setup.cfg",
        "tox.ini",
    }
)

_LARGE_FILE_PENALTIES = (
    (1024 * 1024, -30),
    (256 * 1024, -20),
    (64 * 1024, -10),
)
_TOKEN_SEPARATOR = re.compile(r"[\s/_.-]+")
_TASK_PATH_FRAGMENT = re.compile(r"[\w./\\-]+", flags=re.UNICODE)
_TEST_DIRECTORIES = frozenset({"test", "tests", "__tests__", "spec", "specs"})
_TEST_PREFIXES = ("test_", "test-", "spec_", "spec-")
_TEST_SUFFIXES = (
    "_test",
    "-test",
    ".test",
    "_tests",
    "-tests",
    ".tests",
    "_spec",
    "-spec",
    ".spec",
    "_specs",
    "-specs",
    ".specs",
)


@dataclass(frozen=True)
class RankedFile:
    path: str
    score: int
    reasons: tuple[str, ...]


def rank_files(
    files: list[WorkspaceFile],
    task: str,
    search_hits: list[SearchMatch] | None = None,
) -> list[RankedFile]:
    if not isinstance(task, str):
        raise ValueError("task must be a string.")

    task_tokens = set(_tokenize(task))
    task_basenames = _task_basenames(task)
    hit_counts = Counter(
        _normalize_path(hit.path) for hit in (search_hits or [])
    )
    hit_source_keys = {
        module_key
        for path in hit_counts
        if not _looks_like_test(path)
        for module_key in [_module_key(path)]
        if module_key
    }

    ranked: list[RankedFile] = []
    for file in files:
        path = _normalize_path(file.path)
        score = 0
        reasons: list[str] = []

        if task_mentions_file(task, path, task_basenames=task_basenames):
            score += BASENAME_MENTION_SCORE
            reasons.append(f"basename mentioned in task (+{BASENAME_MENTION_SCORE})")

        matching_tokens = sorted(task_tokens.intersection(_tokenize(path)))
        if matching_tokens:
            token_score = len(matching_tokens) * PATH_TOKEN_SCORE
            score += token_score
            reasons.append(
                f"task tokens in path: {', '.join(matching_tokens)} (+{token_score})"
            )

        hit_count = hit_counts[path]
        if hit_count:
            hit_score = min(
                hit_count * SEARCH_HIT_SCORE,
                MAX_SEARCH_HIT_SCORE,
            )
            score += hit_score
            score_label = (
                f"capped +{hit_score}"
                if hit_count * SEARCH_HIT_SCORE > hit_score
                else f"+{hit_score}"
            )
            reasons.append(
                f"{hit_count} search {'hit' if hit_count == 1 else 'hits'}"
                f" ({score_label})"
            )

        if path.casefold() in PROJECT_ENTRY_PATHS:
            score += PROJECT_ENTRY_SCORE
            reasons.append(f"project entry point (+{PROJECT_ENTRY_SCORE})")

        module_key = _module_key(path)
        if (
            module_key
            and _looks_like_test(path)
            and module_key in hit_source_keys
        ):
            score += RELATED_TEST_SCORE
            reasons.append(
                f"test for search-hit source (+{RELATED_TEST_SCORE})"
            )

        size_penalty = _large_file_penalty(file.size)
        if size_penalty:
            score += size_penalty
            reasons.append(f"large file: {file.size} bytes ({size_penalty})")

        ranked.append(
            RankedFile(
                path=path,
                score=score,
                reasons=tuple(reasons),
            )
        )

    return sorted(ranked, key=lambda file: (-file.score, file.path))


def task_mentions_file(
    task: str,
    path: str,
    *,
    task_basenames: set[str] | None = None,
) -> bool:
    basenames = task_basenames if task_basenames is not None else _task_basenames(task)
    basename = PurePosixPath(_normalize_path(path)).name.casefold()
    return basename in basenames


def _tokenize(value: str) -> tuple[str, ...]:
    normalized = value.replace("\\", "/").casefold()
    return tuple(token for token in _TOKEN_SEPARATOR.split(normalized) if token)


def _task_basenames(task: str) -> set[str]:
    basenames: set[str] = set()
    for fragment in _TASK_PATH_FRAGMENT.findall(task.casefold()):
        normalized = fragment.replace("\\", "/").rstrip("/.")
        if normalized:
            basenames.add(PurePosixPath(normalized).name)
    return basenames


def _looks_like_test(path: str) -> bool:
    normalized = _normalize_path(path).casefold()
    pure_path = PurePosixPath(normalized)
    if any(part in _TEST_DIRECTORIES for part in pure_path.parts[:-1]):
        return True

    stem = _stem_without_extension(pure_path.name)
    return stem.startswith(_TEST_PREFIXES) or stem.endswith(_TEST_SUFFIXES)


def _module_key(path: str) -> str:
    name = PurePosixPath(_normalize_path(path).casefold()).name
    key = _stem_without_extension(name)

    changed = True
    while key and changed:
        changed = False
        for prefix in _TEST_PREFIXES:
            if key.startswith(prefix):
                key = key[len(prefix) :]
                changed = True
                break
        for suffix in _TEST_SUFFIXES:
            if key.endswith(suffix):
                key = key[: -len(suffix)]
                changed = True
                break

    return key.strip("._-")


def _stem_without_extension(name: str) -> str:
    if "." not in name:
        return name
    return name.rsplit(".", 1)[0]


def _large_file_penalty(size: int) -> int:
    for threshold, penalty in _LARGE_FILE_PENALTIES:
        if size >= threshold:
            return penalty
    return 0


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized

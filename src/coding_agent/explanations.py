from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

_EXPLANATION_PATH_MAX_CHARS = 1024
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_PLAIN_CITATION = re.compile(
    r"(?<![A-Za-z0-9_./\\-])"
    r"(?P<path>[A-Za-z0-9_@+()./\\-]+)"
    r":(?P<line>[0-9]+)\b"
)
_COMMON_EXTENSIONLESS_FILES = frozenset(
    {
        "Dockerfile",
        "Gemfile",
        "LICENSE",
        "Makefile",
        "NOTICE",
        "Procfile",
        "Rakefile",
    }
)


@dataclass(frozen=True)
class ExplanationReadEvidence:
    """A bounded record of workspace file contents shown to the model."""

    path: str
    max_line: int
    truncated: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_explanation_path(self.path))
        if isinstance(self.max_line, bool) or not isinstance(self.max_line, int):
            raise TypeError("explanation evidence max_line must be an integer.")
        if self.max_line < 0:
            raise ValueError("explanation evidence max_line must not be negative.")
        if not isinstance(self.truncated, bool):
            raise TypeError("explanation evidence truncated must be a boolean.")


@dataclass(frozen=True)
class ExplanationCitation:
    """One path:line location parsed from a user-visible explanation."""

    path: str
    line: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_explanation_path(self.path))
        if isinstance(self.line, bool) or not isinstance(self.line, int):
            raise TypeError("explanation citation line must be an integer.")
        if self.line <= 0:
            raise ValueError("explanation citation line must be a positive integer.")


def normalize_explanation_path(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("explanation path must be a string.")
    raw = value.strip()
    if not raw:
        raise ValueError("explanation path must be non-empty after trimming.")
    if "\x00" in raw:
        raise ValueError("explanation path must not contain NUL characters.")
    if len(raw) > _EXPLANATION_PATH_MAX_CHARS:
        raise ValueError(
            "explanation path must be at most "
            f"{_EXPLANATION_PATH_MAX_CHARS} characters."
        )

    normalized = raw.replace("\\", "/")
    if (
        normalized.startswith("/")
        or normalized.startswith("//")
        or _WINDOWS_DRIVE.match(normalized)
    ):
        raise ValueError("explanation path must be workspace-relative.")
    raw_parts = normalized.split("/")
    if any(part == ".." for part in raw_parts):
        raise ValueError("explanation path must not contain parent components.")
    parts = [part for part in raw_parts if part not in {"", "."}]
    if not parts:
        raise ValueError("explanation path must identify a file.")
    path = PurePosixPath(*parts).as_posix()
    if path in {"", "."}:
        raise ValueError("explanation path must identify a file.")
    return path


def explanation_read_evidence_to_dict(
    value: ExplanationReadEvidence,
) -> dict[str, object]:
    if not isinstance(value, ExplanationReadEvidence):
        raise TypeError("value must be ExplanationReadEvidence.")
    return {
        "path": value.path,
        "max_line": value.max_line,
        "truncated": value.truncated,
    }


def explanation_read_evidence_from_dict(
    value: Mapping[str, object],
) -> ExplanationReadEvidence:
    if not isinstance(value, Mapping):
        raise TypeError("explanation read evidence must be an object.")
    unknown = set(value) - {"path", "max_line", "truncated"}
    if unknown:
        raise ValueError(
            "explanation read evidence contains unknown fields: "
            + ", ".join(sorted(unknown))
        )
    missing = {"path", "max_line", "truncated"} - set(value)
    if missing:
        raise ValueError(
            "explanation read evidence is missing fields: "
            + ", ".join(sorted(missing))
        )
    return ExplanationReadEvidence(
        path=value["path"],  # type: ignore[arg-type]
        max_line=value["max_line"],  # type: ignore[arg-type]
        truncated=value["truncated"],  # type: ignore[arg-type]
    )


def explanation_read_evidence_list_to_dict(
    values: Iterable[ExplanationReadEvidence],
) -> list[dict[str, object]]:
    return [explanation_read_evidence_to_dict(value) for value in values]


def explanation_read_evidence_list_from_dict(
    value: object,
) -> tuple[ExplanationReadEvidence, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("explanation read evidence files must be an array.")
    evidence: list[ExplanationReadEvidence] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("explanation read evidence entries must be objects.")
        evidence.append(explanation_read_evidence_from_dict(item))
    return merge_explanation_read_evidence(evidence)


def explanation_read_evidence_from_tool_data(
    value: object,
) -> tuple[ExplanationReadEvidence, ...]:
    if not isinstance(value, Mapping):
        raise TypeError("read tool evidence data must be an object.")
    if value.get("type") != "read_evidence":
        raise ValueError("read tool evidence data has an invalid type.")
    unknown = set(value) - {"type", "files"}
    if unknown:
        raise ValueError(
            "read tool evidence data contains unknown fields: "
            + ", ".join(sorted(unknown))
        )
    if "files" not in value:
        raise ValueError("read tool evidence data is missing files.")
    return explanation_read_evidence_list_from_dict(value["files"])


def merge_explanation_read_evidence(
    values: Iterable[ExplanationReadEvidence],
) -> tuple[ExplanationReadEvidence, ...]:
    merged: dict[str, ExplanationReadEvidence] = {}
    for value in values:
        if not isinstance(value, ExplanationReadEvidence):
            raise TypeError("explanation evidence must contain evidence values.")
        previous = merged.get(value.path)
        if previous is None or value.max_line > previous.max_line:
            merged[value.path] = value
        elif value.max_line == previous.max_line:
            merged[value.path] = ExplanationReadEvidence(
                path=value.path,
                max_line=value.max_line,
                truncated=previous.truncated and value.truncated,
            )
    return tuple(merged.values())


def extract_explanation_citations(text: str) -> tuple[ExplanationCitation, ...]:
    if not isinstance(text, str):
        raise TypeError("explanation text must be a string.")

    raw_citations: list[tuple[str, str]] = []
    backtick_spans: list[tuple[int, int]] = []
    for match in re.finditer(r"`([^`\r\n]+)`", text):
        backtick_spans.append(match.span())
        token = match.group(1).strip()
        if ":" not in token:
            continue
        path, line = token.rsplit(":", 1)
        if line.isdigit() and _looks_like_workspace_path(path):
            raw_citations.append((path, line))

    for match in _PLAIN_CITATION.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in backtick_spans):
            continue
        path = match.group("path")
        if _looks_like_workspace_path(path):
            raw_citations.append((path, match.group("line")))

    citations: list[ExplanationCitation] = []
    seen: set[tuple[str, int]] = set()
    for raw_path, raw_line in raw_citations:
        citation = ExplanationCitation(path=raw_path, line=int(raw_line))
        key = (citation.path, citation.line)
        if key not in seen:
            citations.append(citation)
            seen.add(key)
    return tuple(citations)


def validate_explanation_answer(
    answer: str,
    evidence: Iterable[ExplanationReadEvidence],
) -> tuple[ExplanationCitation, ...]:
    if not isinstance(answer, str):
        raise TypeError("explain mode final answer must be a string.")
    merged = merge_explanation_read_evidence(evidence)
    by_path = {item.path: item for item in merged}
    citations = extract_explanation_citations(answer)

    for citation in citations:
        read = by_path.get(citation.path)
        if read is None:
            raise ValueError(
                "explain mode final answer cites a file that was not "
                f"successfully read: {citation.path}."
            )
        if citation.line > read.max_line:
            raise ValueError(
                "explain mode citation line exceeds read evidence: "
                f"{citation.path}:{citation.line} (max line {read.max_line})."
            )

    if any(item.max_line > 0 for item in merged) and not citations:
        raise ValueError(
            "explain mode final answer must cite at least one successfully "
            "read file as path:line."
        )
    return citations


def _looks_like_workspace_path(value: str) -> bool:
    path = value.strip()
    if not path:
        return False
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    if "/" in normalized:
        return True
    if name in _COMMON_EXTENSIONLESS_FILES:
        return True
    if "." not in name:
        return False
    suffix = name.rsplit(".", 1)[-1]
    return any(character.isalpha() for character in suffix)

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, cast

ReviewSeverity = Literal["critical", "high", "medium", "low"]

REVIEW_SEVERITIES = frozenset({"critical", "high", "medium", "low"})
REVIEW_SEVERITY_ORDER: dict[ReviewSeverity, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}
REVIEW_MAX_FINDINGS = 50
REVIEW_MAX_SUMMARY_CHARS = 1000
REVIEW_MAX_TITLE_CHARS = 200
REVIEW_MAX_DETAIL_CHARS = 1000
REVIEW_MAX_SERIALIZED_BYTES = 24 * 1024
_REVIEW_PATH_MAX_CHARS = 1024
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class ReviewFinding:
    """One immutable, source-located code review finding."""

    severity: ReviewSeverity
    path: str
    line: int
    title: str
    detail: str

    def __post_init__(self) -> None:
        if self.severity not in REVIEW_SEVERITIES:
            raise ValueError(f"Unsupported review severity: {self.severity}")
        object.__setattr__(self, "path", normalize_review_path(self.path))
        if isinstance(self.line, bool) or not isinstance(self.line, int):
            raise TypeError("review finding line must be an integer.")
        if self.line <= 0:
            raise ValueError("review finding line must be a positive integer.")
        object.__setattr__(
            self,
            "title",
            _bounded_text(
                self.title,
                "review finding title",
                REVIEW_MAX_TITLE_CHARS,
            ),
        )
        object.__setattr__(
            self,
            "detail",
            _bounded_text(
                self.detail,
                "review finding detail",
                REVIEW_MAX_DETAIL_CHARS,
            ),
        )


@dataclass(frozen=True)
class ReviewResult:
    """Final structured review submitted once for a review-mode session."""

    summary: str
    findings: tuple[ReviewFinding, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "summary",
            _bounded_text(
                self.summary,
                "review summary",
                REVIEW_MAX_SUMMARY_CHARS,
            ),
        )
        if not isinstance(self.findings, tuple):
            raise TypeError("review findings must be a tuple.")
        if len(self.findings) > REVIEW_MAX_FINDINGS:
            raise ValueError(
                f"review may contain at most {REVIEW_MAX_FINDINGS} findings."
            )
        if not all(isinstance(finding, ReviewFinding) for finding in self.findings):
            raise TypeError("review findings must contain ReviewFinding values.")
        keys = [
            (finding.path, finding.line, finding.title)
            for finding in self.findings
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("review findings must be unique by path, line, and title.")
        _validate_serialized_budget(self)


def normalize_review_path(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("review finding path must be a string.")
    raw = value.strip()
    if not raw:
        raise ValueError("review finding path must be non-empty after trimming.")
    if "\x00" in raw:
        raise ValueError("review finding path must not contain NUL characters.")
    if len(raw) > _REVIEW_PATH_MAX_CHARS:
        raise ValueError(
            f"review finding path must be at most {_REVIEW_PATH_MAX_CHARS} characters."
        )

    normalized = raw.replace("\\", "/")
    if (
        normalized.startswith("/")
        or normalized.startswith("//")
        or _WINDOWS_DRIVE.match(normalized)
    ):
        raise ValueError("review finding path must be workspace-relative.")
    raw_parts = normalized.split("/")
    if any(part == ".." for part in raw_parts):
        raise ValueError("review finding path must not contain parent components.")
    parts = [part for part in raw_parts if part not in {"", "."}]
    if not parts:
        raise ValueError("review finding path must identify a file.")
    path = PurePosixPath(*parts).as_posix()
    if path in {"", "."}:
        raise ValueError("review finding path must identify a file.")
    return path


def review_result_to_dict(value: ReviewResult) -> dict[str, object]:
    if not isinstance(value, ReviewResult):
        raise TypeError("value must be a ReviewResult.")
    return {
        "summary": value.summary,
        "findings": [
            {
                "severity": finding.severity,
                "path": finding.path,
                "line": finding.line,
                "title": finding.title,
                "detail": finding.detail,
            }
            for finding in value.findings
        ],
    }


def review_result_from_dict(data: Mapping[str, object]) -> ReviewResult:
    """Strictly decode persisted or tool-provided structured review data."""

    if not isinstance(data, Mapping):
        raise TypeError("ReviewResult must be an object.")
    if not all(isinstance(key, str) for key in data):
        raise TypeError("ReviewResult keys must be strings.")
    fields = set(data)
    required = {"summary", "findings"}
    missing = sorted(required - fields)
    unknown = sorted(fields - required)
    if missing:
        raise ValueError(f"ReviewResult is missing fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"ReviewResult contains unknown fields: {', '.join(unknown)}")

    summary = data["summary"]
    if not isinstance(summary, str):
        raise TypeError("review summary must be a string.")
    raw_findings = data["findings"]
    if not isinstance(raw_findings, (list, tuple)):
        raise TypeError("review findings must be an array.")
    if len(raw_findings) > REVIEW_MAX_FINDINGS:
        raise ValueError(
            f"review may contain at most {REVIEW_MAX_FINDINGS} findings."
        )

    findings: list[ReviewFinding] = []
    seen: set[tuple[str, int, str]] = set()
    required_finding_fields = {"severity", "path", "line", "title", "detail"}
    for index, raw_finding in enumerate(raw_findings):
        label = f"review finding {index + 1}"
        if not isinstance(raw_finding, Mapping):
            raise TypeError(f"{label} must be an object.")
        if not all(isinstance(key, str) for key in raw_finding):
            raise TypeError(f"{label} keys must be strings.")
        finding_fields = set(raw_finding)
        missing_fields = sorted(required_finding_fields - finding_fields)
        unknown_fields = sorted(finding_fields - required_finding_fields)
        if missing_fields:
            raise ValueError(
                f"{label} is missing fields: {', '.join(missing_fields)}"
            )
        if unknown_fields:
            raise ValueError(
                f"{label} contains unknown fields: {', '.join(unknown_fields)}"
            )

        severity = raw_finding["severity"]
        path = raw_finding["path"]
        line = raw_finding["line"]
        title = raw_finding["title"]
        detail = raw_finding["detail"]
        if not isinstance(severity, str):
            raise TypeError(f"{label} severity must be a string.")
        if not isinstance(path, str):
            raise TypeError(f"{label} path must be a string.")
        if isinstance(line, bool) or not isinstance(line, int):
            raise TypeError(f"{label} line must be an integer.")
        if not isinstance(title, str):
            raise TypeError(f"{label} title must be a string.")
        if not isinstance(detail, str):
            raise TypeError(f"{label} detail must be a string.")
        finding = ReviewFinding(
            severity=cast(ReviewSeverity, severity),
            path=path,
            line=line,
            title=title,
            detail=detail,
        )
        key = (finding.path, finding.line, finding.title)
        if key in seen:
            continue
        seen.add(key)
        findings.append(finding)

    return ReviewResult(summary=summary, findings=tuple(findings))


def parse_review_submission(data: Mapping[str, object]) -> ReviewResult:
    return review_result_from_dict(data)


def sorted_review_findings(
    value: ReviewResult,
) -> tuple[ReviewFinding, ...]:
    if not isinstance(value, ReviewResult):
        raise TypeError("value must be a ReviewResult.")
    return tuple(
        sorted(
            value.findings,
            key=lambda finding: (
                REVIEW_SEVERITY_ORDER[finding.severity],
                finding.path.casefold(),
                finding.path,
                finding.line,
                finding.title.casefold(),
                finding.title,
            ),
        )
    )


def _bounded_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} must be non-empty after trimming.")
    if len(normalized) > maximum:
        raise ValueError(f"{label} must be at most {maximum} characters.")
    return normalized


def _validate_serialized_budget(value: ReviewResult) -> None:
    encoded = json.dumps(
        review_result_to_dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > REVIEW_MAX_SERIALIZED_BYTES:
        raise ValueError(
            "serialized review must be at most "
            f"{REVIEW_MAX_SERIALIZED_BYTES} bytes."
        )


__all__ = [
    "REVIEW_MAX_DETAIL_CHARS",
    "REVIEW_MAX_FINDINGS",
    "REVIEW_MAX_SERIALIZED_BYTES",
    "REVIEW_MAX_SUMMARY_CHARS",
    "REVIEW_MAX_TITLE_CHARS",
    "REVIEW_SEVERITIES",
    "REVIEW_SEVERITY_ORDER",
    "ReviewFinding",
    "ReviewResult",
    "ReviewSeverity",
    "normalize_review_path",
    "parse_review_submission",
    "review_result_from_dict",
    "review_result_to_dict",
    "sorted_review_findings",
]

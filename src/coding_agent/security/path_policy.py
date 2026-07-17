from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import cast

from .models import PATH_OPERATIONS, PathOperation, SensitivePathDecision

SENSITIVE_PATH_DENIAL_REASON = "sensitive_path_denied"
SENSITIVE_PATH_ALLOWED_REASON = "sensitive_path_allowed"
SENSITIVE_PATH_EXCEPTION_REASON = "sensitive_path_exception"

DEFAULT_DENIED_NAMES = frozenset(
    {
        ".npmrc",
        ".pypirc",
        ".netrc",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
    }
)
DEFAULT_DENIED_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})
DEFAULT_ALLOWED_EXCEPTIONS = frozenset({".env.example", ".env.sample"})
DEFAULT_DENIED_DIRECTORIES = frozenset({".ssh", ".aws", ".coding-agent"})
DEFAULT_DENIED_PATHS = frozenset({".config/gcloud"})


@dataclass(frozen=True)
class SensitivePathPolicy:
    """Case-insensitive security policy for workspace content paths.

    This policy is deliberately separate from ``IgnorePolicy``. Ignore rules
    control repository relevance; this policy is a fail-closed boundary that
    cannot be weakened by ``.gitignore`` negation rules.
    """

    root: Path
    denied_names: frozenset[str]
    denied_suffixes: frozenset[str]
    allowed_exceptions: frozenset[str]
    denied_directories: frozenset[str] = DEFAULT_DENIED_DIRECTORIES
    denied_paths: frozenset[str] = DEFAULT_DENIED_PATHS
    _normalized_denied_names: frozenset[str] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _normalized_denied_suffixes: frozenset[str] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _normalized_allowed_exceptions: frozenset[str] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _normalized_denied_directories: frozenset[str] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _normalized_denied_paths: tuple[tuple[str, ...], ...] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        root = Path(self.root).resolve()
        if not root.is_dir():
            raise ValueError(f"Workspace is not an existing directory: {root}")

        object.__setattr__(self, "root", root)
        object.__setattr__(
            self,
            "_normalized_denied_names",
            _normalize_name_set(self.denied_names, "denied_names"),
        )
        object.__setattr__(
            self,
            "_normalized_denied_suffixes",
            _normalize_suffix_set(self.denied_suffixes),
        )
        object.__setattr__(
            self,
            "_normalized_allowed_exceptions",
            _normalize_name_set(self.allowed_exceptions, "allowed_exceptions"),
        )
        object.__setattr__(
            self,
            "_normalized_denied_directories",
            _normalize_name_set(self.denied_directories, "denied_directories"),
        )
        object.__setattr__(
            self,
            "_normalized_denied_paths",
            _normalize_denied_paths(self.denied_paths),
        )

    def evaluate(
        self,
        path: str | Path,
        *,
        operation: str,
    ) -> SensitivePathDecision:
        """Return a stable allow/deny decision before content is opened."""

        if operation not in PATH_OPERATIONS:
            raise ValueError(f"Unsupported path operation: {operation}")

        normalized_path, candidate = _normalize_workspace_path(self.root, path)
        matched_rule = self._matched_rule(normalized_path)

        if matched_rule is None:
            resolved_path = candidate.resolve(strict=False)
            try:
                resolved_relative = resolved_path.relative_to(self.root).as_posix()
            except ValueError:
                matched_rule = "resolved_target_outside_workspace"
            else:
                if resolved_relative != normalized_path:
                    matched_rule = self._matched_rule(resolved_relative)

        typed_operation = cast(PathOperation, operation)
        if matched_rule is not None:
            return SensitivePathDecision(
                path=normalized_path,
                operation=typed_operation,
                allowed=False,
                rule_id=SENSITIVE_PATH_DENIAL_REASON,
                reasons=(SENSITIVE_PATH_DENIAL_REASON, matched_rule),
            )

        basename = PurePosixPath(normalized_path).name.casefold()
        if basename in self._normalized_allowed_exceptions:
            return SensitivePathDecision(
                path=normalized_path,
                operation=typed_operation,
                allowed=True,
                rule_id=SENSITIVE_PATH_EXCEPTION_REASON,
                reasons=(SENSITIVE_PATH_EXCEPTION_REASON,),
            )

        return SensitivePathDecision(
            path=normalized_path,
            operation=typed_operation,
            allowed=True,
            rule_id=SENSITIVE_PATH_ALLOWED_REASON,
            reasons=(SENSITIVE_PATH_ALLOWED_REASON,),
        )

    def _matched_rule(self, normalized_path: str) -> str | None:
        parts = tuple(
            part.casefold()
            for part in PurePosixPath(normalized_path).parts
            if part not in {"", "."}
        )
        if not parts:
            return None

        for part in parts:
            if part in self._normalized_denied_directories:
                return "sensitive_directory"

        for denied_path in self._normalized_denied_paths:
            if _contains_component_sequence(parts, denied_path):
                return "sensitive_directory_tree"

        for part in parts:
            if part in self._normalized_denied_names:
                return "sensitive_name"
            if _is_sensitive_env_name(
                part,
                allowed_exceptions=self._normalized_allowed_exceptions,
            ):
                return "sensitive_environment_file"
            if any(
                part.endswith(suffix)
                for suffix in self._normalized_denied_suffixes
            ):
                return "sensitive_key_material"

        return None


def load_sensitive_path_policy(
    workspace: str | Path,
) -> SensitivePathPolicy:
    return SensitivePathPolicy(
        root=Path(workspace),
        denied_names=DEFAULT_DENIED_NAMES,
        denied_suffixes=DEFAULT_DENIED_SUFFIXES,
        allowed_exceptions=DEFAULT_ALLOWED_EXCEPTIONS,
    )


def _normalize_workspace_path(root: Path, path: str | Path) -> tuple[str, Path]:
    if not isinstance(path, (str, Path)):
        raise TypeError("path must be a string or Path.")

    raw_path = os.fspath(path)
    if not raw_path:
        raise ValueError("path must not be empty.")
    if "\x00" in raw_path:
        raise ValueError("path must not contain NUL characters.")

    native_path = Path(raw_path.replace("\\", "/"))
    candidate = native_path if native_path.is_absolute() else root / native_path
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path escapes workspace: {raw_path}") from exc

    normalized_path = relative.as_posix()
    if normalized_path == ".":
        normalized_path = "."
    return normalized_path, candidate


def _normalize_name_set(values: frozenset[str], label: str) -> frozenset[str]:
    if not isinstance(values, frozenset):
        raise TypeError(f"{label} must be a frozenset.")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or "/" in value or "\\" in value:
            raise ValueError(f"{label} entries must be non-empty path component names.")
        normalized.add(value.casefold())
    return frozenset(normalized)


def _normalize_suffix_set(values: frozenset[str]) -> frozenset[str]:
    normalized = _normalize_name_set(values, "denied_suffixes")
    if any(not value.startswith(".") for value in normalized):
        raise ValueError("denied_suffixes entries must start with '.'.")
    return normalized


def _normalize_denied_paths(values: frozenset[str]) -> tuple[tuple[str, ...], ...]:
    if not isinstance(values, frozenset):
        raise TypeError("denied_paths must be a frozenset.")
    normalized: set[tuple[str, ...]] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError("denied_paths entries must be non-empty strings.")
        candidate = PurePosixPath(value.replace("\\", "/"))
        if candidate.is_absolute() or any(
            part in {"", ".", ".."} for part in candidate.parts
        ):
            raise ValueError(
                "denied_paths entries must be canonical relative paths."
            )
        normalized.add(tuple(part.casefold() for part in candidate.parts))
    return tuple(sorted(normalized))


def _contains_component_sequence(
    parts: tuple[str, ...],
    sequence: tuple[str, ...],
) -> bool:
    width = len(sequence)
    return any(
        parts[index : index + width] == sequence
        for index in range(len(parts) - width + 1)
    )


def _is_sensitive_env_name(
    name: str,
    *,
    allowed_exceptions: frozenset[str],
) -> bool:
    if name in allowed_exceptions:
        return False
    return name == ".env" or name.startswith(".env.")

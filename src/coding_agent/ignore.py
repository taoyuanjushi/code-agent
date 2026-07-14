import os
from dataclasses import dataclass, field
from pathlib import Path

from pathspec import GitIgnoreSpec

DEFAULT_IGNORES = frozenset(
    {
        ".git",
        "node_modules",
        "dist",
        "build",
        ".next",
        "coverage",
        ".coding-agent",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
    }
)

BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".7z",
        ".exe",
        ".dll",
    }
)


@dataclass(frozen=True)
class _GitIgnoreRules:
    path: Path
    directory: Path
    spec: GitIgnoreSpec


@dataclass(frozen=True)
class IgnorePolicy:
    root: Path
    gitignore_files: tuple[Path, ...]
    _rules: tuple[_GitIgnoreRules, ...] = field(repr=False, compare=False)

    def is_ignored(self, path: Path) -> bool:
        candidate = _absolute_without_resolving_symlinks(self.root, path)
        relative = _relative_to_root(self.root, candidate)

        if any(part in DEFAULT_IGNORES for part in relative.parts):
            return True

        parts = relative.parts
        for part_count in range(1, len(parts)):
            ancestor = self.root.joinpath(*parts[:part_count])
            if _matches_gitignore_rules(self._rules, ancestor, is_directory=True):
                return True

        return _matches_gitignore_rules(
            self._rules,
            candidate,
            is_directory=candidate.is_dir(),
        )

    def is_binary(self, path: Path) -> bool:
        return path.suffix.lower() in BINARY_SUFFIXES


def load_ignore_policy(workspace: str | Path) -> IgnorePolicy:
    root = Path(workspace).resolve()
    rules: list[_GitIgnoreRules] = []
    gitignore_files: list[Path] = []

    for current_directory, directory_names, file_names in os.walk(root, topdown=True):
        directory = Path(current_directory)
        directory_names.sort()
        file_names.sort()

        if ".gitignore" in file_names:
            gitignore_path = directory / ".gitignore"
            spec = GitIgnoreSpec.from_lines(
                gitignore_path.read_text(
                    encoding="utf-8-sig",
                    errors="replace",
                ).splitlines()
            )
            rules.append(
                _GitIgnoreRules(
                    path=gitignore_path,
                    directory=directory,
                    spec=spec,
                )
            )
            gitignore_files.append(gitignore_path)

        retained_directories: list[str] = []
        for name in directory_names:
            candidate = directory / name
            relative = candidate.relative_to(root)
            if any(part in DEFAULT_IGNORES for part in relative.parts):
                continue
            if _matches_gitignore_rules(tuple(rules), candidate, is_directory=True):
                continue
            retained_directories.append(name)
        directory_names[:] = retained_directories

    return IgnorePolicy(
        root=root,
        gitignore_files=tuple(gitignore_files),
        _rules=tuple(rules),
    )


def _matches_gitignore_rules(
    rules: tuple[_GitIgnoreRules, ...],
    path: Path,
    *,
    is_directory: bool,
) -> bool:
    ignored = False

    for rule_group in rules:
        try:
            relative = path.relative_to(rule_group.directory)
        except ValueError:
            continue

        relative_path = relative.as_posix()
        if relative_path == ".":
            continue
        if is_directory:
            relative_path = f"{relative_path.rstrip('/')}/"

        result = rule_group.spec.check_file(relative_path)
        if result.include is not None:
            ignored = result.include

    return ignored


def _absolute_without_resolving_symlinks(root: Path, path: Path) -> Path:
    candidate = path if path.is_absolute() else root / path
    return Path(os.path.abspath(candidate))


def _relative_to_root(root: Path, path: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path is outside ignore policy root: {path}") from exc

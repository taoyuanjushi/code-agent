from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .path_safety import ensure_parent_directory, resolve_inside_workspace

PatchChangeType = Literal["add", "modify", "delete"]


@dataclass(frozen=True)
class HunkLine:
    type: Literal["context", "add", "remove"]
    content: str


@dataclass
class ParsedHunk:
    old_start: int
    old_length: int
    new_start: int
    new_length: int
    lines: list[HunkLine] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedPatchFile:
    old_path: str | None
    new_path: str | None
    hunks: list[ParsedHunk]


@dataclass(frozen=True)
class FilePatchPlan:
    path: str
    absolute_path: Path
    change_type: PatchChangeType
    before_content: str | None
    after_content: str | None


@dataclass(frozen=True)
class PatchPlan:
    files: list[FilePatchPlan]


@dataclass(frozen=True)
class SplitContent:
    lines: list[str]
    newline: Literal["\n", "\r\n"]
    has_final_newline: bool


def plan_patch(workspace: str | Path, patch: str) -> PatchPlan:
    parsed_files = parse_unified_diff(patch)
    files = [_plan_file_patch(workspace, parsed_file) for parsed_file in parsed_files]
    return PatchPlan(files=files)


def apply_patch_plan(plan: PatchPlan) -> None:
    for file in plan.files:
        if file.after_content is None:
            file.absolute_path.unlink()
            continue

        ensure_parent_directory(file.absolute_path)
        file.absolute_path.write_text(file.after_content, encoding="utf-8")


def summarize_patch_plan(plan: PatchPlan) -> str:
    if not plan.files:
        return "No file changes."

    lines = []
    for file in plan.files:
        before_lines = _count_lines(file.before_content)
        after_lines = _count_lines(file.after_content)
        lines.append(f"{file.change_type:<6} {file.path} ({before_lines} -> {after_lines} lines)")
    return "\n".join(lines)


def parse_unified_diff(patch: str) -> list[ParsedPatchFile]:
    lines = patch.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    files: list[ParsedPatchFile] = []
    index = 0

    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue

        old_path = _parse_diff_path(lines[index], "--- ")
        index += 1

        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise ValueError("Invalid unified diff: missing +++ file header.")

        new_path = _parse_diff_path(lines[index], "+++ ")
        index += 1

        hunks: list[ParsedHunk] = []
        while index < len(lines):
            line = lines[index]
            if line.startswith("--- ") or line.startswith("diff --git "):
                break

            if not line.startswith("@@ "):
                index += 1
                continue

            hunk = _parse_hunk_header(line)
            index += 1

            while index < len(lines):
                hunk_line = lines[index]
                if (
                    hunk_line.startswith("@@ ")
                    or hunk_line.startswith("--- ")
                    or hunk_line.startswith("diff --git ")
                ):
                    break

                if hunk_line.startswith("\\ No newline at end of file"):
                    index += 1
                    continue

                marker = hunk_line[0] if hunk_line else ""
                if marker not in {" ", "+", "-"}:
                    if hunk_line == "" and index == len(lines) - 1:
                        index += 1
                        continue
                    raise ValueError(f"Invalid unified diff hunk line: {hunk_line}")

                hunk.lines.append(
                    HunkLine(
                        type="context" if marker == " " else "add" if marker == "+" else "remove",
                        content=hunk_line[1:],
                    )
                )
                index += 1

            _validate_hunk_line_counts(hunk)
            hunks.append(hunk)

        if not hunks:
            raise ValueError("Invalid unified diff: file section has no hunks.")

        files.append(ParsedPatchFile(old_path=old_path, new_path=new_path, hunks=hunks))

    if not files:
        raise ValueError("Patch did not contain any unified diff file sections.")

    return files


def _plan_file_patch(workspace: str | Path, patch_file: ParsedPatchFile) -> FilePatchPlan:
    if (
        patch_file.old_path is not None
        and patch_file.new_path is not None
        and patch_file.old_path != patch_file.new_path
    ):
        raise ValueError(f"Renames are not supported yet: {patch_file.old_path} -> {patch_file.new_path}")

    relative_path = patch_file.new_path or patch_file.old_path
    if relative_path is None:
        raise ValueError("Invalid patch: both old and new paths are /dev/null.")

    absolute_path = resolve_inside_workspace(workspace, relative_path)
    change_type = _get_change_type(patch_file)
    before_content = _read_existing_content(absolute_path)

    if change_type == "add" and before_content is not None:
        raise ValueError(f"Cannot add file because it already exists: {relative_path}")

    if change_type in {"modify", "delete"} and before_content is None:
        raise ValueError(f"Cannot {change_type} missing file: {relative_path}")

    before = _split_content(before_content or "")
    after_lines = _apply_hunks(before.lines, patch_file.hunks, relative_path)
    after_content = (
        None
        if change_type == "delete"
        else _join_content(
            after_lines,
            before.newline,
            _choose_final_newline(change_type, before, after_lines),
        )
    )

    return FilePatchPlan(
        path=relative_path,
        absolute_path=absolute_path,
        change_type=change_type,
        before_content=before_content,
        after_content=after_content,
    )


def _apply_hunks(base_lines: list[str], hunks: list[ParsedHunk], relative_path: str) -> list[str]:
    output: list[str] = []
    cursor = 0

    for hunk in hunks:
        hunk_start = 0 if hunk.old_start == 0 else hunk.old_start - 1
        if hunk_start < cursor or hunk_start > len(base_lines):
            raise ValueError(f"Patch hunk is out of range for {relative_path}.")

        output.extend(base_lines[cursor:hunk_start])
        cursor = hunk_start

        for line in hunk.lines:
            if line.type == "add":
                output.append(line.content)
                continue

            if cursor >= len(base_lines) or base_lines[cursor] != line.content:
                raise ValueError(
                    f'Patch context mismatch in {relative_path} at line {cursor + 1}: expected "{line.content}".'
                )

            if line.type == "context":
                output.append(line.content)

            cursor += 1

    output.extend(base_lines[cursor:])
    return output


def _parse_diff_path(line: str, prefix: Literal["--- ", "+++ "]) -> str | None:
    raw = line[len(prefix) :].strip()
    token = raw.split()[0] if raw.split() else ""

    if not token:
        raise ValueError(f"Invalid unified diff path header: {line}")

    if token == "/dev/null":
        return None

    unquoted = token[1:-1] if token.startswith('"') and token.endswith('"') else token
    return _strip_git_path_prefix(unquoted)


def _strip_git_path_prefix(value: str) -> str:
    if value.startswith(("a/", "b/")):
        return value[2:]
    return value


def _parse_hunk_header(line: str) -> ParsedHunk:
    import re

    match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
    if not match:
        raise ValueError(f"Invalid unified diff hunk header: {line}")

    return ParsedHunk(
        old_start=int(match.group(1)),
        old_length=int(match.group(2) or "1"),
        new_start=int(match.group(3)),
        new_length=int(match.group(4) or "1"),
    )


def _validate_hunk_line_counts(hunk: ParsedHunk) -> None:
    old_count = sum(1 for line in hunk.lines if line.type != "add")
    new_count = sum(1 for line in hunk.lines if line.type != "remove")

    if old_count != hunk.old_length or new_count != hunk.new_length:
        raise ValueError(
            f"Invalid unified diff hunk counts: expected -{hunk.old_length}/+{hunk.new_length}, "
            f"got -{old_count}/+{new_count}."
        )


def _read_existing_content(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _get_change_type(patch_file: ParsedPatchFile) -> PatchChangeType:
    if patch_file.old_path is None:
        return "add"
    if patch_file.new_path is None:
        return "delete"
    return "modify"


def _split_content(content: str) -> SplitContent:
    newline: Literal["\n", "\r\n"] = "\r\n" if "\r\n" in content else "\n"
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    has_final_newline = normalized.endswith("\n")
    lines = [] if normalized == "" else normalized.split("\n")

    if has_final_newline:
        lines.pop()

    return SplitContent(lines=lines, newline=newline, has_final_newline=has_final_newline)


def _join_content(lines: list[str], newline: Literal["\n", "\r\n"], has_final_newline: bool) -> str:
    if not lines:
        return newline if has_final_newline else ""
    return f"{newline.join(lines)}{newline if has_final_newline else ''}"


def _choose_final_newline(
    change_type: PatchChangeType,
    before: SplitContent,
    after_lines: list[str],
) -> bool:
    if not after_lines:
        return False
    if change_type == "add":
        return True
    return before.has_final_newline


def _count_lines(content: str | None) -> int:
    if not content:
        return 0
    return len(_split_content(content).lines)

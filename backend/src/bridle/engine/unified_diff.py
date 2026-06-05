"""Unified diff parsing, dry-run apply, and patch validation."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    context_lines: tuple[str, ...] = ()
    added_lines: tuple[str, ...] = ()
    removed_lines: tuple[str, ...] = ()
    old_lines: tuple[str, ...] = ()
    new_lines: tuple[str, ...] = ()
    no_newline_marker: bool = False


@dataclass(frozen=True)
class ParsedDiff:
    hunks: tuple[DiffHunk, ...]
    old_path: str = ""
    new_path: str = ""


@dataclass(frozen=True)
class DryRunResult:
    valid: bool
    hunk_count: int = 0
    added_lines: int = 0
    removed_lines: int = 0
    new_text: str = ""
    error: str = ""


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    error: str = ""
    recovery_hint: str = ""
    dry_run: DryRunResult | None = None


_HUNK_HEADER = "@@ -{old_start},{old_count} +{new_start},{new_count} @@"


def recovery_hint_for_error(error: str, *, change_type: str = "") -> str:
    lower = error.lower()
    if "empty diff" in lower:
        return "Provide a unified diff with ---/+++ headers and at least one @@ hunk."
    if "no hunks" in lower:
        return "Include one or more @@ -old,+new @@ hunks with context lines."
    if "invalid hunk header" in lower:
        return "Use @@ -<start>,<count> +<start>,<count> @@ with matching context lines."
    if "context" in lower and "mismatch" in lower:
        return "Re-read the file and align diff context lines exactly with the current content."
    if "add diff must not contain removed" in lower:
        return "For add, only + lines are allowed; use modify to change an existing file."
    if "cannot add file that already exists" in lower:
        return "Use change_type modify with a patch against the existing file content."
    if "cannot modify file that does not exist" in lower:
        return "Use change_type add with a new-file diff (--- /dev/null)."
    if "cannot remove file that does not exist" in lower:
        return "Only remove paths that exist under the sandbox workspace."
    if "remove diff must delete" in lower:
        return "Remove patches must delete all lines; use a single hunk removing the full file."
    if change_type == "remove":
        return "Use --- path and +++ path headers with hunks that remove every line."
    return "Verify ---/+++ paths, hunk line numbers, and that context lines match the file."
_DIFF_HEADER_OLD = "--- "
_DIFF_HEADER_NEW = "+++ "
_DEV_NULL = "/dev/null"


def parse_unified_diff(diff: str) -> ParsedDiff:
    if not diff or not diff.strip():
        raise ValueError("Empty diff string")
    lines = diff.splitlines()
    hunks: list[DiffHunk] = []
    old_path = ""
    new_path = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(_DIFF_HEADER_OLD):
            old_path = line[len(_DIFF_HEADER_OLD):].strip()
            i += 1
            continue
        if line.startswith(_DIFF_HEADER_NEW):
            new_path = line[len(_DIFF_HEADER_NEW):].strip()
            i += 1
            continue
        if line.startswith("@@"):
            hunk, consumed = _parse_hunk(lines, i)
            if hunk is None:
                raise ValueError(f"Invalid hunk header at line {i + 1}")
            hunks.append(hunk)
            i += consumed
            continue
        i += 1
    if not hunks:
        raise ValueError("No hunks found in diff")
    return ParsedDiff(hunks=tuple(hunks), old_path=old_path, new_path=new_path)


def _parse_hunk(lines: list[str], start: int) -> tuple[DiffHunk | None, int]:
    import re
    header_match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", lines[start])
    if not header_match:
        return None, 1
    old_start = int(header_match.group(1))
    old_count = int(header_match.group(2) or "1")
    new_start = int(header_match.group(3))
    new_count = int(header_match.group(4) or "1")
    context: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    old_side: list[str] = []
    new_side: list[str] = []
    no_newline_marker = False
    i = start + 1
    while i < len(lines):
        line = lines[i]
        if line.startswith("@@") or line.startswith(_DIFF_HEADER_OLD) or line.startswith(_DIFF_HEADER_NEW):
            break
        if line.startswith(" "):
            context.append(line[1:])
            old_side.append(line[1:])
            new_side.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
            new_side.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
            old_side.append(line[1:])
        elif line.startswith("\\"):
            if "No newline at end of file" in line:
                no_newline_marker = True
        else:
            context.append(line)
            old_side.append(line)
            new_side.append(line)
        i += 1
    consumed = i - start
    return DiffHunk(
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        context_lines=tuple(context),
        added_lines=tuple(added),
        removed_lines=tuple(removed),
        old_lines=tuple(old_side),
        new_lines=tuple(new_side),
        no_newline_marker=no_newline_marker,
    ), consumed


def dry_run_apply(original_text: str, parsed_diff: ParsedDiff) -> DryRunResult:
    if not parsed_diff.hunks:
        return DryRunResult(valid=False, error="No hunks to apply")
    if any(h.no_newline_marker for h in parsed_diff.hunks):
        return DryRunResult(valid=False, error="No newline at end of file marker is not supported")
    original_lines = original_text.splitlines(keepends=True)
    result_lines = list(original_lines)
    total_added = 0
    total_removed = 0
    offset = 0
    for hunk in parsed_diff.hunks:
        start_idx = hunk.old_start - 1 + offset
        if start_idx < 0 or start_idx > len(result_lines):
            return DryRunResult(
                valid=False,
                error=(
                    f"Hunk starts at line {hunk.old_start}, "
                    f"but file has {len(result_lines)} lines"
                ),
            )
        actual_old = [line.rstrip("\r\n") for line in result_lines[start_idx:start_idx + hunk.old_count]]
        if len(actual_old) != len(hunk.old_lines):
            return DryRunResult(
                valid=False,
                error=(
                    f"Hunk expects {len(hunk.old_lines)} lines at line {hunk.old_start}, "
                    f"but file has {len(actual_old)} lines"
                ),
            )
        for ci, (expected, actual) in enumerate(zip(hunk.old_lines, actual_old, strict=True)):
            if expected != actual:
                return DryRunResult(valid=False, error=f"Context mismatch at line {hunk.old_start + ci}")
        replacement = [line + "\n" for line in hunk.new_lines]
        total_added += len(hunk.added_lines)
        total_removed += len(hunk.removed_lines)
        result_lines[start_idx:start_idx + hunk.old_count] = replacement
        offset += len(replacement) - hunk.old_count
    return DryRunResult(
        valid=True,
        hunk_count=len(parsed_diff.hunks),
        added_lines=total_added,
        removed_lines=total_removed,
        new_text="".join(result_lines),
    )


def validate_patch_for_path(
    path: str,
    change_type: str,
    diff: str,
    original_text: str | None = None,
    file_exists: bool = False,
) -> ValidationResult:
    if change_type not in ("modify", "add", "remove"):
        err = f"Unsupported change_type: {change_type}"
        return ValidationResult(
            valid=False,
            error=err,
            recovery_hint=recovery_hint_for_error(err, change_type=change_type),
        )
    try:
        parsed = parse_unified_diff(diff)
    except ValueError as exc:
        err = str(exc)
        return ValidationResult(
            valid=False,
            error=err,
            recovery_hint=recovery_hint_for_error(err, change_type=change_type),
        )
    path_error = _validate_header_paths(path, change_type, parsed)
    if path_error:
        return ValidationResult(
            valid=False,
            error=path_error,
            recovery_hint=recovery_hint_for_error(path_error, change_type=change_type),
        )
    if change_type == "add" and file_exists:
        err = f"Cannot add file that already exists: {path}"
        return ValidationResult(
            valid=False,
            error=err,
            recovery_hint=recovery_hint_for_error(err, change_type=change_type),
        )
    if change_type == "remove" and not file_exists:
        err = f"Cannot remove file that does not exist: {path}"
        return ValidationResult(
            valid=False,
            error=err,
            recovery_hint=recovery_hint_for_error(err, change_type=change_type),
        )
    if change_type == "modify":
        if not file_exists:
            err = f"Cannot modify file that does not exist: {path}"
            return ValidationResult(
                valid=False,
                error=err,
                recovery_hint=recovery_hint_for_error(err, change_type=change_type),
            )
        if original_text is None:
            err = "original_text required for modify"
            return ValidationResult(
                valid=False,
                error=err,
                recovery_hint=recovery_hint_for_error(err, change_type=change_type),
            )
        dr = dry_run_apply(original_text, parsed)
        if not dr.valid:
            return ValidationResult(
                valid=False,
                error=dr.error,
                recovery_hint=recovery_hint_for_error(dr.error, change_type=change_type),
                dry_run=dr,
            )
        return ValidationResult(valid=True, dry_run=dr)
    if change_type == "add":
        removed_count = sum(len(h.removed_lines) for h in parsed.hunks)
        old_line_count = sum(len(h.old_lines) for h in parsed.hunks)
        if removed_count or old_line_count:
            err = "Add diff must not contain removed lines"
            return ValidationResult(
                valid=False,
                error=err,
                recovery_hint=recovery_hint_for_error(err, change_type=change_type),
            )
        dr = DryRunResult(
            valid=True,
            hunk_count=len(parsed.hunks),
            added_lines=sum(len(h.added_lines) for h in parsed.hunks),
            removed_lines=0,
            new_text="\n".join(line for h in parsed.hunks for line in h.new_lines) + (
                "\n" if any(h.new_lines for h in parsed.hunks) else ""
            ),
        )
        return ValidationResult(valid=True, dry_run=dr)
    if change_type == "remove":
        if original_text is None:
            err = "original_text required for remove"
            return ValidationResult(
                valid=False,
                error=err,
                recovery_hint=recovery_hint_for_error(err, change_type=change_type),
            )
        dr = dry_run_apply(original_text, parsed)
        if not dr.valid:
            return ValidationResult(
                valid=False,
                error=dr.error,
                recovery_hint=recovery_hint_for_error(dr.error, change_type=change_type),
                dry_run=dr,
            )
        if dr.new_text:
            err = "Remove diff must delete the full file content"
            failed = DryRunResult(
                valid=False,
                hunk_count=dr.hunk_count,
                added_lines=dr.added_lines,
                removed_lines=dr.removed_lines,
                new_text=dr.new_text,
                error=err,
            )
            return ValidationResult(
                valid=False,
                error=failed.error,
                recovery_hint=recovery_hint_for_error(err, change_type=change_type),
                dry_run=failed,
            )
        dr = DryRunResult(
            valid=True,
            hunk_count=len(parsed.hunks),
            added_lines=0,
            removed_lines=sum(len(h.removed_lines) for h in parsed.hunks),
        )
        return ValidationResult(valid=True, dry_run=dr)
    return ValidationResult(valid=False, error="Unreachable")


def _normalize_diff_path(raw_path: str) -> str:
    text = raw_path.strip()
    if not text:
        return ""
    path = text.split("\t", 1)[0].split(" ", 1)[0]
    if path == _DEV_NULL:
        return _DEV_NULL
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _validate_header_paths(path: str, change_type: str, parsed: ParsedDiff) -> str:
    target = _normalize_diff_path(path)
    old_path = _normalize_diff_path(parsed.old_path)
    new_path = _normalize_diff_path(parsed.new_path)

    if change_type == "modify":
        if old_path and old_path != target:
            return f"Diff old path '{old_path}' does not match target path '{target}'"
        if new_path and new_path != target:
            return f"Diff new path '{new_path}' does not match target path '{target}'"
    elif change_type == "add":
        if old_path and old_path != _DEV_NULL:
            return f"Add diff old path must be /dev/null, got '{old_path}'"
        if new_path and new_path != target:
            return f"Diff new path '{new_path}' does not match target path '{target}'"
    elif change_type == "remove":
        if old_path and old_path != target:
            return f"Diff old path '{old_path}' does not match target path '{target}'"
        if new_path and new_path != _DEV_NULL:
            return f"Remove diff new path must be /dev/null, got '{new_path}'"
    return ""

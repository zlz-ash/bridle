"""Tests for unified_diff module."""
from __future__ import annotations

import pytest

from bridle.engine.unified_diff import (
    DryRunResult,
    ParsedDiff,
    ValidationResult,
    dry_run_apply,
    parse_unified_diff,
    validate_patch_for_path,
)


class TestParseUnifiedDiff:
    def test_single_hunk(self) -> None:
        diff = "--- a.py\n+++ b.py\n@@ -1,3 +1,3 @@\n line1\n-old\n+new\n line3\n"
        parsed = parse_unified_diff(diff)
        assert len(parsed.hunks) == 1
        assert parsed.old_path == "a.py"
        assert parsed.new_path == "b.py"
        hunk = parsed.hunks[0]
        assert hunk.old_start == 1
        assert hunk.old_count == 3
        assert hunk.new_start == 1
        assert hunk.new_count == 3

    def test_multiple_hunks(self) -> None:
        diff = (
            "--- a.py\n+++ b.py\n"
            "@@ -1,3 +1,3 @@\n line1\n-old1\n+new1\n line3\n"
            "@@ -10,2 +10,2 @@\n ctx\n-old2\n+new2\n"
        )
        parsed = parse_unified_diff(diff)
        assert len(parsed.hunks) == 2

    def test_empty_diff_raises(self) -> None:
        with pytest.raises(ValueError, match="Empty diff"):
            parse_unified_diff("")

    def test_no_hunks_raises(self) -> None:
        with pytest.raises(ValueError, match="No hunks"):
            parse_unified_diff("--- a.py\n+++ b.py\n")

    def test_invalid_hunk_header_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_unified_diff("@@ invalid @@\n")

    def test_hunk_without_count_defaults_to_one(self) -> None:
        diff = "@@ -1 +1 @@\n-old\n+new\n"
        parsed = parse_unified_diff(diff)
        assert parsed.hunks[0].old_count == 1
        assert parsed.hunks[0].new_count == 1


class TestDryRunApply:
    def test_simple_replacement(self) -> None:
        original = "line1\nold\nline3\n"
        diff = "@@ -1,3 +1,3 @@\nline1\n-old\n+new\nline3\n"
        parsed = parse_unified_diff(diff)
        result = dry_run_apply(original, parsed)
        assert result.valid is True
        assert result.added_lines == 1
        assert result.removed_lines == 1
        assert "new" in result.new_text
        assert "old" not in result.new_text

    def test_add_lines(self) -> None:
        original = "line1\nline2\n"
        diff = "@@ -1,2 +1,3 @@\nline1\n+inserted\nline2\n"
        parsed = parse_unified_diff(diff)
        result = dry_run_apply(original, parsed)
        assert result.valid is True
        assert result.added_lines == 1
        assert "inserted" in result.new_text

    def test_remove_lines(self) -> None:
        original = "line1\nremoved\nline3\n"
        diff = "@@ -1,3 +1,2 @@\nline1\n-removed\nline3\n"
        parsed = parse_unified_diff(diff)
        result = dry_run_apply(original, parsed)
        assert result.valid is True
        assert result.removed_lines == 1
        assert "removed" not in result.new_text

    def test_context_mismatch_fails(self) -> None:
        original = "line1\ndifferent\nline3\n"
        diff = "@@ -1,3 +1,3 @@\nline1\n-old\n+new\nline3\n"
        parsed = parse_unified_diff(diff)
        result = dry_run_apply(original, parsed)
        assert result.valid is False
        assert result.error

    def test_trailing_whitespace_mismatch_fails(self) -> None:
        original = "line1\nvalue \nline3\n"
        diff = "@@ -1,3 +1,3 @@\nline1\n-value\n+changed\nline3\n"
        parsed = parse_unified_diff(diff)
        result = dry_run_apply(original, parsed)
        assert result.valid is False
        assert "Context mismatch" in result.error

    def test_no_newline_marker_is_rejected_when_unsupported(self) -> None:
        original = "line1\nold"
        diff = "@@ -1,2 +1,2 @@\nline1\n-old\n+new\n\\ No newline at end of file\n"
        parsed = parse_unified_diff(diff)
        result = dry_run_apply(original, parsed)
        assert result.valid is False
        assert "newline" in result.error.lower()

    def test_hunk_out_of_range_fails(self) -> None:
        original = "line1\n"
        diff = "@@ -100,1 +100,1 @@\n-old\n+new\n"
        parsed = parse_unified_diff(diff)
        result = dry_run_apply(original, parsed)
        assert result.valid is False

    def test_multiple_hunks_applied(self) -> None:
        original = "a\nb\nc\nd\ne\n"
        diff = (
            "@@ -1,2 +1,2 @@\n-a\n+A\nb\n"
            "@@ -4,2 +4,2 @@\nd\n-e\n+E\n"
        )
        parsed = parse_unified_diff(diff)
        result = dry_run_apply(original, parsed)
        assert result.valid is True
        assert result.hunk_count == 2
        assert "A" in result.new_text
        assert "E" in result.new_text
        assert "a" not in result.new_text.split("\n")[0]


class TestValidatePatchForPath:
    def test_modify_valid(self) -> None:
        original = "hello\nworld\n"
        diff = "@@ -1,2 +1,2 @@\n-hello\n+Hello\nworld\n"
        result = validate_patch_for_path("a.py", "modify", diff, original_text=original, file_exists=True)
        assert result.valid is True
        assert result.dry_run is not None
        assert result.dry_run.valid is True

    def test_modify_rejects_mismatched_diff_header_path(self) -> None:
        original = "hello\nworld\n"
        diff = "--- a/other.py\n+++ b/other.py\n@@ -1,2 +1,2 @@\n-hello\n+Hello\nworld\n"
        result = validate_patch_for_path("src/a.py", "modify", diff, original_text=original, file_exists=True)
        assert result.valid is False
        assert "path" in result.error.lower()

    def test_modify_accepts_a_b_header_prefix_for_target_path(self) -> None:
        original = "hello\nworld\n"
        diff = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,2 @@\n-hello\n+Hello\nworld\n"
        result = validate_patch_for_path("src/a.py", "modify", diff, original_text=original, file_exists=True)
        assert result.valid is True

    def test_modify_nonexistent_file_fails(self) -> None:
        diff = "@@ -1,1 +1,1 @@\n-old\n+new\n"
        result = validate_patch_for_path("a.py", "modify", diff, original_text=None, file_exists=False)
        assert result.valid is False
        assert "does not exist" in result.error

    def test_add_over_existing_fails(self) -> None:
        diff = "@@ -0,0 +1,1 @@\n+new file\n"
        result = validate_patch_for_path("a.py", "add", diff, original_text=None, file_exists=True)
        assert result.valid is False
        assert "already exists" in result.error

    def test_remove_nonexistent_fails(self) -> None:
        diff = "@@ -1,1 +0,0 @@\n-old\n"
        result = validate_patch_for_path("a.py", "remove", diff, original_text=None, file_exists=False)
        assert result.valid is False
        assert "does not exist" in result.error

    def test_invalid_change_type(self) -> None:
        result = validate_patch_for_path("a.py", "delete", "diff", original_text=None, file_exists=False)
        assert result.valid is False
        assert "Unsupported" in result.error

    def test_invalid_diff_format(self) -> None:
        result = validate_patch_for_path("a.py", "modify", "not a diff", original_text="x", file_exists=True)
        assert result.valid is False

    def test_add_new_file_succeeds(self) -> None:
        diff = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+line1\n+line2\n"
        result = validate_patch_for_path("new.py", "add", diff, original_text=None, file_exists=False)
        assert result.valid is True
        assert result.dry_run is not None
        assert result.dry_run.added_lines == 2

    def test_add_rejects_removed_lines(self) -> None:
        diff = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n-old\n+line1\n"
        result = validate_patch_for_path("new.py", "add", diff, original_text=None, file_exists=False)
        assert result.valid is False
        assert "remove" in result.error.lower() or "deleted" in result.error.lower()

    def test_add_rejects_mismatched_new_path(self) -> None:
        diff = "--- /dev/null\n+++ b/other.py\n@@ -0,0 +1,1 @@\n+line1\n"
        result = validate_patch_for_path("new.py", "add", diff, original_text=None, file_exists=False)
        assert result.valid is False
        assert "path" in result.error.lower()

    def test_remove_existing_file_succeeds(self) -> None:
        diff = "--- a/old.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-old content\n"
        result = validate_patch_for_path("old.py", "remove", diff, original_text="old content\n", file_exists=True)
        assert result.valid is True
        assert result.dry_run is not None
        assert result.dry_run.valid is True
        assert result.dry_run.removed_lines == 1

    def test_remove_rejects_mismatched_old_path(self) -> None:
        diff = "--- a/other.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-old content\n"
        result = validate_patch_for_path("old.py", "remove", diff, original_text="old content\n", file_exists=True)
        assert result.valid is False
        assert "path" in result.error.lower()

    def test_remove_rejects_content_mismatch(self) -> None:
        diff = "--- a/old.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-old content\n"
        result = validate_patch_for_path("old.py", "remove", diff, original_text="different\n", file_exists=True)
        assert result.valid is False
        assert result.dry_run is not None
        assert result.dry_run.valid is False

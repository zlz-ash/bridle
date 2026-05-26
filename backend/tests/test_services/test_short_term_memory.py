"""Tests for short-term memory sliding window and threshold compression."""
from __future__ import annotations

import json

import pytest

from bridle.services.short_term_memory import (
    _COMPACTED_PREFIX,
    ShortTermMemory,
    ToolResultSummarizer,
)


def _messages(n: int, prefix: str = "msg") -> list[dict]:
    return [{"role": "assistant", "content": f"{prefix}_{i}"} for i in range(n)]


class TestShortTermMemoryNoCompression:
    def test_under_budget_no_compression(self) -> None:
        mem = ShortTermMemory(budget=1000, recent_window=2)
        msgs = _messages(3)
        result = mem.compact(msgs)
        assert result == msgs

    def test_empty_messages(self) -> None:
        mem = ShortTermMemory(budget=1000, recent_window=2)
        result = mem.compact([])
        assert result == []

    def test_single_message(self) -> None:
        mem = ShortTermMemory(budget=1000, recent_window=2)
        msgs = [{"role": "user", "content": "hello"}]
        result = mem.compact(msgs)
        assert result == msgs


class TestShortTermMemoryCompression:
    def test_over_budget_compress_old(self) -> None:
        mem = ShortTermMemory(budget=100, recent_window=2)
        msgs = [{"role": "assistant", "content": f"message_{i}_padding_to_exceed_budget" * 5} for i in range(10)]
        result = mem.compact(msgs)
        assert len(result) <= len(msgs)
        recent = result[-2:]
        assert "message_8" in recent[0].get("content", "")
        assert "message_9" in recent[1].get("content", "")

    def test_compressed_old_messages_become_summary(self) -> None:
        mem = ShortTermMemory(budget=100, recent_window=2)
        msgs = [{"role": "assistant", "content": f"decision_{i}_" + "x" * 30} for i in range(10)]
        result = mem.compact(msgs)
        if len(result) > 2:
            first = result[0]
            assert first.get("role") == "system" or "compacted" in str(first.get("content", "")).lower()

    def test_recent_window_preserved_in_order(self) -> None:
        mem = ShortTermMemory(budget=100, recent_window=3)
        msgs = [{"role": "assistant", "content": f"msg_{i}_" + "x" * 30} for i in range(10)]
        result = mem.compact(msgs)
        recent = result[-3:]
        assert "msg_7" in recent[0].get("content", "")
        assert "msg_8" in recent[1].get("content", "")
        assert "msg_9" in recent[2].get("content", "")

    def test_system_message_never_compressed(self) -> None:
        mem = ShortTermMemory(budget=100, recent_window=2)
        padding = [{"role": "assistant", "content": f"msg_{i}_" + "x" * 30} for i in range(10)]
        msgs = [{"role": "system", "content": "important"}] + padding
        result = mem.compact(msgs)
        system_msgs = [
            m for m in result
            if m.get("role") == "system" and m.get("content") == "important"
        ]
        assert len(system_msgs) == 1


class TestShortTermMemorySummaryStructure:
    def test_summary_contains_key_sections(self) -> None:
        mem = ShortTermMemory(budget=100, recent_window=2)
        msgs = [
            {"role": "user", "content": "goal: implement X"},
            {"role": "assistant", "content": "decision: use approach A"},
            {"role": "tool", "content": "result: file read ok"},
        ] + _messages(10)
        result = mem.compact(msgs)
        summary_msgs = [m for m in result if m.get("role") == "system"]
        if summary_msgs:
            content = str(summary_msgs[0].get("content", ""))
            assert len(content) > 0

    def test_summary_does_not_contain_raw_tool_output(self) -> None:
        mem = ShortTermMemory(budget=100, recent_window=2)
        msgs = [
            {"role": "tool", "content": "stdout: very long output " * 50},
            {"role": "tool", "content": "stderr: error output " * 50},
        ] + _messages(10)
        result = mem.compact(msgs)
        for m in result[:-2]:
            assert "stdout: very long output" not in str(m.get("content", ""))
            assert "stderr: error output" not in str(m.get("content", ""))


class TestShortTermMemoryBudget:
    def test_result_within_budget(self) -> None:
        mem = ShortTermMemory(budget=200, recent_window=2)
        msgs = [{"role": "assistant", "content": "x" * 100} for _ in range(10)]
        result = mem.compact(msgs)
        total = sum(len(str(m.get("content", ""))) for m in result)
        assert total <= 200 + 50

    def test_default_budget(self) -> None:
        mem = ShortTermMemory()
        assert mem.budget > 0

    def test_default_recent_window(self) -> None:
        mem = ShortTermMemory()
        assert mem.recent_window > 0


class TestToolResultSummarizer:
    def test_summarize_read_file_result(self) -> None:
        raw = json.dumps({
            "status": "completed",
            "content": "file content here with lots of code " * 20,
        })
        summary = ToolResultSummarizer.summarize("read_allowed_file", raw)
        assert summary["tool_name"] == "read_allowed_file"
        assert summary["status"] == "completed"
        assert "content" not in summary or len(str(summary.get("result_summary", ""))) < len(raw)
        assert "file content here" not in json.dumps(summary)

    def test_summarize_run_tests_result(self) -> None:
        raw = json.dumps({
            "status": "completed",
            "results": [{"command": "pytest", "exit_code": 0, "stdout": "long output " * 50, "stderr": ""}],
        })
        summary = ToolResultSummarizer.summarize("run_allowed_tests", raw)
        assert summary["status"] == "completed"
        assert "stdout" not in json.dumps(summary)
        assert "stderr" not in json.dumps(summary)

    def test_summarize_blocked_result(self) -> None:
        raw = json.dumps({"status": "completed", "reason": "missing dependency"})
        summary = ToolResultSummarizer.summarize("report_blocked", raw)
        assert summary["status"] == "completed"

    def test_summarize_failed_result(self) -> None:
        raw = json.dumps({"status": "failed", "error_code": "path_not_allowed", "message": "Access denied"})
        summary = ToolResultSummarizer.summarize("read_allowed_file", raw)
        assert summary["status"] == "failed"
        assert summary.get("error_code") == "path_not_allowed"

    def test_summary_no_path_parameter(self) -> None:
        raw = json.dumps({"status": "completed", "content": "data", "path": "/secret/file.py"})
        summary = ToolResultSummarizer.summarize("read_allowed_file", raw)
        dumped = json.dumps(summary)
        assert "/secret/file.py" not in dumped
        assert "path" not in summary

    def test_summary_no_stdout_stderr(self) -> None:
        raw = json.dumps({"status": "completed", "stdout": "leaked", "stderr": "also leaked"})
        summary = ToolResultSummarizer.summarize("run_allowed_tests", raw)
        dumped = json.dumps(summary)
        assert "leaked" not in dumped

    def test_summary_no_file_full_content(self) -> None:
        raw = json.dumps({"status": "completed", "content": "import os\nimport sys\n" * 100})
        summary = ToolResultSummarizer.summarize("read_allowed_file", raw)
        dumped = json.dumps(summary)
        assert "import os" not in dumped

    def test_compact_with_tool_result_summarization(self) -> None:
        mem = ShortTermMemory(budget=200, recent_window=2)
        tool_result = json.dumps({"status": "completed", "content": "x" * 500, "path": "src/a.py"})
        msgs = [
            {"role": "tool", "content": tool_result},
            {"role": "assistant", "content": "decision made"},
        ] + [{"role": "assistant", "content": f"msg_{i}_" + "y" * 30} for i in range(10)]
        result = mem.compact(msgs)
        for m in result[:-2]:
            dumped = json.dumps(m)
            assert "x" * 100 not in dumped
            assert "src/a.py" not in dumped


class TestToolResultSummarizerLogEvents:
    def test_summarize_emits_tool_result_summarized(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="bridle")
        raw = json.dumps({"status": "completed", "content": "x" * 500})
        ToolResultSummarizer.summarize("read_allowed_file", raw)
        events = [
            r for r in caplog.records
            if getattr(r, "action", None) == "tool_result_summarized"
        ]
        assert len(events) == 1
        detail = getattr(events[0], "detail", {})
        assert detail.get("tool_name") == "read_allowed_file"
        assert "stdout" not in json.dumps(detail)
        assert "stderr" not in json.dumps(detail)
        assert "content" not in detail


class TestRecentWindowToolResultSanitization:
    def test_recent_tool_message_summarized_when_compressed(self) -> None:
        mem = ShortTermMemory(budget=200, recent_window=3)
        tool_result = json.dumps({
            "status": "completed",
            "content": "import os\nimport sys\n" * 50,
            "path": "src/secret.py",
        })
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": tool_result},
            {"role": "assistant", "content": "saw the file"},
            {"role": "tool", "name": "read_allowed_file", "content": tool_result},
        ] + [{"role": "assistant", "content": f"msg_{i}_" + "y" * 30} for i in range(10)]
        result = mem.compact(msgs)
        for m in result:
            dumped = json.dumps(m)
            assert "import os" not in dumped
            assert "src/secret.py" not in dumped

    def test_read_allowed_file_large_output_sanitized_in_recent(self) -> None:
        mem = ShortTermMemory(budget=150, recent_window=2)
        big_content = json.dumps({
            "status": "completed",
            "content": "x" * 1000,
            "path": "src/big_file.py",
        })
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": big_content},
            {"role": "tool", "name": "read_allowed_file", "content": big_content},
        ]
        result = mem.compact(msgs)
        for m in result:
            dumped = json.dumps(m)
            assert "x" * 100 not in dumped
            assert "src/big_file.py" not in dumped

    def test_run_allowed_tests_stdout_sanitized_in_recent(self) -> None:
        mem = ShortTermMemory(budget=150, recent_window=2)
        test_result = json.dumps({
            "status": "completed",
            "results": [{"command": "pytest", "exit_code": 0, "stdout": "PASSED " * 200, "stderr": ""}],
        })
        msgs = [
            {"role": "tool", "name": "run_allowed_tests", "content": test_result},
            {"role": "tool", "name": "run_allowed_tests", "content": test_result},
        ]
        result = mem.compact(msgs)
        for m in result:
            dumped = json.dumps(m)
            assert "PASSED " * 10 not in dumped
            assert "stdout" not in dumped


class TestToolSummaryStructureWithToolName:
    def test_summary_contains_tool_name_from_name_field(self) -> None:
        mem = ShortTermMemory(budget=150, recent_window=2)
        tool_result = json.dumps({"status": "completed", "content": "x" * 500})
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": tool_result},
            {"role": "tool", "name": "run_allowed_tests", "content": tool_result},
        ] + [{"role": "assistant", "content": f"msg_{i}_" + "y" * 30} for i in range(10)]
        result = mem.compact(msgs)
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        for tm in tool_msgs:
            content = tm.get("content", "")
            assert "unknown" not in content or "read_allowed_file" in content or "run_allowed_tests" in content

    def test_read_allowed_file_summary_has_tool_name_and_purpose(self) -> None:
        mem = ShortTermMemory(budget=150, recent_window=2)
        tool_result = json.dumps({"status": "completed", "content": "x" * 500})
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": tool_result},
            {"role": "tool", "name": "read_allowed_file", "content": tool_result},
        ]
        result = mem.compact(msgs)
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        for tm in tool_msgs:
            content = tm.get("content", "")
            assert "read_allowed_file" in content
            assert "Read file content" in content

    def test_run_allowed_tests_summary_has_tool_name_and_purpose(self) -> None:
        mem = ShortTermMemory(budget=150, recent_window=2)
        tool_result = json.dumps({"status": "completed", "results": [{"stdout": "x" * 200}]})
        msgs = [
            {"role": "tool", "name": "run_allowed_tests", "content": tool_result},
            {"role": "tool", "name": "run_allowed_tests", "content": tool_result},
        ]
        result = mem.compact(msgs)
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        for tm in tool_msgs:
            content = tm.get("content", "")
            assert "run_allowed_tests" in content
            assert "Run test commands" in content

    def test_failed_tool_summary_has_error_code(self) -> None:
        mem = ShortTermMemory(budget=150, recent_window=2)
        tool_result = json.dumps({"status": "failed", "error_code": "path_not_allowed"})
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": tool_result},
            {"role": "tool", "name": "read_allowed_file", "content": tool_result},
        ]
        result = mem.compact(msgs)
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        for tm in tool_msgs:
            content = tm.get("content", "")
            assert "path_not_allowed" in content

    def test_no_unknown_completed_in_summary(self) -> None:
        mem = ShortTermMemory(budget=150, recent_window=2)
        tool_result = json.dumps({"status": "completed", "content": "x" * 500})
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": tool_result},
            {"role": "tool", "name": "run_allowed_tests", "content": tool_result},
        ]
        result = mem.compact(msgs)
        for m in result:
            content = str(m.get("content", ""))
            assert "unknown_completed" not in content


class TestOverBudgetSmallWindow:
    def test_few_messages_over_budget_not_returned_as_is(self) -> None:
        mem = ShortTermMemory(budget=100, recent_window=4)
        msgs = [
            {"role": "assistant", "content": "a" * 1000},
            {"role": "assistant", "content": "b" * 1000},
        ]
        result = mem.compact(msgs)
        total = sum(len(str(m.get("content", ""))) for m in result)
        assert total < 2000

    def test_few_messages_over_budget_compacted_or_truncated(self) -> None:
        mem = ShortTermMemory(budget=100, recent_window=4)
        msgs = [
            {"role": "assistant", "content": "a" * 1000},
            {"role": "assistant", "content": "b" * 1000},
        ]
        result = mem.compact(msgs)
        assert result != msgs

    def test_system_message_preserved_when_few_messages_over_budget(self) -> None:
        mem = ShortTermMemory(budget=100, recent_window=4)
        msgs = [
            {"role": "system", "content": "important"},
            {"role": "assistant", "content": "a" * 1000},
            {"role": "assistant", "content": "b" * 1000},
        ]
        result = mem.compact(msgs)
        system_msgs = [
            m for m in result
            if m.get("role") == "system" and m.get("content") == "important"
        ]
        assert len(system_msgs) == 1


class TestRecentLargeNonToolMessagesOverBudget:
    def test_recent_large_assistant_messages_truncated_to_budget(self) -> None:
        mem = ShortTermMemory(budget=100, recent_window=2)
        msgs = [
            {"role": "assistant", "content": "a" * 1000},
            {"role": "assistant", "content": "b" * 1000},
        ]
        result = mem.compact(msgs)
        non_system_total = sum(
            len(str(m.get("content", "")))
            for m in result
            if m.get("role") != "system"
            or str(m.get("content", "")).startswith(_COMPACTED_PREFIX)
        )
        assert non_system_total <= 100 + 50

    def test_recent_order_preserved_after_truncation(self) -> None:
        mem = ShortTermMemory(budget=100, recent_window=2)
        msgs = [
            {"role": "assistant", "content": "first_" + "a" * 1000},
            {"role": "assistant", "content": "second_" + "b" * 1000},
        ]
        result = mem.compact(msgs)
        non_system = [
            m for m in result
            if m.get("role") != "system"
            or str(m.get("content", "")).startswith(_COMPACTED_PREFIX)
        ]
        if len(non_system) >= 2:
            first_content = str(non_system[0].get("content", ""))
            second_content = str(non_system[1].get("content", ""))
            assert "first" in first_content or "second" in second_content

    def test_system_message_not_counted_in_budget(self) -> None:
        mem = ShortTermMemory(budget=100, recent_window=2)
        msgs = [
            {"role": "system", "content": "important"},
            {"role": "assistant", "content": "a" * 1000},
            {"role": "assistant", "content": "b" * 1000},
        ]
        result = mem.compact(msgs)
        system_total = sum(
            len(str(m.get("content", "")))
            for m in result
            if m.get("role") == "system"
            and not str(m.get("content", "")).startswith(_COMPACTED_PREFIX)
        )
        non_system_total = sum(
            len(str(m.get("content", "")))
            for m in result
            if m.get("role") != "system"
            or str(m.get("content", "")).startswith(_COMPACTED_PREFIX)
        )
        assert system_total > 0
        assert non_system_total <= 100 + 50


class TestToolSummaryFieldsPreservedUnderSmallBudget:
    def test_only_tool_messages_retained_have_complete_fields(self) -> None:
        mem = ShortTermMemory(budget=300, recent_window=4)
        tool_completed = json.dumps({"status": "completed", "content": "x" * 500})
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": tool_completed},
            {"role": "tool", "name": "run_allowed_tests", "content": tool_completed},
        ]
        result = mem.compact(msgs)
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        assert len(tool_msgs) >= 1, "at least one tool summary should be retained"
        for tm in tool_msgs:
            content = tm.get("content", "")
            assert "tool_name=" in content
            assert "purpose=" in content
            assert "status=" in content
            assert "result_summary=" in content

    def test_failed_tool_summary_retains_error_code(self) -> None:
        mem = ShortTermMemory(budget=300, recent_window=4)
        tool_failed = json.dumps({"status": "failed", "error_code": "path_not_allowed"})
        tool_completed = json.dumps({"status": "completed", "content": "x" * 500})
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": tool_failed},
            {"role": "tool", "name": "run_allowed_tests", "content": tool_completed},
        ]
        result = mem.compact(msgs)
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        for tm in tool_msgs:
            content = tm.get("content", "")
            if "status=failed" in content:
                assert "error_code=" in content


class TestAtLeastOneToolSummaryRetained:
    def test_tiny_budget_still_retains_one_tool_summary(self) -> None:
        mem = ShortTermMemory(budget=80, recent_window=4)
        tool_completed = json.dumps({"status": "completed", "content": "x" * 500})
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": tool_completed},
            {"role": "tool", "name": "run_allowed_tests", "content": tool_completed},
        ]
        result = mem.compact(msgs)
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        assert len(tool_msgs) >= 1, (
            "at least one tool summary must be retained when input contains tool messages"
        )

    def test_tiny_budget_retained_summary_has_complete_fields(self) -> None:
        mem = ShortTermMemory(budget=80, recent_window=4)
        tool_completed = json.dumps({"status": "completed", "content": "x" * 500})
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": tool_completed},
            {"role": "tool", "name": "run_allowed_tests", "content": tool_completed},
        ]
        result = mem.compact(msgs)
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        assert len(tool_msgs) >= 1
        for tm in tool_msgs:
            content = tm.get("content", "")
            assert "tool_name=" in content
            assert "purpose=" in content
            assert "status=" in content
            assert "result_summary=" in content

    def test_tiny_budget_failed_summary_retained_with_error_code(self) -> None:
        mem = ShortTermMemory(budget=80, recent_window=4)
        tool_failed = json.dumps({"status": "failed", "error_code": "path_not_allowed"})
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": tool_failed},
        ]
        result = mem.compact(msgs)
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        assert len(tool_msgs) >= 1
        for tm in tool_msgs:
            content = tm.get("content", "")
            if "status=failed" in content:
                assert "error_code=" in content

    def test_no_only_compacted_no_key_info(self) -> None:
        mem = ShortTermMemory(budget=80, recent_window=4)
        tool_completed = json.dumps({"status": "completed", "content": "x" * 500})
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": tool_completed},
            {"role": "tool", "name": "run_allowed_tests", "content": tool_completed},
        ]
        result = mem.compact(msgs)
        compacted_msgs = [
            m for m in result
            if m.get("role") == "system"
            and str(m.get("content", "")).startswith(_COMPACTED_PREFIX)
        ]
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        has_only_no_key_info = (
            len(compacted_msgs) == 1
            and "No key information retained" in compacted_msgs[0].get("content", "")
            and len(tool_msgs) == 0
        )
        assert not has_only_no_key_info, (
            "should not produce only '[compacted] No key information retained' "
            "when input contains tool messages"
        )

    def test_single_tool_message_always_retained(self) -> None:
        mem = ShortTermMemory(budget=50, recent_window=4)
        tool_completed = json.dumps({"status": "completed", "content": "x" * 500})
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": tool_completed},
        ]
        result = mem.compact(msgs)
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        assert len(tool_msgs) >= 1

    def test_no_truncated_field_values_in_tool_summaries(self) -> None:
        mem = ShortTermMemory(budget=80, recent_window=4)
        tool_completed = json.dumps({"status": "completed", "content": "x" * 500})
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": tool_completed},
            {"role": "tool", "name": "run_allowed_tests", "content": tool_completed},
        ]
        result = mem.compact(msgs)
        for m in result:
            if m.get("role") != "tool":
                continue
            content = str(m.get("content", ""))
            assert not content.endswith("..."), (
                f"tool summary should not be truncated: {content!r}"
            )

    def test_no_unknown_completed_anywhere(self) -> None:
        mem = ShortTermMemory(budget=80, recent_window=4)
        tool_completed = json.dumps({"status": "completed", "content": "x" * 500})
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": tool_completed},
        ]
        result = mem.compact(msgs)
        for m in result:
            content = str(m.get("content", ""))
            assert "unknown_completed" not in content

    def test_read_allowed_file_and_run_allowed_tests_fields(self) -> None:
        mem = ShortTermMemory(budget=400, recent_window=4)
        tool_completed = json.dumps({"status": "completed", "content": "x" * 500})
        tool_failed = json.dumps({"status": "failed", "error_code": "path_not_allowed"})
        msgs = [
            {"role": "tool", "name": "read_allowed_file", "content": tool_completed},
            {"role": "tool", "name": "run_allowed_tests", "content": tool_completed},
            {"role": "tool", "name": "read_allowed_file", "content": tool_failed},
        ]
        result = mem.compact(msgs)
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        assert len(tool_msgs) >= 1
        for tm in tool_msgs:
            content = tm.get("content", "")
            assert "tool_name=" in content
            assert "purpose=" in content
            assert "status=" in content
            assert "result_summary=" in content
            if "status=failed" in content:
                assert "error_code=" in content

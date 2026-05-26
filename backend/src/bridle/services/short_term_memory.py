"""Short-term memory sliding window with threshold-based compression."""
from __future__ import annotations

import json as _json
from typing import Any

from bridle.logging.jsonl import log_event

_COMPACTED_ROLE = "system"
_COMPACTED_PREFIX = "[compacted] "


class ToolResultSummarizer:
    _TOOL_PURPOSES = {
        "read_allowed_file": "Read file content",
        "propose_file_patch": "Propose file patch",
        "run_allowed_tests": "Run test commands",
        "report_blocked": "Report blocked status",
    }

    @staticmethod
    def summarize(tool_name: str, raw_content: str) -> dict[str, Any]:
        summary: dict[str, Any] = {"tool_name": tool_name}

        try:
            data = _json.loads(raw_content)
            if not isinstance(data, dict):
                data = {}
        except (ValueError, TypeError):
            data = {}

        status = data.get("status", "unknown")
        summary["status"] = status

        if status == "failed":
            error_code = data.get("error_code", "")
            if error_code:
                summary["error_code"] = error_code
            summary["result_summary"] = "failed"
        elif status == "completed":
            purpose = ToolResultSummarizer._TOOL_PURPOSES.get(tool_name, "Tool executed")
            summary["result_summary"] = purpose + " successfully"
        else:
            summary["result_summary"] = f"tool_{status}"

        log_event(
            "tool_result_summarized",
            "completed",
            detail={"tool_name": tool_name, "status": status},
        )

        return summary

    @staticmethod
    def format_summary(summary: dict[str, Any]) -> str:
        tool_name = summary.get("tool_name", "unknown")
        purpose = ToolResultSummarizer._TOOL_PURPOSES.get(tool_name, "Tool executed")
        status = summary.get("status", "unknown")
        result_summary = summary.get("result_summary", "")
        parts = [f"tool_name={tool_name}", f"purpose={purpose}", f"status={status}"]
        if result_summary:
            parts.append(f"result_summary={result_summary}")
        error_code = summary.get("error_code")
        if error_code:
            parts.append(f"error_code={error_code}")
        return "; ".join(parts)


class ShortTermMemory:
    def __init__(
        self,
        *,
        budget: int = 4000,
        recent_window: int = 4,
        run_id: str | None = None,
        node_id: str | None = None,
    ) -> None:
        self.budget = budget
        self.recent_window = recent_window
        self._run_id = run_id
        self._node_id = node_id

    def compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not messages:
            return []

        total_size = self._estimate_size(messages)
        if total_size <= self.budget:
            return list(messages)

        log_event(
            "short_term_memory_compacted",
            "started",
            run_id=self._run_id,
            node_id=self._node_id,
            detail={"original_size": total_size, "budget": self.budget, "message_count": len(messages)},
        )

        system_msgs = [
            m for m in messages
            if m.get("role") == "system"
            and not str(m.get("content", "")).startswith(_COMPACTED_PREFIX)
        ]
        non_system = [m for m in messages if m not in system_msgs]

        sanitized = [self._sanitize_tool_message(m) for m in non_system]

        if len(sanitized) <= self.recent_window:
            summary, tool_summaries = self._build_summary(sanitized, max_length=max(self.budget // 2, 100))
            result = system_msgs + [summary] + tool_summaries
            if self._estimate_size(result) > self.budget:
                result = self._trim_to_budget(result)
            self._log_compaction(total_size, self._estimate_size(result), len(messages), len(result))
            return result

        old = sanitized[: -self.recent_window]
        recent = sanitized[-self.recent_window :]

        summary, tool_summaries = self._build_summary(old, max_length=max(self.budget // 2, 100))
        result = system_msgs + [summary] + tool_summaries + recent

        if self._estimate_size(result) > self.budget:
            result = self._trim_to_budget(result)

        self._log_compaction(total_size, self._estimate_size(result), len(messages), len(result))
        return result

    def _sanitize_tool_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("role") != "tool":
            return msg
        tool_name = msg.get("name", "") or "unknown"
        content = str(msg.get("content", ""))
        summary = ToolResultSummarizer.summarize(tool_name, content)
        formatted = ToolResultSummarizer.format_summary(summary)
        return {"role": "tool", "name": tool_name, "content": formatted}

    def _build_summary(
        self,
        old_messages: list[dict[str, Any]],
        *,
        max_length: int = 500,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        goals = []
        decisions = []
        tool_summaries: list[dict[str, Any]] = []

        for m in old_messages:
            content = str(m.get("content", ""))
            role = m.get("role", "")
            if role == "user":
                goals.append(content[:120])
            elif role == "assistant":
                decisions.append(content[:120])
            elif role == "tool":
                tool_summaries.append(m)
            else:
                decisions.append(content[:80])

        parts = []
        if goals:
            parts.append("Goals: " + "; ".join(goals[-3:]))
        if decisions:
            parts.append("Decisions: " + "; ".join(decisions[-3:]))

        content = _COMPACTED_PREFIX + (" | ".join(parts) if parts else "No key information retained")
        if len(content) > max_length:
            content = content[: max_length - 3] + "..."
        return {"role": _COMPACTED_ROLE, "content": content}, tool_summaries

    def _estimate_size(self, messages: list[dict[str, Any]]) -> int:
        return sum(len(str(m.get("content", ""))) for m in messages)

    def _trim_to_budget(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        system = []
        summary = []
        tool_summaries: list[dict[str, Any]] = []
        other = []
        for m in messages:
            if m.get("role") == "system" and str(m.get("content", "")).startswith(_COMPACTED_PREFIX):
                summary.append(m)
            elif m.get("role") == "system":
                system.append(m)
            elif m.get("role") == "tool":
                tool_summaries.append(m)
            else:
                other.append(m)

        system_size = self._estimate_size(system)
        available = max(self.budget - system_size, 20)

        if summary:
            summary_budget = available // 3
            shortened = dict(summary[0])
            content = str(shortened.get("content", ""))
            if len(content) > summary_budget:
                shortened["content"] = content[: summary_budget - 3] + "..."
            summary = [shortened]

        summary_size = self._estimate_size(summary)
        remaining = max(available - summary_size, 20)

        recent_min = 20 if other else 0
        tool_budget = remaining - recent_min
        dropped_count = 0
        while len(tool_summaries) > 1 and self._estimate_size(tool_summaries) > tool_budget:
            tool_summaries.pop(0)
            dropped_count += 1
        if dropped_count:
            log_event(
                "tool_summary_dropped",
                "completed",
                run_id=self._run_id,
                node_id=self._node_id,
                detail={"dropped_count": dropped_count, "retained_count": len(tool_summaries)},
            )

        tool_size = self._estimate_size(tool_summaries)
        recent_budget = max(remaining - tool_size, 20)
        recent = self._truncate_messages(other[-self.recent_window :], recent_budget)

        return system + summary + tool_summaries + recent

    def _truncate_messages(self, messages: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
        if not messages:
            return []
        total = self._estimate_size(messages)
        if total <= budget:
            return list(messages)
        per_msg = max(budget // len(messages), 10)
        result = []
        for m in messages:
            content = str(m.get("content", ""))
            if len(content) > per_msg:
                truncated = dict(m)
                truncated["content"] = content[: per_msg - 3] + "..."
                result.append(truncated)
            else:
                result.append(m)
        if self._estimate_size(result) > budget:
            while len(result) > 1 and self._estimate_size(result) > budget:
                result.pop(0)
        return result

    def _log_compaction(self, original_size: int, new_size: int, original_count: int, new_count: int) -> None:
        log_event(
            "short_term_memory_compacted",
            "completed",
            run_id=self._run_id,
            node_id=self._node_id,
            detail={
                "original_size": original_size,
                "new_size": new_size,
                "original_count": original_count,
                "new_count": new_count,
            },
        )

"""Short-term memory sliding window with threshold-based compression."""
from __future__ import annotations

import asyncio
import json as _json
from collections.abc import Awaitable, Callable
from typing import Any

from bridle.logging.jsonl import log_event

_COMPACTED_ROLE = "system"
_COMPACTED_PREFIX = "[compacted] "


class ToolResultReceiptBuilder:
    """Build a deterministic, allowlisted receipt for a consumed tool result."""

    _ERROR_SUMMARY_MAX_CHARS = 240
    _SUCCESS_FIELDS = (
        "status",
        "success",
        "id",
        "path",
        "sha256",
        "cursor",
        "exit_code",
    )
    _ERROR_FIELDS = (
        "status",
        "success",
        "error_code",
        "error_type",
        "exit_code",
        "category",
        "retryable",
    )

    @classmethod
    def build(cls, tool_name: str, raw_content: str) -> str:
        try:
            parsed = _json.loads(raw_content)
            data = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            data = {}

        status = data.get("status", "unknown")
        fields = cls._ERROR_FIELDS if status == "failed" else cls._SUCCESS_FIELDS
        receipt = {
            key: data[key]
            for key in fields
            if key in data and data[key] is not None
        }
        error_summary_truncated = False
        if status == "failed" and data.get("message"):
            message = str(data["message"])
            error_summary_truncated = len(message) > cls._ERROR_SUMMARY_MAX_CHARS
            receipt["error_summary"] = message[: cls._ERROR_SUMMARY_MAX_CHARS]
        receipt["tool_name"] = tool_name or "unknown"

        log_event(
            "tool_result_receipt_built",
            "completed",
            detail={
                "tool_name": receipt["tool_name"],
                "status": status,
                "field_count": len(receipt),
                "error_summary_truncated": error_summary_truncated,
            },
        )
        return _json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ShortTermMemory:
    def __init__(
        self,
        *,
        budget: int = 4000,
        recent_window: int = 4,
        optimizer: Callable[[str, list[dict[str, Any]]], Awaitable[str]] | None = None,
        optimizer_timeout_seconds: float = 5.0,
        run_id: str | None = None,
        node_id: str | None = None,
    ) -> None:
        self.budget = budget
        self.recent_window = recent_window
        self._optimizer = optimizer
        self._optimizer_timeout_seconds = optimizer_timeout_seconds
        self._summary = ""
        self._messages: list[dict[str, Any]] = []
        self._anchor_message_id: str | None = None
        self._run_id = run_id
        self._node_id = node_id

    @property
    def summary(self) -> str:
        return self._summary

    @property
    def anchor_message_id(self) -> str | None:
        return self._anchor_message_id

    def restore(
        self,
        *,
        summary: str,
        messages: list[dict[str, Any]],
        anchor_message_id: str | None,
    ) -> None:
        """Restore a persisted summary plus only the messages after its anchor."""
        self._summary = self._bound_summary(summary)
        self._messages = [dict(message) for message in messages]
        self._anchor_message_id = anchor_message_id

    async def append(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Append only new messages and compact the evicted delta above the watermark."""
        candidate_messages = [
            *self._messages,
            *(dict(message) for message in messages),
        ]
        if self._estimate_size(candidate_messages) <= self.budget:
            self._messages = candidate_messages
            return self._render_window()

        evicted_count = max(len(candidate_messages) - self.recent_window, 0)
        if evicted_count == 0:
            self._messages = candidate_messages
            return self._render_window()

        evicted = candidate_messages[:evicted_count]
        retained = candidate_messages[evicted_count:]
        optimized = await self._optimize_evicted(evicted)
        bounded_summary = self._bound_summary(optimized)
        anchor = evicted[-1].get("id")

        self._messages = retained
        self._summary = bounded_summary
        if anchor:
            self._anchor_message_id = str(anchor)

        log_event(
            "short_term_memory_window_optimized",
            "completed",
            run_id=self._run_id,
            node_id=self._node_id,
            detail={
                "evicted_count": len(evicted),
                "retained_count": len(self._messages),
                "used_optimizer": self._optimizer is not None,
            },
        )
        return self._render_window()

    async def _optimize_evicted(self, evicted: list[dict[str, Any]]) -> str:
        if self._optimizer is not None:
            try:
                optimized = await asyncio.wait_for(
                    self._optimizer(self._summary, evicted),
                    timeout=self._optimizer_timeout_seconds,
                )
                if optimized.strip():
                    return optimized.strip()
                failure = "empty_result"
            except TimeoutError:
                failure = "timeout"
            except Exception as exc:  # noqa: BLE001 - optimizer failures must fall back
                failure = type(exc).__name__

            log_event(
                "short_term_memory_optimizer_fallback",
                "completed",
                run_id=self._run_id,
                node_id=self._node_id,
                detail={"reason": failure, "evicted_count": len(evicted)},
            )

        parts: list[str] = []
        if self._summary:
            parts.append(self._summary)
        for message in evicted:
            content = str(message.get("content", "")).strip()
            if content:
                parts.append(f"{message.get('role', 'unknown')}: {content[:240]}")
        return " | ".join(part for part in parts if part)

    def _bound_summary(self, summary: str) -> str:
        text = summary.strip()
        limit = max(0, int(self.budget))
        if len(text) <= limit:
            return text
        bounded = text[-limit:] if limit else ""
        log_event(
            "short_term_memory_summary_bounded",
            "completed",
            run_id=self._run_id,
            node_id=self._node_id,
            detail={
                "original_size": len(text),
                "retained_size": len(bounded),
                "budget": limit,
            },
        )
        return bounded

    def _render_window(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if self._summary:
            result.append({"role": _COMPACTED_ROLE, "content": _COMPACTED_PREFIX + self._summary})
        result.extend(dict(message) for message in self._messages)
        return result

    def _estimate_size(self, messages: list[dict[str, Any]]) -> int:
        return sum(len(str(m.get("content", ""))) for m in messages)

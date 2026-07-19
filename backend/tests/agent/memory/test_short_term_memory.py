"""Contracts for the incremental per-session short-term memory window."""
from __future__ import annotations

import asyncio

import pytest

from bridle.agent.memory.short_term_memory import _COMPACTED_PREFIX, ShortTermMemory
from bridle.agent.providers.agent_provider import FakeAgentProvider


class TestIncrementalConversationOptimizer:
    @pytest.mark.asyncio
    async def test_memory_optimizer_threshold_fallbacks_and_bounded_summary(self) -> None:
        threshold_calls: list[tuple[str, list[dict]]] = []

        async def threshold_optimizer(summary: str, evicted: list[dict]) -> str:
            threshold_calls.append((summary, evicted))
            return "optimized"

        below = ShortTermMemory(budget=5, recent_window=1, optimizer=threshold_optimizer)
        below_window = await below.append([{"role": "user", "content": "1234"}])
        assert below_window[-1]["content"] == "1234"
        assert threshold_calls == []

        equal = ShortTermMemory(budget=5, recent_window=1, optimizer=threshold_optimizer)
        equal_window = await equal.append(
            [
                {"id": "equal-old", "role": "user", "content": "1234"},
                {"id": "equal-current", "role": "assistant", "content": "5"},
            ]
        )
        assert [item["content"] for item in equal_window] == ["1234", "5"]
        assert threshold_calls == []

        above = ShortTermMemory(budget=5, recent_window=1, optimizer=threshold_optimizer)
        above_window = await above.append(
            [
                {"id": "above-old", "role": "user", "content": "12345"},
                {"id": "above-current", "role": "assistant", "content": "6"},
            ]
        )
        assert threshold_calls == [
            ("", [{"id": "above-old", "role": "user", "content": "12345"}])
        ]
        assert above_window[-1] == {
            "id": "above-current",
            "role": "assistant",
            "content": "6",
        }

        prior_summaries: list[str] = []

        async def growing_optimizer(summary: str, evicted: list[dict]) -> str:
            prior_summaries.append(summary)
            return f"{summary}|optimized|" + "x" * 80

        bounded = ShortTermMemory(
            budget=12,
            recent_window=1,
            optimizer=growing_optimizer,
        )
        last_content = ""
        bounded_window: list[dict] = []
        for index in range(6):
            last_content = f"recent-{index}-" + "r" * 20
            bounded_window = await bounded.append(
                [{"id": f"m-{index}", "role": "user", "content": last_content}]
            )

        async def raises(_: str, __: list[dict]) -> str:
            raise RuntimeError("optimizer unavailable")

        async def returns_empty(_: str, __: list[dict]) -> str:
            return ""

        async def times_out(_: str, __: list[dict]) -> str:
            await asyncio.sleep(0.02)
            return "late"

        fallback_summaries: list[str] = []
        for failed_optimizer, timeout in (
            (raises, 0.05),
            (returns_empty, 0.05),
            (times_out, 0.001),
            (FakeAgentProvider().optimize_memory, 0.05),
        ):
            fallback = ShortTermMemory(
                budget=12,
                recent_window=1,
                optimizer=failed_optimizer,
                optimizer_timeout_seconds=timeout,
            )
            fallback_window = await fallback.append(
                [
                    {"id": "old", "role": "user", "content": "old-" + "z" * 40},
                    {"id": "recent", "role": "assistant", "content": "recent verbatim"},
                ]
            )
            fallback_summaries.append(fallback.summary)
            assert fallback_window[-1]["content"] == "recent verbatim"

        assert prior_summaries
        assert all(len(summary) <= bounded.budget for summary in prior_summaries)
        assert len(bounded.summary) <= bounded.budget
        assert bounded_window[0]["content"] == _COMPACTED_PREFIX + bounded.summary
        assert bounded_window[-1]["content"] == last_content
        assert all(len(summary) <= 12 for summary in fallback_summaries)

    @pytest.mark.asyncio
    async def test_optimizer_runs_incrementally_above_watermark_and_falls_back(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        calls: list[tuple[str, list[dict]]] = []

        async def optimizer(summary: str, evicted: list[dict]) -> str:
            calls.append((summary, evicted))
            return "optimized history"

        memory = ShortTermMemory(
            budget=20,
            recent_window=2,
            optimizer=optimizer,
            optimizer_timeout_seconds=0.05,
        )
        first = await memory.append([{"role": "user", "content": "first"}])
        assert first == [{"role": "user", "content": "first"}]
        assert calls == []

        result = await memory.append(
            [
                {"role": "assistant", "content": "x" * 30},
                {"role": "user", "content": "current"},
            ]
        )
        assert calls == [("", [{"role": "user", "content": "first"}])]
        assert result[0] == {
            "role": "system",
            "content": _COMPACTED_PREFIX + "optimized history",
        }
        assert result[-1] == {"role": "user", "content": "current"}

        calls.clear()
        await memory.append(
            [
                {"role": "assistant", "content": "y" * 30},
                {"role": "user", "content": "next-current"},
            ]
        )
        assert calls == [
            (
                "optimized history",
                [
                    {"role": "assistant", "content": "x" * 30},
                    {"role": "user", "content": "current"},
                ],
            )
        ]

        async def raises(_: str, __: list[dict]) -> str:
            raise RuntimeError("optimizer unavailable")

        async def returns_empty(_: str, __: list[dict]) -> str:
            return ""

        async def times_out(_: str, __: list[dict]) -> str:
            await asyncio.sleep(0.02)
            return "late"

        for failed_optimizer, timeout in (
            (raises, 0.05),
            (returns_empty, 0.05),
            (times_out, 0.001),
        ):
            fallback = ShortTermMemory(
                budget=10,
                recent_window=1,
                optimizer=failed_optimizer,
                optimizer_timeout_seconds=timeout,
            )
            fallback_result = await fallback.append(
                [
                    {"role": "user", "content": "old message"},
                    {"role": "assistant", "content": "recent message"},
                ]
            )
            assert fallback_result[0]["content"].startswith(_COMPACTED_PREFIX)
            bounded_summary = fallback_result[0]["content"][len(_COMPACTED_PREFIX):]
            assert bounded_summary == "ld message"
            assert len(bounded_summary) <= fallback.budget
            assert fallback_result[-1]["content"] == "recent message"

        fallback_events = [
            record
            for record in caplog.records
            if getattr(record, "action", None) == "short_term_memory_optimizer_fallback"
        ]
        assert len(fallback_events) == 3

    @pytest.mark.asyncio
    async def test_under_watermark_does_not_optimize(self) -> None:
        calls = 0

        async def optimizer(_: str, __: list[dict]) -> str:
            nonlocal calls
            calls += 1
            return "unexpected"

        memory = ShortTermMemory(budget=100, recent_window=2, optimizer=optimizer)
        result = await memory.append([{"id": "m1", "role": "user", "content": "hello"}])

        assert result == [{"id": "m1", "role": "user", "content": "hello"}]
        assert calls == 0

    @pytest.mark.asyncio
    async def test_restore_uses_summary_and_only_post_anchor_messages(self) -> None:
        memory = ShortTermMemory(budget=100, recent_window=2)
        memory.restore(
            summary="persisted summary",
            messages=[{"id": "m2", "role": "assistant", "content": "retained"}],
            anchor_message_id="m1",
        )

        result = await memory.append([{"id": "m3", "role": "user", "content": "current"}])

        assert memory.anchor_message_id == "m1"
        assert result == [
            {"role": "system", "content": _COMPACTED_PREFIX + "persisted summary"},
            {"id": "m2", "role": "assistant", "content": "retained"},
            {"id": "m3", "role": "user", "content": "current"},
        ]

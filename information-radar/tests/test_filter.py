"""规则过滤契约测试。

业务契约（来自方案）：
- 排除抖音/TikTok 来源（source_name 或 URL 含 douyin/tiktok）
- 排除标题含 "toy programming" 等
- 排除摘要含 "purely conceptual" / "no implementation" / "role-play only" / "subjective rating only"
- 不命中规则的保留
"""
from __future__ import annotations

from paper_bridge.models import Item
from paper_bridge.pipeline.filter import (
    ExcludeRule,
    filter_items,
    should_exclude,
)


def make(**kw) -> Item:
    base = dict(source_type="arxiv", source_name="test", title="t", url="http://x")
    base.update(kw)
    return Item(**base)


RULE = ExcludeRule(
    source_blacklist=["douyin", "tiktok"],
    title_contains=["toy programming", "benchmark leaderboard only"],
    abstract_contains_any=["purely conceptual", "no implementation", "role-play only", "subjective rating only"],
)


class TestShouldExclude:
    def test_douyin_in_source_name_excluded(self):
        it = make(source_name="douyin_hot", url="http://x")
        excluded, reason = should_exclude(it, RULE)
        assert excluded is True
        assert reason == "source_blacklist:douyin"

    def test_tiktok_in_url_excluded(self):
        it = make(source_name="ok", url="https://tiktok.com/video/123")
        excluded, reason = should_exclude(it, RULE)
        assert excluded is True
        assert "tiktok" in reason

    def test_title_toy_programming_excluded(self):
        it = make(title="A toy programming challenge")
        excluded, reason = should_exclude(it, RULE)
        assert excluded is True
        assert "title_contains" in reason

    def test_abstract_purely_conceptual_excluded(self):
        it = make(title="Some Paper", abstract="This is purely conceptual work.")
        excluded, reason = should_exclude(it, RULE)
        assert excluded is True
        assert "abstract_contains_any" in reason

    def test_abstract_no_implementation_excluded(self):
        it = make(title="Framework", abstract="There is no implementation yet.")
        excluded, reason = should_exclude(it, RULE)
        assert excluded is True

    def test_abstract_role_play_only_excluded(self):
        it = make(title="Multi-Agent", abstract="This is role-play only simulation.")
        excluded, _ = should_exclude(it, RULE)
        assert excluded is True

    def test_normal_paper_not_excluded(self):
        it = make(
            title="Repository-Level Coding Agents: An Empirical Study",
            abstract="We study coding agents on real GitHub repositories with benchmarks.",
        )
        excluded, reason = should_exclude(it, RULE)
        assert excluded is False
        assert reason is None

    def test_no_abstract_not_excluded_by_abstract_rule(self):
        it = make(title="Paper Without Abstract", abstract=None)
        excluded, _ = should_exclude(it, RULE)
        assert excluded is False


class TestFilterBatch:
    def test_keeps_normal_drops_blacklisted(self):
        items = [
            make(title="Good Paper", url="http://arxiv.org/abs/123"),
            make(title="Douyin Trend", source_name="douyin", url="http://d"),
            make(title="Another Good One", url="http://arxiv.org/abs/456"),
        ]
        kept, dropped = filter_items(items, RULE)
        assert len(kept) == 2
        assert len(dropped) == 1
        assert "douyin" in dropped[0][1]

    def test_empty_batch(self):
        kept, dropped = filter_items([], RULE)
        assert kept == [] and dropped == []

    def test_drops_recorded_with_reason(self):
        items = [
            make(title="toy programming exercise"),
            make(title="Pure Concept", abstract="purely conceptual framework"),
        ]
        _, dropped = filter_items(items, RULE)
        assert len(dropped) == 2
        assert all(isinstance(r[1], str) and r[1] for r in dropped)

    def test_preserves_order_of_kept(self):
        items = [
            make(title="First", url="http://1"),
            make(title="douyin", source_name="douyin", url="http://2"),
            make(title="Third", url="http://3"),
        ]
        kept, _ = filter_items(items, RULE)
        assert [k.title for k in kept] == ["First", "Third"]

"""评分契约测试。

业务契约（来自方案）：
- 总分 100：领域相关性 35 / 实践价值 25 / 证据质量 20 / 可复现性 10 / 时效性 10
- ≥70：selected（进入每日精选）
- 50-69：archived（归档，暂不推送）
- <50：audit_only（仅保留审计记录）
- 加分项：真实仓库/PR/可执行基准/公开代码/数据集/成本分析 → 更高分
- 真实仓库/PR/可执行基准论文优先级更高
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from paper_bridge.models import Item
from paper_bridge.pipeline.scoring import (
    ScoringConfig,
    score_batch,
    score_item,
)


def make(**kw) -> Item:
    base = dict(source_type="arxiv", source_name="test", title="t", url="http://x")
    base.update(kw)
    return Item(**base)


CFG = ScoringConfig()


class TestScoringWeightsContract:
    """五维权重严格按方案 35/25/20/10/10。"""

    def test_weights_sum_to_100(self):
        assert sum(CFG.weights.values()) == 100

    def test_exact_weights(self):
        assert CFG.weights["domain_relevance"] == 35
        assert CFG.weights["practical_value"] == 25
        assert CFG.weights["evidence_quality"] == 20
        assert CFG.weights["reproducibility"] == 10
        assert CFG.weights["timeliness"] == 10


class TestTierContract:
    """分档边界：≥70 selected, 50-69 archived, <50 audit_only。"""

    def test_tier_boundaries(self):
        assert CFG.tiers["selected"] == 70
        assert CFG.tiers["archived"] == 50

    def test_score_70_is_selected(self):
        # 构造一个恰好 70 分的条目很难，这里验证分档逻辑
        from paper_bridge.pipeline.scoring import _determine_tier

        assert _determine_tier(70, CFG.tiers) == "selected"
        assert _determine_tier(69, CFG.tiers) == "archived"
        assert _determine_tier(50, CFG.tiers) == "archived"
        assert _determine_tier(49, CFG.tiers) == "audit_only"
        assert _determine_tier(0, CFG.tiers) == "audit_only"


class TestTimelinessContract:
    """时效性按距今天数打分。"""

    def test_within_30_days_full_score(self):
        recent = make(published_at=datetime.now(UTC) - timedelta(days=10))
        s = score_item(recent, CFG)
        assert s.timeliness == 10

    def test_within_90_days(self):
        item = make(published_at=datetime.now(UTC) - timedelta(days=60))
        assert score_item(item, CFG).timeliness == 7

    def test_within_180_days(self):
        item = make(published_at=datetime.now(UTC) - timedelta(days=120))
        assert score_item(item, CFG).timeliness == 4

    def test_within_365_days(self):
        item = make(published_at=datetime.now(UTC) - timedelta(days=300))
        assert score_item(item, CFG).timeliness == 2

    def test_older_zero(self):
        item = make(published_at=datetime.now(UTC) - timedelta(days=500))
        assert score_item(item, CFG).timeliness == 0

    def test_no_date_zero(self):
        item = make(published_at=None)
        assert score_item(item, CFG).timeliness == 0


class TestDomainRelevanceContract:
    """领域相关性：SE 核心关键词 + Agent 关键词。"""

    def test_se_keywords_boost_score(self):
        item = make(
            title="Bug Fixing and Test Generation in Software Engineering",
            abstract="We study code review and fault localization.",
        )
        s = score_item(item, CFG)
        assert s.domain_relevance > 10

    def test_agent_keywords_boost_score(self):
        item = make(
            title="Coding Agent with Context Management",
            abstract="An LLM agent with agent planning and agent memory.",
        )
        s = score_item(item, CFG)
        assert s.domain_relevance > 5

    def test_irrelevant_paper_low_score(self):
        item = make(title="Chocolate Cake Recipe", abstract="A dessert guide.")
        s = score_item(item, CFG)
        assert s.domain_relevance == 0

    def test_non_paper_source_default_score(self):
        blog = make(source_type="blog", category="engineering")
        assert score_item(blog, CFG).domain_relevance == 25

    def test_non_paper_tech_source_lower(self):
        bili = make(source_type="bilibili", category="tech")
        assert score_item(bili, CFG).domain_relevance == 15


class TestEvidenceQualityContract:
    """证据质量：基础分 + 加分项（真实仓库/PR/基准/代码/数据集/成本/失败）。"""

    def test_full_evidence_gets_high_score(self):
        item = make(
            title="Real Repository Study with SWE-Bench",
            abstract="We use a real repository from GitHub. Public code and dataset released. "
                     "Cost analysis and failure mode analysis included.",
            authors=["A"],
            venue="ICSE",
            published_at=datetime.now(UTC),
            full_text_url="http://x.pdf",
            has_full_text=True,
            doi="10.1/x",
        )
        s = score_item(item, CFG)
        assert s.evidence_quality == 20  # 满分（加分受上限约束）
        # 应命中多个加分项
        assert len(s.bonus_applied) >= 4

    def test_minimal_evidence_low_score(self):
        item = make(title="Paper", abstract=None, authors=[], venue=None)
        s = score_item(item, CFG)
        assert s.evidence_quality == 0

    def test_bonus_capped_at_max(self):
        """加分项总和超过上限时，该维度得分不超过 20。"""
        item = make(
            title="Real repository real PR SWE-Bench benchmark",
            abstract="GitHub open source. Dataset released. Cost analysis. Failure analysis. "
                     "Dockerfile. Reproduction script.",
            authors=["A"],
            venue="ICSE",
            published_at=datetime.now(UTC),
            full_text_url="http://x.pdf",
            has_full_text=True,
            doi="10.1/x",
        )
        s = score_item(item, CFG)
        assert s.evidence_quality <= 20

    def test_real_repository_bonus_applied(self):
        item = make(
            title="Study on Real Repository",
            abstract="We analyze a real repository.",
            authors=["A"],
        )
        s = score_item(item, CFG)
        assert any("real_repository" in b for b in s.bonus_applied)


class TestReproducibilityContract:
    """可复现性：DOI/arXiv ID + full_text + 加分（dockerfile/script/data）。"""

    def test_doi_gives_base_score(self):
        item = make(title="t", doi="10.1/x")
        s = score_item(item, CFG)
        assert s.reproducibility >= 3

    def test_arxiv_id_gives_base_score(self):
        item = make(title="t", arxiv_id="2401.00001")
        s = score_item(item, CFG)
        assert s.reproducibility >= 3

    def test_dockerfile_bonus(self):
        item = make(title="t", abstract="Includes Dockerfile for reproduction.", doi="10.1/x")
        s = score_item(item, CFG)
        assert any("dockerfile" in b for b in s.bonus_applied)

    def test_capped_at_max(self):
        item = make(
            title="t",
            abstract="Dockerfile reproduction script data release.",
            doi="10.1/x",
            full_text_url="http://x.pdf",
        )
        s = score_item(item, CFG)
        assert s.reproducibility <= 10


class TestTotalScoreContract:
    """总分 = 五维之和，不超过 100。"""

    def test_high_value_paper_reaches_selected(self):
        item = make(
            title="Repository-Level Coding Agent for Bug Fixing and Test Generation",
            abstract="We present a coding agent operating on real GitHub repositories. "
                     "We use SWE-Bench as executable benchmark. Public code and dataset released. "
                     "Cost analysis and failure mode analysis included. Dockerfile provided.",
            authors=["Alice", "Bob"],
            venue="ICSE 2026",
            published_at=datetime.now(UTC) - timedelta(days=5),
            full_text_url="http://example.com/paper.pdf",
            has_full_text=True,
            doi="10.1145/icse.001",
        )
        s = score_item(item, CFG)
        assert s.total >= 70
        assert s.tier == "selected"

    def test_conceptual_paper_low_score(self):
        item = make(
            title="A Theoretical Framework",
            abstract="A purely theoretical discussion.",
        )
        s = score_item(item, CFG)
        assert s.total < 50
        assert s.tier == "audit_only"

    def test_total_never_exceeds_100(self):
        item = make(
            title="Repository Bug Fixing Test Generation Code Review Agent",
            abstract="Real repository real PR SWE-Bench GitHub open source dataset "
                     "cost analysis failure analysis Dockerfile reproduction script",
            authors=["A"],
            venue="ICSE",
            published_at=datetime.now(UTC),
            full_text_url="http://x.pdf",
            has_full_text=True,
            doi="10.1/x",
        )
        s = score_item(item, CFG)
        assert s.total <= 100


class TestBatchScoring:
    """批量评分：按总分降序排列。"""

    def test_sorted_descending(self):
        high = make(
            title="Repository Coding Agent Bug Fixing",
            abstract="Real repository GitHub benchmark SWE-Bench public code",
            authors=["A"], venue="ICSE", published_at=datetime.now(UTC),
            full_text_url="http://h.pdf", has_full_text=True, doi="10.1/h",
        )
        low = make(title="Conceptual Note", abstract=None)
        results = score_batch([low, high], CFG)
        assert results[0][1].total >= results[1][1].total
        assert results[0][0].title.startswith("Repository")

    def test_tier_counts_logged(self):
        items = [
            make(title="High Value Bug Fixing Agent", abstract="Real repository benchmark",
                 authors=["A"], venue="ICSE", published_at=datetime.now(UTC),
                 full_text_url="http://x.pdf", doi="10.1/x"),
            make(title="Low Conceptual", abstract=None),
        ]
        results = score_batch(items, CFG)
        tiers = [s.tier for _, s in results]
        assert "selected" in tiers or "archived" in tiers
        assert "audit_only" in tiers

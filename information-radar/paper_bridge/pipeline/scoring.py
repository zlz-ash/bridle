"""评分：五维评分（总分 100）+ 加分项 + 分档。

业务契约（来自方案）：
- 总分 100：领域相关性 35 / 实践价值 25 / 证据质量 20 / 可复现性 10 / 时效性 10
- ≥70：进入每日精选
- 50-69：归档，暂不推送
- <50：仅保留审计记录
- 加分项：真实仓库、真实 PR、可执行基准、公开代码/数据集、成本与失败分析
- 真实仓库/PR/可执行基准/公开代码/数据集/成本分析获得更高优先级
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml
from loguru import logger

from paper_bridge.models import Item


@dataclass
class ScoringConfig:
    """评分配置（从 scoring.yaml 加载）。"""
    weights: dict[str, int] = field(default_factory=lambda: {
        "domain_relevance": 35,
        "practical_value": 25,
        "evidence_quality": 20,
        "reproducibility": 10,
        "timeliness": 10,
    })
    tiers: dict[str, int] = field(default_factory=lambda: {
        "selected": 70,
        "archived": 50,
        "audit_only": 0,
    })
    bonus: dict[str, dict[str, int]] = field(default_factory=lambda: {
        "evidence_quality": {
            "real_repository": 6,
            "real_pr": 5,
            "executable_benchmark": 4,
            "public_code": 4,
            "public_dataset": 3,
            "cost_analysis": 3,
            "failure_analysis": 3,
        },
        "reproducibility": {
            "has_dockerfile": 3,
            "has_reproduction_script": 3,
            "has_data_release": 2,
        },
    })
    timeliness: dict[str, int] = field(default_factory=lambda: {
        "within_30_days": 10,
        "within_90_days": 7,
        "within_180_days": 4,
        "within_365_days": 2,
        "older": 0,
    })


def load_scoring_config(path: str | Path = "config/scoring.yaml") -> ScoringConfig:
    """从 scoring.yaml 加载评分配置。"""
    p = Path(path)
    if not p.exists():
        return ScoringConfig()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = ScoringConfig()
    if "weights" in data:
        cfg.weights = data["weights"]
    if "tiers" in data:
        cfg.tiers = data["tiers"]
    if "bonus" in data:
        cfg.bonus = data["bonus"]
    if "timeliness" in data:
        cfg.timeliness = data["timeliness"]
    return cfg


# ---- 关键词集合（用于领域相关性与实践价值打分）----

SE_CORE_KEYWORDS = [
    "software engineering", "code generation", "bug fixing", "issue resolution",
    "test generation", "code review", "fault localization", "software maintenance",
    "dependency upgrade", "ci/cd", "repository-level", "pull request",
]

AGENT_KEYWORDS = [
    "coding agent", "agent tool", "context management", "agent memory",
    "agent planning", "human-ai collaboration", "autonomous agent",
    "llm agent", "developer study", "agent pr",
]

PRACTICE_KEYWORDS = [
    "real repository", "real-world", "production", "benchmark", "empirical study",
    "case study", "industrial", "github", "execution", "reproduce",
]

EXCLUDE_CONCEPTUAL = [
    "purely conceptual", "toy programming", "subjective rating only",
    "role-play only", "no implementation",
]


@dataclass
class ScoreBreakdown:
    """单条目的评分明细。"""
    domain_relevance: int
    practical_value: int
    evidence_quality: int
    reproducibility: int
    timeliness: int
    total: int
    tier: str  # selected / archived / audit_only
    bonus_applied: list[str] = field(default_factory=list)
    notes: str = ""


def _count_hits(text: str, keywords: list[str]) -> int:
    """统计关键词命中数。"""
    text_lower = text.lower()
    return sum(1 for k in keywords if k in text_lower)


def _score_domain_relevance(item: Item, max_score: int) -> int:
    """领域相关性（满分 35）。

    - 命中 SE 核心关键词：每个 +5，上限 20
    - 命中 Agent 关键词：每个 +3，上限 15
    - 非论文类（博客/B站）默认给中等分
    """
    if item.source_type in ("blog", "bilibili", "wechat_mp"):
        # 非论文来源：engineering/ai 类给 25，tech 类给 15
        return 25 if item.category in ("engineering", "ai") else 15

    text = f"{item.title} {item.abstract or ''}"
    se_hits = _count_hits(text, SE_CORE_KEYWORDS)
    agent_hits = _count_hits(text, AGENT_KEYWORDS)
    # SE 核心 + Agent 主题同等权重（方案明确把 Agent 主题列为重点监控）
    score = min(se_hits * 5, 20) + min(agent_hits * 5, 15)
    return min(score, max_score)


def _score_practical_value(item: Item, max_score: int) -> int:
    """实践价值（满分 25）。

    - 命中实践关键词：每个 +5，上限 20
    - 有开放全文：+5
    """
    if item.source_type in ("blog", "bilibili", "wechat_mp"):
        return 15  # 工程实践内容默认中等

    text = f"{item.title} {item.abstract or ''}"
    practice_hits = _count_hits(text, PRACTICE_KEYWORDS)
    score = min(practice_hits * 5, 20)
    if item.has_full_text:
        score += 5
    return min(score, max_score)


def _score_evidence_quality(
    item: Item, max_score: int, bonus_cfg: dict[str, int]
) -> tuple[int, list[str]]:
    """证据质量（满分 20）+ 加分项。

    基础分：
    - 有 abstract：8
    - 有 authors：3
    - 有 venue：3
    - 有 published_at：3
    - 有 full_text_url：3
    加分项（在 evidence_quality 维度内，受满分上限约束）：
    - real_repository / real_pr / executable_benchmark / public_code / public_dataset / cost_analysis / failure_analysis
    """
    base = 0
    if item.abstract:
        base += 8
    if item.authors:
        base += 3
    if item.venue:
        base += 3
    if item.published_at:
        base += 3
    if item.full_text_url:
        base += 3

    applied: list[str] = []
    text = f"{item.title} {item.abstract or ''}".lower()
    bonus_map = {
        "real_repository": ["real repository", "real-world repository", "production repository"],
        "real_pr": ["real pr", "real pull request", "merged pr", "agent pr"],
        "executable_benchmark": ["executable benchmark", "benchmark suite", "swe-bench", "humaneval"],
        "public_code": ["github.com", "open-source", "open source", "code available", "source code"],
        "public_dataset": ["dataset released", "public dataset", "data available", "dataset"],
        "cost_analysis": ["cost analysis", "token cost", "api cost", "latency analysis"],
        "failure_analysis": ["failure mode", "failure analysis", "error analysis", "limitations"],
    }
    for key, patterns in bonus_map.items():
        if any(p in text for p in patterns):
            bonus = bonus_cfg.get(key, 0)
            base += bonus
            applied.append(f"evidence:{key}(+{bonus})")

    return min(base, max_score), applied


def _score_reproducibility(
    item: Item, max_score: int, bonus_cfg: dict[str, int]
) -> tuple[int, list[str]]:
    """可复现性（满分 10）+ 加分项。

    基础分：
    - 有 DOI 或 arXiv ID：3
    - 有 full_text_url：2
    加分项：
    - has_dockerfile / has_reproduction_script / has_data_release
    """
    base = 0
    if item.doi or item.arxiv_id:
        base += 3
    if item.full_text_url:
        base += 2

    applied: list[str] = []
    text = f"{item.title} {item.abstract or ''} {item.url}".lower()
    bonus_map = {
        "has_dockerfile": ["dockerfile", "docker image", "container"],
        "has_reproduction_script": ["reproduction script", "replication package", "replication kit"],
        "has_data_release": ["data release", "released dataset", "benchmark released"],
    }
    for key, patterns in bonus_map.items():
        if any(p in text for p in patterns):
            bonus = bonus_cfg.get(key, 0)
            base += bonus
            applied.append(f"repro:{key}(+{bonus})")

    return min(base, max_score), applied


def _score_timeliness(item: Item, timeliness_cfg: dict[str, int]) -> int:
    """时效性（满分 10）。

    按发布距今天数打分：
    - ≤30 天：10
    - ≤90 天：7
    - ≤180 天：4
    - ≤365 天：2
    - 更旧/无日期：0
    """
    if not item.published_at:
        return 0
    now = datetime.now(UTC)
    pub = item.published_at
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=UTC)
    days = (now - pub).days
    if days <= 30:
        return timeliness_cfg.get("within_30_days", 10)
    elif days <= 90:
        return timeliness_cfg.get("within_90_days", 7)
    elif days <= 180:
        return timeliness_cfg.get("within_180_days", 4)
    elif days <= 365:
        return timeliness_cfg.get("within_365_days", 2)
    return timeliness_cfg.get("older", 0)


def _determine_tier(total: int, tiers: dict[str, int]) -> str:
    """根据总分确定分档。"""
    if total >= tiers["selected"]:
        return "selected"
    elif total >= tiers["archived"]:
        return "archived"
    return "audit_only"


def score_item(item: Item, cfg: ScoringConfig) -> ScoreBreakdown:
    """对单个条目进行五维评分。"""
    max_dr = cfg.weights["domain_relevance"]
    max_pv = cfg.weights["practical_value"]
    max_eq = cfg.weights["evidence_quality"]
    max_rp = cfg.weights["reproducibility"]

    dr = _score_domain_relevance(item, max_dr)
    pv = _score_practical_value(item, max_pv)
    eq, eq_bonus = _score_evidence_quality(item, max_eq, cfg.bonus.get("evidence_quality", {}))
    rp, rp_bonus = _score_reproducibility(item, max_rp, cfg.bonus.get("reproducibility", {}))
    tl = _score_timeliness(item, cfg.timeliness)

    total = dr + pv + eq + rp + tl
    tier = _determine_tier(total, cfg.tiers)

    return ScoreBreakdown(
        domain_relevance=dr,
        practical_value=pv,
        evidence_quality=eq,
        reproducibility=rp,
        timeliness=tl,
        total=total,
        tier=tier,
        bonus_applied=eq_bonus + rp_bonus,
    )


def score_batch(items: list[Item], cfg: ScoringConfig) -> list[tuple[Item, ScoreBreakdown]]:
    """对一批条目评分，返回 [(item, breakdown)]，按总分降序。"""
    results = [(it, score_item(it, cfg)) for it in items]
    results.sort(key=lambda x: x[1].total, reverse=True)
    logger.info(
        "scored batch: {} items, selected={} archived={} audit_only={}",
        len(items),
        sum(1 for _, s in results if s.tier == "selected"),
        sum(1 for _, s in results if s.tier == "archived"),
        sum(1 for _, s in results if s.tier == "audit_only"),
    )
    return results

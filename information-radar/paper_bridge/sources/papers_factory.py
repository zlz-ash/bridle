"""论文来源工厂：从 sources.yaml 的 papers 配置构造全部论文来源实例。"""
from __future__ import annotations

from typing import Any

from paper_bridge.sources.arxiv import ArxivSource
from paper_bridge.sources.crossref import CrossrefSource
from paper_bridge.sources.semantic_scholar import SemanticScholarSource


def build_paper_sources(papers_cfg: dict[str, Any]) -> list:
    """根据 sources.yaml 的 papers 段构造论文来源。

    papers_cfg 形如：
      {"arxiv": {...}, "crossref": {...}, "semantic_scholar": {...}}
    """
    sources: list = []
    if arxiv_cfg := papers_cfg.get("arxiv"):
        sources.append(
            ArxivSource(
                categories=arxiv_cfg.get("categories", ["cs.SE"]),
                keywords=arxiv_cfg.get("keywords"),
                max_results_per_category=arxiv_cfg.get("max_results_per_category", 50),
            )
        )
    if crossref_cfg := papers_cfg.get("crossref"):
        sources.append(
            CrossrefSource(
                venues=crossref_cfg.get("venues", []),
                days_back=crossref_cfg.get("days_back", 365),
                max_results_per_venue=crossref_cfg.get("max_results_per_venue", 30),
            )
        )
    if s2_cfg := papers_cfg.get("semantic_scholar"):
        sources.append(
            SemanticScholarSource(
                queries=s2_cfg.get("queries", []),
                fields=s2_cfg.get("fields"),
                max_results_per_query=s2_cfg.get("max_results_per_query", 30),
                days_back=s2_cfg.get("days_back", 365),
            )
        )
    return sources

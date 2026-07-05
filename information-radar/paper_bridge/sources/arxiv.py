"""arXiv 论文来源：经 arXiv Atom API 采集。

API: https://export.arxiv.org/api/query?search_query=cat:cs.SE&start=0&max_results=50
返回 Atom feed，用 feedparser 解析，提取 arXiv ID / DOI / 作者 / PDF 链接。
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from loguru import logger

from paper_bridge.http_client import fetch_text
from paper_bridge.models import Item
from paper_bridge.sources.base import feed_to_items

ARXIV_API = "https://export.arxiv.org/api/query"


def _extract_arxiv_id(entry) -> str | None:
    """从 entry.id（形如 http://arxiv.org/abs/2401.00001v1）提取 arXiv ID。"""
    eid = entry.get("id") or ""
    m = re.search(r"arxiv\.org/abs/([^/]+)$", eid)
    if m:
        return m.group(1)
    return None


def _extract_pdf_link(entry) -> str | None:
    """从 entry.links 找 PDF 链接。"""
    for link in entry.get("links", []):
        if link.get("type") == "application/pdf" or "pdf" in link.get("href", "").lower():
            return link.get("href")
    return None


def _extract_doi(entry) -> str | None:
    """arXiv entry 的 doi 字段。"""
    for tag in ("arxiv_doi", "doi"):
        v = entry.get(tag)
        if v:
            return v
    return None


def _extract_authors(entry) -> list[str]:
    authors = []
    for a in entry.get("authors", []):
        name = a.get("name")
        if name:
            authors.append(name)
    return authors


class ArxivSource:
    """arXiv 论文来源。

    按分类（cs.SE/cs.AI/cs.CL）采集，可叠加关键词过滤。
    每个分类单独请求，合并结果。
    """

    source_type = "arxiv"

    def __init__(
        self,
        categories: list[str],
        keywords: list[str] | None = None,
        max_results_per_category: int = 50,
        sort_by: str = "submittedDate",
        sort_order: str = "descending",
    ):
        self.categories = categories
        self.keywords = [k.lower() for k in keywords] if keywords else []
        self.max_results = max_results_per_category
        self.sort_by = sort_by
        self.sort_order = sort_order

    @property
    def name(self) -> str:
        return "arxiv"

    @property
    def url(self) -> str:
        # 主要用于日志/审计
        cats = "+OR+".join(f"cat:{c}" for c in self.categories)
        return f"{ARXIV_API}?search_query={cats}"

    def _build_query(self, category: str) -> dict:
        return {
            "search_query": f"cat:{category}",
            "start": 0,
            "max_results": self.max_results,
            "sortBy": self.sort_by,
            "sortOrder": self.sort_order,
        }

    def _keyword_match(self, title: str, abstract: str | None) -> bool:
        if not self.keywords:
            return True
        text = f"{title} {abstract or ''}".lower()
        return any(k in text for k in self.keywords)

    def fetch(self, client) -> Iterable[Item]:
        import httpx

        all_items: list[Item] = []
        for cat in self.categories:
            params = self._build_query(cat)
            url = f"{ARXIV_API}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
            logger.info("arxiv fetch: cat={} url={}", cat, url)
            try:
                text = fetch_text(client, url)
            except httpx.HTTPError as e:
                logger.warning("arxiv cat={} fetch failed: {}", cat, e)
                continue
            items = feed_to_items(text, "arxiv", f"arxiv:{cat}", "se_paper")
            for it in items:
                # 填充论文特有字段
                entry = it.raw
                it.arxiv_id = _extract_arxiv_id(entry)
                it.doi = _extract_doi(entry)
                it.full_text_url = _extract_pdf_link(entry)
                it.has_full_text = it.full_text_url is not None
                it.authors = _extract_authors(entry)
                it.venue = f"arXiv {cat}"
                # 关键词过滤
                if not self._keyword_match(it.title, it.abstract):
                    continue
                all_items.append(it)
            logger.info("arxiv cat={} items={} (after keyword filter)", cat, len(all_items))
        return all_items

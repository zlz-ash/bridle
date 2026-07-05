"""Semantic Scholar 论文来源：经 Graph API 搜索。

API: https://api.semanticscholar.org/graph/v1/paper/search
支持 year / openAccessPdf / fieldsOfStudy 过滤。
字段：title, abstract, authors, venue, year, externalIds, openAccessPdf, url
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from loguru import logger

from paper_bridge.http_client import fetch_json
from paper_bridge.models import Item

S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"


def _parse_year(year) -> datetime | None:
    if year is None:
        return None
    try:
        return datetime(int(year), 1, 1)
    except (TypeError, ValueError):
        return None


class SemanticScholarSource:
    """Semantic Scholar 论文来源。

    按查询语句检索，支持年份范围与字段过滤。
    每个查询单独请求，合并结果。
    """

    source_type = "semantic_scholar"

    def __init__(
        self,
        queries: list[str],
        fields: list[str] | None = None,
        max_results_per_query: int = 30,
        days_back: int = 365,
    ):
        self.queries = queries
        self.fields = ",".join(fields) if fields else "title,abstract,authors,venue,year,externalIds,openAccessPdf,url"
        self.max_results = max_results_per_query
        self.days_back = days_back

    @property
    def name(self) -> str:
        return "semantic_scholar"

    @property
    def url(self) -> str:
        return S2_API

    def _year_filter(self) -> str:
        from datetime import UTC, timedelta
        from datetime import datetime as dt

        start = dt.now(UTC) - timedelta(days=self.days_back)
        return f"{start.year}-{dt.now(UTC).year}"

    def _build_params(self, query: str) -> dict:
        return {
            "query": query,
            "fields": self.fields,
            "limit": self.max_results,
            "year": self._year_filter(),
        }

    def _parse_paper(self, paper: dict, query: str) -> Item | None:
        title = paper.get("title")
        if not title:
            return None
        url = paper.get("url")
        if not url:
            paper_id = paper.get("paperId")
            if paper_id:
                url = f"https://www.semanticscholar.org/paper/{paper_id}"
            else:
                return None

        external_ids = paper.get("externalIds") or {}
        doi = external_ids.get("DOI")
        arxiv_id = external_ids.get("ArXiv") or external_ids.get("ARXIV")

        open_pdf = paper.get("openAccessPdf")
        full_text_url = open_pdf.get("url") if open_pdf else None
        has_full_text = bool(full_text_url)

        authors = [a.get("name") for a in (paper.get("authors") or []) if a.get("name")]
        affiliations: list[str] = []
        for a in (paper.get("authors") or []):
            for aff in (a.get("affiliations") or []):
                if aff and aff not in affiliations:
                    affiliations.append(aff)

        venue = paper.get("venue")
        year = paper.get("year")
        published_at = _parse_year(year)

        return Item(
            source_type="semantic_scholar",
            source_name=f"s2:{query}",
            title=title.strip(),
            url=url.strip(),
            published_at=published_at,
            authors=authors,
            affiliations=affiliations,
            venue=venue,
            abstract=(paper.get("abstract") or "").strip() or None,
            full_text_url=full_text_url,
            has_full_text=has_full_text,
            category="se_paper",
            doi=doi,
            arxiv_id=arxiv_id,
            raw=paper,
        )

    def fetch(self, client) -> Iterable[Item]:
        import httpx

        all_items: list[Item] = []
        for query in self.queries:
            params = self._build_params(query)
            logger.info("semantic_scholar fetch: query={}", query)
            try:
                data = fetch_json(client, S2_API, params=params)
            except httpx.HTTPError as e:
                logger.warning("semantic_scholar query={!r} fetch failed: {}", query, e)
                continue
            papers = data.get("data", []) if isinstance(data, dict) else []
            for p in papers:
                item = self._parse_paper(p, query)
                if item:
                    all_items.append(item)
            logger.info(
                "semantic_scholar query={!r} total={} fetched={}",
                query,
                data.get("total", 0) if isinstance(data, dict) else 0,
                len(papers),
            )
        return all_items

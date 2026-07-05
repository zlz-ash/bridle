"""Crossref 论文来源：检索顶级软件工程会议论文。

API: https://api.crossref.org/works
支持 query.bibliographic + filter(from-pub-date, type) 过滤。
字段：DOI, title, author, published, container-title, abstract, URL, link
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

from loguru import logger

from paper_bridge.http_client import fetch_json
from paper_bridge.models import Item

CROSSREF_API = "https://api.crossref.org/works"


def _parse_date(parts: list[int] | None) -> datetime | None:
    if not parts:
        return None
    try:
        return datetime(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
    except (TypeError, ValueError, IndexError):
        return None


def _strip_tags(text: str | None) -> str | None:
    """Crossref abstract 常含 XML 标签，简单去除。"""
    if not text:
        return None
    return re.sub(r"<[^>]+>", "", text).strip() or None


class CrossrefSource:
    """Crossref 论文来源。

    按会议名（ICSE/FSE/ASE/MSR）检索 proceedings-article。
    每个会议单独请求，合并结果。
    """

    source_type = "crossref"

    def __init__(
        self,
        venues: list[str],
        days_back: int = 365,
        max_results_per_venue: int = 30,
    ):
        self.venues = venues
        self.days_back = days_back
        self.max_results = max_results_per_venue

    @property
    def name(self) -> str:
        return "crossref"

    @property
    def url(self) -> str:
        return CROSSREF_API

    def _from_date(self) -> str:
        from datetime import UTC, timedelta
        from datetime import datetime as dt

        return (dt.now(UTC) - timedelta(days=self.days_back)).strftime("%Y-%m-%d")

    def _build_params(self, venue: str) -> dict:
        return {
            "query.bibliographic": venue,
            "filter": f"from-pub-date:{self._from_date()},type:proceedings-article",
            "rows": self.max_results,
            "select": "DOI,title,author,published,container-title,abstract,URL,link",
        }

    def _parse_item(self, work: dict, venue: str) -> Item | None:
        titles = work.get("title") or []
        title = titles[0] if titles else None
        if not title:
            return None

        url = work.get("URL")
        if not url:
            doi = work.get("DOI")
            if doi:
                url = f"https://doi.org/{doi}"
            else:
                return None

        doi = work.get("DOI")
        authors = []
        affiliations: list[str] = []
        for a in (work.get("author") or []):
            given = a.get("given", "")
            family = a.get("family", "")
            full = f"{given} {family}".strip()
            if full:
                authors.append(full)
            for aff in (a.get("affiliation") or []):
                name = aff.get("name")
                if name and name not in affiliations:
                    affiliations.append(name)

        date_parts = (work.get("published") or {}).get("date-parts", [[None]])
        published_at = _parse_date(date_parts[0] if date_parts else None)

        container = work.get("container-title") or []
        venue_str = container[0] if container else venue

        # PDF 链接
        full_text_url = None
        for link in (work.get("link") or []):
            if link.get("content-type") == "application/pdf":
                full_text_url = link.get("URL")
                break

        abstract = _strip_tags(work.get("abstract"))

        return Item(
            source_type="crossref",
            source_name=f"crossref:{venue}",
            title=title.strip(),
            url=url.strip(),
            published_at=published_at,
            authors=authors,
            affiliations=affiliations,
            venue=venue_str,
            abstract=abstract,
            full_text_url=full_text_url,
            has_full_text=bool(full_text_url),
            category="se_paper",
            doi=doi,
            arxiv_id=None,
            raw=work,
        )

    def fetch(self, client) -> Iterable[Item]:
        import httpx

        all_items: list[Item] = []
        for venue in self.venues:
            params = self._build_params(venue)
            logger.info("crossref fetch: venue={}", venue)
            try:
                data = fetch_json(client, CROSSREF_API, params=params)
            except httpx.HTTPError as e:
                logger.warning("crossref venue={} fetch failed: {}", venue, e)
                continue
            items = data.get("message", {}).get("items", []) if isinstance(data, dict) else []
            for work in items:
                item = self._parse_item(work, venue)
                if item:
                    all_items.append(item)
            logger.info("crossref venue={} items={}", venue, len(items))
        return all_items

"""RSS 来源基类：共享 feedparser 解析 + Item 归一化。

博客、B站（经 RSSHub）、公众号（经 WeWe-RSS）都产出 RSS/Atom feed，
统一用 feedparser 解析后转为 Item。
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

import feedparser
from loguru import logger

from paper_bridge.http_client import fetch_text
from paper_bridge.models import Item


def _parse_published(entry) -> datetime | None:
    """从 feedparser entry 解析发布时间，失败返回 None。"""
    for key in ("published_parsed", "updated_parsed"):
        tp = entry.get(key)
        if tp:
            try:

                return datetime(*tp[:6])
            except (TypeError, ValueError):
                continue
    return None


def _first(*values) -> str | None:
    """返回第一个非空值。"""
    for v in values:
        if v:
            return v if isinstance(v, str) else str(v)
    return None


def _entry_url(entry) -> str | None:
    """从 entry 提取 URL。

    link 优先；id/link 仅在是 http(s) URL 时才用。
    feedparser 会把 <id> 复制到 link（guidislink），所以两者都要校验是否合法 URL。
    """
    for key in ("link", "id"):
        v = entry.get(key)
        if v and isinstance(v, str) and (v.startswith("http://") or v.startswith("https://")):
            return v
    return None


def feed_to_items(
    feed_text: str,
    source_type: str,
    source_name: str,
    category: str | None = None,
) -> list[Item]:
    """把 RSS/Atom 文本解析为 Item 列表。"""
    parsed = feedparser.parse(feed_text)
    items: list[Item] = []
    for entry in parsed.entries:
        title = _first(entry.get("title"), entry.get("summary", "")[:60])
        if not title:
            continue
        url = _entry_url(entry)
        if not url:
            continue
        abstract = entry.get("summary") or entry.get("description")
        items.append(
            Item(
                source_type=source_type,  # type: ignore[arg-type]
                source_name=source_name,
                title=title.strip(),
                url=url.strip(),
                published_at=_parse_published(entry),
                abstract=abstract.strip() if abstract else None,
                category=category,  # type: ignore[arg-type]
                raw=dict(entry),
            )
        )
    logger.info(
        "feed parsed: source={} entries={} items={}",
        source_name,
        len(parsed.entries),
        len(items),
    )
    return items


class RSSSource:
    """RSS 来源基类。子类提供 name/url/category。"""

    source_type: str = "blog"

    def __init__(self, name: str, url: str, category: str | None = None):
        self.name = name
        self.url = url
        self.category = category

    def fetch(self, client) -> Iterable[Item]:
        """拉取并解析。失败抛异常给上层隔离。"""
        text = fetch_text(client, self.url)
        return feed_to_items(text, self.source_type, self.name, self.category)

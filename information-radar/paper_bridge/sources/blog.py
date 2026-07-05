"""工程博客 RSS 直采。"""
from __future__ import annotations

from paper_bridge.sources.base import RSSSource


class BlogRSSSource(RSSSource):
    """博客 RSS：直接 GET feed URL 并解析。"""

    source_type = "blog"

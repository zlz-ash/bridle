"""微信公众号：经 WeWe-RSS 桥接。

WeWe-RSS 为每个订阅的公众号生成 RSS feed，路径形如：
  {wewe}/feeds/{feed_id}.atom
具体路径前缀以 WeWe-RSS 版本为准，本类支持自定义 path_template。
"""
from __future__ import annotations

from paper_bridge.sources.base import RSSSource


class WechatMpSource(RSSSource):
    """公众号来源。feed_id 由 WeWe-RSS 后台分配。"""

    source_type = "wechat_mp"

    def __init__(
        self,
        name: str,
        feed_id: str,
        wewe_rss_url: str,
        path_template: str = "/feeds/{feed_id}.atom",
        category: str | None = "tech",
    ):
        base = wewe_rss_url.rstrip("/")
        url = base + path_template.format(feed_id=feed_id)
        super().__init__(name=name, url=url, category=category)
        self.feed_id = feed_id

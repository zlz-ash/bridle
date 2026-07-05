"""B站指定账号动态：经 RSSHub 桥接。

URL 模板：{rsshub}/bilibili/user/dynamic/{uid}
"""
from __future__ import annotations

from paper_bridge.sources.base import RSSSource


class BilibiliSource(RSSSource):
    """B站动态来源。

    uid 是 B站用户 UID；rsshub_url 是 RSSHub 服务地址（Compose 内部走 NO_PROXY）。
    """

    source_type = "bilibili"

    def __init__(self, name: str, uid: str, rsshub_url: str, category: str | None = "tech"):
        url = f"{rsshub_url.rstrip('/')}/bilibili/user/dynamic/{uid}"
        super().__init__(name=name, url=url, category=category)
        self.uid = uid

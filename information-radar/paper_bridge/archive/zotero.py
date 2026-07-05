"""Zotero 归档：通过 Web API 收藏论文。

业务契约（来自方案）：
- 高价值论文进入 Zotero
- 使用 Zotero Web API（api.zotero.org，userID + API key + collection ID）
"""
from __future__ import annotations

import httpx
from loguru import logger

from paper_bridge.report.brief import BriefItem

ZOTERO_API = "https://api.zotero.org"


class ZoteroArchiver:
    """Zotero 论文收藏器。

    通过 Zotero Web API 的 /users/{userID}/items 端点创建条目。
    高价值论文（selected tier）收藏到指定 collection。
    """

    def __init__(
        self,
        user_id: str,
        api_key: str,
        collection_id: str = "",
        proxy: str | None = "http://127.0.0.1:7890",
    ):
        self.user_id = user_id
        self.api_key = api_key
        self.collection_id = collection_id
        self._client: httpx.Client | None = None
        self._proxy = proxy

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            kwargs = {
                "timeout": 30.0,
                "headers": {
                    "Zotero-API-Key": self.api_key,
                    "Content-Type": "application/json",
                },
            }
            if self._proxy:
                kwargs["proxy"] = self._proxy
            self._client = httpx.Client(**kwargs)
        return self._client

    def _build_journal_article(self, item: BriefItem) -> dict:
        """构造 Zotero journalArticle 条目。"""
        creators = []
        for author in item.authors:
            parts = author.rsplit(" ", 1)
            if len(parts) == 2:
                creators.append({"creatorType": "author", "firstName": parts[0], "lastName": parts[1]})
            else:
                creators.append({"creatorType": "author", "name": author})

        data = {
            "itemType": "journalArticle",
            "title": item.title,
            "creators": creators,
            "abstractNote": item.summary.get("research_question", ""),
            "publicationTitle": item.venue or "",
            "date": item.date or "",
            "url": item.url,
            "tags": [{"tag": item.tier}, {"tag": item.source_type}],
        }
        if item.doi:
            data["DOI"] = item.doi
        if item.pdf_url:
            data["url"] = item.pdf_url  # 优先 PDF

        # collection 归属
        if self.collection_id:
            data["collections"] = [self.collection_id]
        return [{"data": data, "itemType": "journalArticle"}]

    def archive_item(self, item: BriefItem) -> bool:
        """收藏单篇论文到 Zotero。"""
        try:
            client = self._get_client()
            payload = self._build_journal_article(item)
            resp = client.post(
                f"{ZOTERO_API}/users/{self.user_id}/items",
                json=payload,
            )
            if resp.status_code == 200:
                logger.debug("zotero archived: {}", item.title[:50])
                return True
            logger.warning("zotero archive status={}: {}", resp.status_code, resp.text[:200])
            return False
        except Exception as e:
            logger.error("zotero archive failed: {}", e)
            return False

    def archive_batch(self, items: list[BriefItem]) -> tuple[int, int]:
        """批量收藏高价值论文。返回 (成功数, 失败数)。"""
        success = 0
        failed = 0
        for item in items:
            if self.archive_item(item):
                success += 1
            else:
                failed += 1
        logger.info("zotero archive batch: ok={} fail={}", success, failed)
        return success, failed

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

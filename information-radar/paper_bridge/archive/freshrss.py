"""FreshRSS 归档：通过 Google Reader API 添加订阅。

业务契约（来自方案）：
- 普通内容进入 FreshRSS
- 使用 FreshRSS 的 Google Reader API（需 API 密码）
"""
from __future__ import annotations

import httpx
from loguru import logger

from paper_bridge.report.brief import BriefItem


class FreshRSSArchiver:
    """FreshRSS 归档器。

    通过 Google Reader API 的 subscription/edit 端点添加订阅源。
    对于单篇内容，使用 bookmark 方式保存 URL。
    """

    def __init__(
        self,
        base_url: str = "http://freshrss:80",
        api_user: str = "admin",
        api_password: str = "",
        proxy: str | None = None,  # 内部服务不走代理
    ):
        self.base_url = base_url.rstrip("/")
        self.api_user = api_user
        self.api_password = api_password
        self._client: httpx.Client | None = None
        self._proxy = proxy
        self._auth_token: str | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            kwargs = {"timeout": 30.0}
            if self._proxy:
                kwargs["proxy"] = self._proxy
            self._client = httpx.Client(**kwargs)
        return self._client

    def _login(self) -> bool:
        """登录获取 Auth token。"""
        if self._auth_token:
            return True
        try:
            client = self._get_client()
            resp = client.post(
                f"{self.base_url}/api/greader.php/accounts/ClientLogin",
                data={"Email": self.api_user, "Passwd": self.api_password},
            )
            resp.raise_for_status()
            for line in resp.text.splitlines():
                if line.startswith("Auth="):
                    self._auth_token = line[5:]
                    logger.info("freshrss login ok: user={}", self.api_user)
                    return True
            logger.error("freshrss login: no Auth token in response")
            return False
        except Exception as e:
            logger.error("freshrss login failed: {}", e)
            return False

    def _auth_header(self) -> dict:
        return {"Authorization": f"GoogleLogin auth={self._auth_token}"} if self._auth_token else {}

    def archive_item(self, item: BriefItem) -> bool:
        """归档单篇内容到 FreshRSS（添加为订阅源/书签）。"""
        if not self._login():
            return False
        try:
            client = self._get_client()
            # 使用 subscription/edit 添加 URL 为订阅源
            resp = client.post(
                f"{self.base_url}/api/greader.php/reader/api/0/subscription/edit",
                data={
                    "s": f"feed/{item.url}",
                    "ac": "subscribe",
                    "t": item.title,
                },
                headers=self._auth_header(),
            )
            if resp.status_code == 200:
                logger.debug("freshrss archived: {}", item.title[:50])
                return True
            logger.warning("freshrss archive status={}: {}", resp.status_code, resp.text[:100])
            return False
        except Exception as e:
            logger.error("freshrss archive failed: {}", e)
            return False

    def archive_batch(self, items: list[BriefItem]) -> tuple[int, int]:
        """批量归档。返回 (成功数, 失败数)。"""
        success = 0
        failed = 0
        for item in items:
            if self.archive_item(item):
                success += 1
            else:
                failed += 1
        logger.info("freshrss archive batch: ok={} fail={}", success, failed)
        return success, failed

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
        self._auth_token = None

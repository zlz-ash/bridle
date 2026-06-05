"""OpenAI-compatible HTTP client adapter with injectable mock."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Protocol

logger = logging.getLogger("bridle")


class OpenAICompatibleClient(Protocol):
    async def chat_completion(
        self,
        *,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        ...


class HttpOpenAICompatibleClient:
    """POST /chat/completions via urllib (proxy-aware).

    Works with any OpenAI-compatible API (DeepSeek, OpenAI, local models, etc.).
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        proxy: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._proxy = proxy

    async def chat_completion(
        self,
        *,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        import asyncio

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools

        url = f"{self._base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        payload = json.dumps(body).encode("utf-8")

        def _post() -> dict[str, Any]:
            handlers: list = []
            if self._proxy:
                handlers.append(urllib.request.ProxyHandler({
                    "http": self._proxy,
                    "https": self._proxy,
                }))
            opener = urllib.request.build_opener(*handlers)
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            try:
                with opener.open(req, timeout=timeout_seconds) as resp:
                    data = resp.read().decode("utf-8", errors="replace")
                    return json.loads(data)
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", errors="replace")
                raise LLMHttpError(exc.code, err_body) from exc
            except TimeoutError as exc:
                raise LLMHttpError(408, "timeout") from exc

        return await asyncio.to_thread(_post)


class LLMHttpError(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"LLM HTTP {status_code}")

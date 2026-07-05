"""HTTP 客户端：统一代理、超时、重试、User-Agent。

所有来源采集器共享此客户端，保证：
- 外部请求走 7890 代理
- 内部服务（rsshub/wewe/freshrss）走 NO_PROXY
- 重试由 tenacity 控制，单来源失败不影响其他
"""
from __future__ import annotations

import httpx
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


def build_client(
    proxy: str | None = "http://127.0.0.1:7890",
    no_proxy: str = "localhost,127.0.0.1",
    timeout: float = 30.0,
) -> httpx.Client:
    """构造带代理的 httpx.Client。

    no_proxy 中的主机直连（用于 Compose 内部服务 rsshub/wewe/freshrss），
    其余走 7890 代理。httpx 通过 mounts 按目标主机分流。
    """
    proxy_transport = httpx.HTTPTransport(proxy=proxy, retries=1) if proxy else httpx.HTTPTransport()
    direct_transport = httpx.HTTPTransport()

    mounts: dict[str, httpx.BaseTransport] = {"all://": proxy_transport}
    for host in [h.strip() for h in no_proxy.split(",") if h.strip()]:
        # httpx 0.28: all://host 覆盖该主机所有端口，不支持 all://host:* 通配
        mounts[f"all://{host}"] = direct_transport

    return httpx.Client(
        timeout=timeout,
        mounts=mounts,
        follow_redirects=True,
        headers={"User-Agent": "InformationRadar/0.1 (+https://github.com/ash)"},
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    reraise=True,
)
def fetch_text(client: httpx.Client, url: str) -> str:
    """带重试的 GET，返回文本。失败抛出异常给上层隔离。"""
    logger.debug("fetch GET {}", url)
    r = client.get(url)
    r.raise_for_status()
    return r.text


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    reraise=True,
)
def fetch_json(client: httpx.Client, url: str, params: dict | None = None) -> dict | list:
    logger.debug("fetch JSON {} params={}", url, params)
    r = client.get(url, params=params)
    r.raise_for_status()
    return r.json()

"""Environment variables for agent containers."""
from __future__ import annotations

import os

_DEFAULT_CONTAINER_PROXY = "http://host.docker.internal:7890"
_BYPASS_HOSTS = ("host.docker.internal", "localhost", "127.0.0.1")


def _normalize_proxy(value: str | None) -> str:
    if not value:
        return _DEFAULT_CONTAINER_PROXY
    return value.replace("127.0.0.1", "host.docker.internal").replace("localhost", "host.docker.internal")


def _merge_no_proxy(value: str | None) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for raw in [*(value or "").split(","), *_BYPASS_HOSTS]:
        host = raw.strip()
        if host and host not in seen:
            parts.append(host)
            seen.add(host)
    return ",".join(parts)


def build_agent_container_env(
    *,
    run_id: str | None = None,
    node_id: str | None = None,
    network_allowed: bool = False,
) -> dict[str, str]:
    """Build container env without model credentials; proxy only when network is allowed."""
    env: dict[str, str] = {
        "BRIDLE_BACKEND_URL": os.environ.get("BRIDLE_BACKEND_URL", "http://host.docker.internal:8900"),
    }
    if network_allowed:
        http_proxy = _normalize_proxy(os.environ.get("HTTP_PROXY"))
        https_proxy = _normalize_proxy(os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"))
        no_proxy = _merge_no_proxy(os.environ.get("NO_PROXY") or os.environ.get("no_proxy"))
        env.update(
            {
                "HTTP_PROXY": http_proxy,
                "HTTPS_PROXY": https_proxy,
                "NO_PROXY": no_proxy,
                "no_proxy": no_proxy,
            }
        )
    if run_id:
        env["BRIDLE_RUN_ID"] = run_id
    if node_id:
        env["BRIDLE_NODE_ID"] = node_id
    return env

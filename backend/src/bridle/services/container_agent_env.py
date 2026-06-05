"""Shared environment variables for agent containers."""
from __future__ import annotations

import os

_DEFAULT_CONTAINER_PROXY = "http://host.docker.internal:7890"

# Hosts that MUST be reached directly, never through the host proxy.
# - host.docker.internal: the bridle backend lives here; Clash refuses local hops.
# - localhost / 127.0.0.1: container loopback (rare, but keep for safety).
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


def build_agent_container_env(*, run_id: str | None = None, node_id: str | None = None) -> dict[str, str]:
    http_proxy = _normalize_proxy(os.environ.get("HTTP_PROXY"))
    https_proxy = _normalize_proxy(os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"))
    no_proxy = _merge_no_proxy(os.environ.get("NO_PROXY") or os.environ.get("no_proxy"))
    env: dict[str, str] = {
        "BRIDLE_BACKEND_URL": os.environ.get("BRIDLE_BACKEND_URL", "http://host.docker.internal:8900"),
        "BRIDLE_AGENT_API_KEY": os.environ.get("BRIDLE_AGENT_API_KEY", ""),
        "BRIDLE_AGENT_MODEL": os.environ.get("BRIDLE_AGENT_MODEL", ""),
        "BRIDLE_AGENT_PROVIDER": os.environ.get("BRIDLE_AGENT_PROVIDER", "deepseek"),
        "HTTP_PROXY": http_proxy,
        "HTTPS_PROXY": https_proxy,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,  # some libs only respect lowercase
    }
    if run_id:
        env["BRIDLE_RUN_ID"] = run_id
    if node_id:
        env["BRIDLE_NODE_ID"] = node_id
    return env

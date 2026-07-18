"""Container-bound model tools for one agent run."""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

from bridle.agent.safety.sandbox_policy import SandboxPolicy
from bridle.logging.jsonl import log_event
from bridle.observability import get_observability


class SandboxedToolExecutor:
    """Execute the minimal model tool set with structured audit logging."""

    def __init__(self, policy: SandboxPolicy, *, test_backend: Any | None = None) -> None:
        self.policy = policy
        self._test_backend = test_backend

    async def run_command(
        self,
        command: str,
        *,
        authority: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._tool_call(
            "run_command",
            {"command_length": len(command)},
            self._run_command_impl(
                command,
                authority=authority,
                command_id=command_id,
            ),
        )

    async def report_blocked(
        self,
        reason: str,
        evidence: dict | None = None,
    ) -> dict[str, Any]:
        return await self._tool_call(
            "report_blocked",
            {"reason": reason},
            self._report_blocked_impl(reason, evidence),
        )

    async def web_search(
        self,
        query: str,
        *,
        allowed_domains: list[str] | None = None,
        max_results: int = 5,
    ) -> dict[str, Any]:
        return await self._tool_call(
            "web_search",
            {
                "query_len": len(query),
                "domain_count": len(allowed_domains) if allowed_domains else 0,
                "max_results": max_results,
            },
            self._web_search_impl(
                query,
                allowed_domains=allowed_domains,
                max_results=max_results,
            ),
        )

    async def _report_blocked_impl(
        self,
        reason: str,
        evidence: dict | None,
    ) -> dict[str, Any]:
        return _completed({"reason": reason, "evidence": evidence or {}})

    async def _web_search_impl(
        self,
        query: str,
        *,
        allowed_domains: list[str] | None = None,
        max_results: int = 5,
    ) -> dict[str, Any]:
        if not self.policy.network_allowed:
            return _failed("NetworkDisabled", ["Network access is disabled in sandbox policy"])
        capped = min(max(1, max_results), 10)
        proxy_url = os.environ.get(
            "HTTPS_PROXY",
            os.environ.get("HTTP_PROXY", "http://127.0.0.1:7890"),
        )
        try:
            encoded_query = urllib.parse.quote_plus(query)
            url = (
                "https://api.duckduckgo.com/"
                f"?q={encoded_query}&format=json&no_redirect=1"
            )
            proxy_handler = urllib.request.ProxyHandler(
                {"https": proxy_url, "http": proxy_url}
            )
            opener = urllib.request.build_opener(proxy_handler)
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "BridleAgent/1.0"},
            )
            with opener.open(request, timeout=15) as response:
                body = response.read().decode("utf-8", errors="replace")
            data = json.loads(body)
        except Exception as exc:
            return _failed(
                "WebSearchError",
                [f"Search request failed: {type(exc).__name__}"],
            )
        results: list[dict[str, Any]] = []
        for item in data.get("RelatedTopics", []):
            if len(results) >= capped:
                break
            if not isinstance(item, dict):
                continue
            title = str(item.get("Text", ""))[:200]
            url_value = str(item.get("FirstURL", ""))
            if not url_value:
                continue
            domain = urllib.parse.urlparse(url_value).netloc
            if allowed_domains and domain not in allowed_domains:
                continue
            results.append(
                {
                    "title": title,
                    "url": url_value,
                    "snippet": title[:150],
                    "domain": domain,
                }
            )
        abstract = str(data.get("Abstract", ""))
        abstract_url = str(data.get("AbstractURL", ""))
        if abstract and abstract_url and len(results) < capped:
            domain = urllib.parse.urlparse(abstract_url).netloc
            if not allowed_domains or domain in allowed_domains:
                results.append(
                    {
                        "title": abstract[:200],
                        "url": abstract_url,
                        "snippet": abstract[:150],
                        "domain": domain,
                    }
                )
        return _completed({"search_results": results, "result_count": len(results)})

    async def _tool_call(
        self,
        tool_name: str,
        input_summary: dict,
        coro,
    ) -> dict[str, Any]:
        started = time.monotonic()
        log_event(
            "sandbox_tool_started",
            "started",
            run_id=self.policy.run_id,
            node_id=self.policy.node_id,
            detail={"tool_name": tool_name, "input_summary": input_summary},
        )
        try:
            result = await coro
            duration_ms = int((time.monotonic() - started) * 1000)
            status = result.get("status", "completed")
            log_event(
                "sandbox_tool_completed",
                status,
                run_id=self.policy.run_id,
                node_id=self.policy.node_id,
                duration_ms=duration_ms,
                detail={
                    "tool_name": tool_name,
                    "error_code": result.get("error_code"),
                    "exit_code": result.get("exit_code"),
                },
            )
            get_observability().record_tool_call(
                tool_name=tool_name,
                input_summary=dict(input_summary),
                output_summary=dict(result),
                duration_ms=duration_ms,
                status=status,
                error_code=(
                    str(result.get("error_code")) if result.get("error_code") else None
                ),
                metadata={"run_id": self.policy.run_id, "node_id": self.policy.node_id},
            )
            result["duration_ms"] = duration_ms
            result["tool_name"] = tool_name
            return result
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            log_event(
                "sandbox_tool_failed",
                "failed",
                run_id=self.policy.run_id,
                node_id=self.policy.node_id,
                duration_ms=duration_ms,
                detail={"tool_name": tool_name, "error_code": type(exc).__name__},
            )
            get_observability().record_tool_call(
                tool_name=tool_name,
                input_summary=dict(input_summary),
                output_summary={"error": type(exc).__name__, "message": str(exc)},
                duration_ms=duration_ms,
                status="failed",
                error_code=type(exc).__name__,
                metadata={"run_id": self.policy.run_id, "node_id": self.policy.node_id},
            )
            return {
                "status": "failed",
                "tool_name": tool_name,
                "error_code": type(exc).__name__,
                "message": str(exc),
                "duration_ms": duration_ms,
            }

    async def _run_command_impl(
        self,
        command: str,
        *,
        authority: str | None,
        command_id: str | None,
    ) -> dict[str, Any]:
        if authority is not None or command_id is not None:
            return {
                "status": "failed",
                "authority": "exploratory",
                "error_code": "exploratory_authority_fixed",
            }
        if not command.strip():
            return {
                "status": "failed",
                "authority": "exploratory",
                "error_code": "command_required",
            }
        if self._test_backend is None:
            return {
                "status": "failed",
                "authority": "exploratory",
                "error_code": "container_backend_required",
            }
        raw = await self._test_backend.run_command(command, policy=self.policy)
        return {**raw, "authority": "exploratory"}


def _completed(payload: dict) -> dict[str, Any]:
    return {"status": "completed", **payload}


def _failed(error_code: str, errors: list[str]) -> dict[str, Any]:
    return {"status": "failed", "error_code": error_code, "errors": errors}

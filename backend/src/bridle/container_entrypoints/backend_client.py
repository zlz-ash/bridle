"""Synchronous HTTP client for main-agent → backend APIs."""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("bridle")


class BridleBackendClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._get(f"/api/v1/agent/coding-sessions/{session_id}")

    def poll_messages(self, session_id: str, *, created_after: str | None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if created_after:
            params["created_after"] = created_after
        data = self._get(f"/api/v1/agent/coding-sessions/{session_id}/messages", params=params)
        return list(data) if isinstance(data, list) else []

    def get_eligible_snapshot(self, session_id: str) -> dict[str, Any]:
        payload = self._get(f"/api/v1/agent/coding-sessions/{session_id}/eligible-nodes")
        return payload if isinstance(payload, dict) else {}

    def eligible_nodes(self, session_id: str) -> list[dict[str, Any]]:
        payload = self.get_eligible_snapshot(session_id)
        return list(payload.get("eligible_nodes") or [])

    def negotiate_complexity(self, plan_id: str) -> dict[str, Any]:
        return self._post(
            f"/api/v1/plans/{plan_id}/negotiate-complexity",
            {},
            timeout_seconds=180.0,
        )

    def fail_session(self, session_id: str, *, reason: str) -> dict[str, Any]:
        return self._post(
            f"/api/v1/agent/coding-sessions/{session_id}/fail",
            {"reason": reason},
        )

    def current_plan(self) -> dict[str, Any]:
        return self._get("/api/v1/plan/current")

    def select_node(self, session_id: str, node_id: str) -> dict[str, Any]:
        return self._post(
            f"/api/v1/agent/coding-sessions/{session_id}/select-node",
            {"intent": "select_node", "node_id": node_id, "reason": "main_agent"},
        )

    def post_assistant(self, session_id: str, content: str) -> dict[str, Any]:
        return self._post(
            f"/api/v1/agent/coding-sessions/{session_id}/messages",
            {"role": "assistant", "content": content},
        )

    def get_recent_failed_runs(self, session_id: str, limit: int = 3) -> list[dict[str, Any]]:
        data = self._get(
            f"/api/v1/agent/coding-sessions/{session_id}/recent-failed-runs",
            params={"limit": str(limit)},
        )
        return list(data) if isinstance(data, list) else []

    def _get(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        with httpx.Client(timeout=self._timeout) as client:
            response = client.get(url, params=params)
        self._raise_for_status(response)
        return response.json()

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        timeout = self._timeout if timeout_seconds is None else timeout_seconds
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=body)
        self._raise_for_status(response)
        return response.json()

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code >= 500:
            raise RuntimeError(f"http_{response.status_code}")
        if response.status_code >= 400:
            body = response.text[:1000]
            raise httpx.HTTPStatusError(
                f"{response.status_code} for {response.request.url} :: {body}",
                request=response.request,
                response=response,
            )

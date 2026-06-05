"""Main-agent polling loop (HTTP only, no ORM)."""
from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from bridle.container_entrypoints.backend_client import BridleBackendClient
from bridle.container_entrypoints.decider import Decision, DeepSeekDecider
from bridle.container_entrypoints.main_agent_config import MainAgentConfig

logger = logging.getLogger("bridle")

_TERMINAL_SESSION = frozenset({"completed", "failed", "cancelled"})


class _Decider(Protocol):
    def decide(
        self,
        chat_history: list[dict[str, Any]],
        plan: dict[str, Any],
        eligible: list[dict[str, Any]],
        *,
        failed_runs: list[dict[str, Any]] | None = None,
    ) -> Decision: ...


class _Client(Protocol):
    def get_session(self, session_id: str) -> dict[str, Any]: ...
    def poll_messages(self, session_id: str, *, created_after: str | None) -> list[dict[str, Any]]: ...
    def get_eligible_snapshot(self, session_id: str) -> dict[str, Any]: ...
    def eligible_nodes(self, session_id: str) -> list[dict[str, Any]]: ...
    def current_plan(self) -> dict[str, Any]: ...
    def negotiate_complexity(self, plan_id: str) -> dict[str, Any]: ...
    def fail_session(self, session_id: str, *, reason: str) -> dict[str, Any]: ...
    def select_node(self, session_id: str, node_id: str) -> dict[str, Any]: ...
    def post_assistant(self, session_id: str, content: str) -> dict[str, Any]: ...
    def get_recent_failed_runs(self, session_id: str, limit: int = 3) -> list[dict[str, Any]]: ...


@dataclass
class MainAgentLoop:
    client: _Client
    decider: _Decider
    session_id: str
    plan_id: str
    poll_interval_seconds: float = 2.0
    _last_seen_iso: str | None = None
    _history: list[dict[str, Any]] | None = None
    _http_failures: int = 0
    _max_http_failures: int = 5
    _stop_requested: bool = False

    @classmethod
    def from_config(cls, cfg: MainAgentConfig) -> MainAgentLoop:
        client = _SessionClient(BridleBackendClient(cfg.backend_url), cfg.session_id)
        decider = DeepSeekDecider(cfg.api_key, cfg.model)
        return cls(client, decider, session_id=cfg.session_id, plan_id=cfg.plan_id)

    def install_signal_handlers(self) -> None:
        def _handle(_signum: int, _frame: object) -> None:
            self._stop_requested = True

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)

    def run_forever(self, *, max_iterations: int | None = None) -> int:
        iterations = 0
        while True:
            if self._stop_requested:
                try:
                    self.client.post_assistant(self.session_id, "main agent stopped")
                except Exception:
                    logger.exception("main_agent_stop_message_failed")
                return 0
            already_slept = False
            try:
                code = self.run_once()
                if code != 0:
                    return code
            except RuntimeError as exc:
                if str(exc).startswith("http_5"):
                    self._http_failures += 1
                    delay = min(16, 2 ** (self._http_failures - 1))
                    logger.warning(
                        "main_agent_http_backoff",
                        extra={"detail": {"failures": self._http_failures, "delay": delay}},
                    )
                    time.sleep(delay)
                    already_slept = True
                    if self._http_failures >= self._max_http_failures:
                        return 2
                else:
                    raise
            except httpx.HTTPStatusError as exc:
                # 后端返回业务冲突（4xx）。不要让一次决策失败杀掉整个 main-agent；
                # 写一条 assistant 消息上报，继续轮询等待下一次决策。
                status = exc.response.status_code
                body_snippet = exc.response.text[:500]
                logger.warning(
                    "main_agent_business_conflict",
                    extra={"detail": {"status": status, "body": body_snippet}},
                )
                try:
                    self.client.post_assistant(
                        self.session_id,
                        f"⚠️ Action 被后端拒绝 (HTTP {status})：{body_snippet}",
                    )
                except Exception:
                    logger.exception("main_agent_conflict_report_failed")
            except httpx.RequestError as exc:
                self._http_failures += 1
                delay = min(16, 2 ** (self._http_failures - 1))
                logger.warning(
                    "main_agent_transport_backoff",
                    extra={
                        "detail": {
                            "failures": self._http_failures,
                            "delay": delay,
                            "error": str(exc),
                        }
                    },
                )
                time.sleep(delay)
                already_slept = True
                if self._http_failures >= self._max_http_failures:
                    return 2
            else:
                self._http_failures = 0
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return 0
            if not already_slept:
                time.sleep(self.poll_interval_seconds)

    def run_once(self) -> int:
        session = self.client.get_session(self.session_id)
        status = str(session.get("status", ""))
        if status in _TERMINAL_SESSION:
            return 0

        msgs = self.client.poll_messages(self.session_id, created_after=self._last_seen_iso)
        self._update_last_seen(msgs)
        new_user = [m for m in msgs if m.get("role") == "user"]
        if new_user or self._idle_tick():
            snapshot = self.client.get_eligible_snapshot(self.session_id)
            eligible = list(snapshot.get("eligible_nodes") or [])
            blocked = list(snapshot.get("blocked_nodes") or [])
            if not eligible and blocked:
                complexity_blocked = [
                    b
                    for b in blocked
                    if b.get("reason") in {
                        "node_too_complex",
                        "node_too_granular",
                        "node_incomplete",
                        "node_blocked",
                    }
                ]
                if complexity_blocked:
                    if self._try_runtime_negotiation(complexity_blocked):
                        return 0
                    snapshot = self.client.get_eligible_snapshot(self.session_id)
                    eligible = list(snapshot.get("eligible_nodes") or [])
            plan = self.client.current_plan()
            failed_runs = self.client.get_recent_failed_runs(self.session_id, 3)
            decision = self.decider.decide(
                self._compose_history(),
                plan,
                eligible,
                failed_runs=failed_runs,
            )
            self._dispatch(decision)
        return 0

    def _idle_tick(self) -> bool:
        return False

    def _compose_history(self) -> list[dict[str, Any]]:
        if self._history is None:
            self._history = []
        return list(self._history)

    def _update_last_seen(self, msgs: list[dict[str, Any]]) -> None:
        if self._history is None:
            self._history = []
        for msg in msgs:
            self._history.append(msg)
            created = msg.get("created_at")
            if created:
                self._last_seen_iso = str(created)

    def _try_runtime_negotiation(self, complexity_blocked: list[dict[str, Any]]) -> bool:
        """Return True when this turn should stop (e.g. negotiation failed with 422)."""
        try:
            self.client.negotiate_complexity(self.plan_id)
            logger.info(
                "main_agent_runtime_negotiation_ok",
                extra={"detail": {"blocked_count": len(complexity_blocked)}},
            )
            return False
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 422:
                reason = exc.response.text[:500]
                try:
                    self.client.fail_session(self.session_id, reason=reason)
                except Exception:
                    logger.exception("main_agent_fail_session_failed")
                try:
                    self.client.post_assistant(
                        self.session_id,
                        f"plan 已无法自动调整，请人工介入：{reason}",
                    )
                except Exception:
                    logger.exception("main_agent_negotiation_report_failed")
                return True
            raise

    def _dispatch(self, decision: Decision) -> None:
        logger.info(
            "main_agent_dispatch",
            extra={
                "detail": {
                    "action": decision.action,
                    "node_id": decision.node_id,
                    "reply_len": len(decision.reply or ""),
                    "reason": decision.reason,
                }
            },
        )
        if decision.action == "select_node" and decision.node_id:
            self.client.select_node(self.session_id, decision.node_id)
            text = decision.reply or f"已派发节点 {decision.node_id}"
            self.client.post_assistant(self.session_id, text)
            return
        if decision.action == "reply":
            text = decision.reply or f"（模型未返回内容；reason={decision.reason or 'unknown'}）"
            self.client.post_assistant(self.session_id, text)
            return
        if decision.action == "done":
            self.client.post_assistant(self.session_id, decision.reply or "会话结束")
            return
        if decision.action == "wait":
            self.client.post_assistant(
                self.session_id,
                f"（等待中：{decision.reason or '无具体原因'}）",
            )
            return
        logger.warning(
            "main_agent_dispatch_unknown_action",
            extra={"detail": {"action": decision.action, "reason": decision.reason}},
        )
        self.client.post_assistant(
            self.session_id,
            f"⚠️ 模型返回了未识别的 action='{decision.action}'，已跳过本轮。",
        )


@dataclass
class _SessionClient:
    _inner: BridleBackendClient
    _session_id: str

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._inner.get_session(session_id)

    def poll_messages(self, session_id: str, *, created_after: str | None) -> list[dict[str, Any]]:
        return self._inner.poll_messages(session_id, created_after=created_after)

    def get_eligible_snapshot(self, session_id: str) -> dict[str, Any]:
        return self._inner.get_eligible_snapshot(session_id)

    def eligible_nodes(self, session_id: str) -> list[dict[str, Any]]:
        return self._inner.eligible_nodes(session_id)

    def negotiate_complexity(self, plan_id: str) -> dict[str, Any]:
        return self._inner.negotiate_complexity(plan_id)

    def fail_session(self, session_id: str, *, reason: str) -> dict[str, Any]:
        return self._inner.fail_session(session_id, reason=reason)

    def current_plan(self) -> dict[str, Any]:
        return self._inner.current_plan()

    def select_node(self, session_id: str, node_id: str) -> dict[str, Any]:
        return self._inner.select_node(session_id, node_id)

    def post_assistant(self, session_id: str, content: str) -> dict[str, Any]:
        return self._inner.post_assistant(session_id, content)

    def get_recent_failed_runs(self, session_id: str, limit: int = 3) -> list[dict[str, Any]]:
        return self._inner.get_recent_failed_runs(session_id, limit)

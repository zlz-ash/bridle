"""Multi-dimensional tool budget tracking for agent tool loops."""
from __future__ import annotations

import json
import os
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

# Bounds for the exceptional wall-clock watchdog. Normal termination is model-owned.
_DEFAULT_ESTIMATED_MINUTES = 60
_WALL_SECONDS_MIN = 60.0
_WALL_SECONDS_MAX = 1800.0
_WALL_SECONDS_PER_MINUTE = 12.0  # = 0.2 * 60 (LLM wall budget vs human estimate)


def budget_for_node_minutes(estimated_minutes: int | None) -> dict[str, float]:
    """Compute the exceptional wall-clock watchdog for a plan node.

    Returns a dict mutable by callers (e.g. AgentProviderFactory.create) without
    affecting subsequent calls. Always returns positive ints/floats within
    documented bounds.
    """
    est = max(int(estimated_minutes or _DEFAULT_ESTIMATED_MINUTES), 1)
    max_wall_seconds = min(
        _WALL_SECONDS_MAX,
        max(_WALL_SECONDS_MIN, float(est) * _WALL_SECONDS_PER_MINUTE),
    )
    budget: dict[str, float] = {"max_wall_seconds": float(max_wall_seconds)}
    # Env override (highest precedence)
    if (raw := os.getenv("BRIDLE_DEEPSEEK_MAX_WALL_SECONDS")):
        with suppress(ValueError):
            budget["max_wall_seconds"] = float(raw)
    return budget


@dataclass(frozen=True)
class ToolBudgetLimits:
    max_wall_seconds: float


@dataclass
class ToolBudgetUsage:
    rounds_used: int = 0
    tool_calls_used: int = 0
    wall_seconds_used: float = 0.0


def summarize_tool_args(args: dict[str, Any]) -> str:
    from bridle.agent.providers.deepseek_agent_provider import sanitize_model_response_text

    try:
        text = json.dumps(args, ensure_ascii=False, default=str)
    except TypeError:
        text = str(args)
    return sanitize_model_response_text(text[:500])


class ToolBudgetTracker:
    def __init__(
        self,
        limits: ToolBudgetLimits,
        *,
        start_time: float | None = None,
    ) -> None:
        self.limits = limits
        self._start = start_time if start_time is not None else time.monotonic()
        self.usage = ToolBudgetUsage()
        self.changed_files: list[str] = []
        self.last_test_result: dict[str, Any] | None = None
        self.last_tool_call: dict[str, Any] | None = None

    def sync_wall_clock(self) -> None:
        self.usage.wall_seconds_used = time.monotonic() - self._start

    def check_before_round(self) -> str | None:
        self.sync_wall_clock()
        if self.usage.wall_seconds_used >= self.limits.max_wall_seconds:
            return "wall_seconds"
        return None

    def begin_round(self) -> None:
        self.usage.rounds_used += 1

    def check_before_tool_call(self) -> str | None:
        self.sync_wall_clock()
        if self.usage.wall_seconds_used >= self.limits.max_wall_seconds:
            return "wall_seconds"
        return None

    def check_exhausted(self) -> str | None:
        return self.check_before_round() or self.check_before_tool_call()

    def record_tool_call(
        self,
        *,
        tool_name: str = "",
        args_summary: str = "",
    ) -> None:
        self.usage.tool_calls_used += 1
        self.last_tool_call = {
            "tool_name": tool_name,
            "args_summary": args_summary[:200],
        }

    def note_tool_result(self, tool_name: str, result: dict[str, Any]) -> None:
        if tool_name != "run_command":
            return
        target = result
        self.last_test_result = {
            "exit_code": target.get("exit_code"),
            "timed_out": bool(target.get("timed_out", False)),
            "policy_rejected": bool(target.get("policy_rejected", False)),
            "stdout_preview": str(target.get("stdout_preview", "") or "")[:200],
            "stderr_preview": str(target.get("stderr_preview", "") or "")[:200],
        }

    def build_exhausted_report(self, budget_type: str) -> dict[str, Any]:
        self.sync_wall_clock()
        return {
            "error_code": "tool_budget_exhausted",
            "budget": {
                "type": budget_type,
                "limits": {
                    "max_wall_seconds": self.limits.max_wall_seconds,
                },
                "used": {
                    "rounds_used": self.usage.rounds_used,
                    "tool_calls_used": self.usage.tool_calls_used,
                    "wall_seconds_used": round(self.usage.wall_seconds_used, 3),
                },
            },
            "changed_files": list(self.changed_files),
            "last_test_result": self.last_test_result,
            "last_tool_call": self.last_tool_call,
            "needs_replan": True,
            "suggested_split": [
                "types/schema",
                "core logic",
                "tests",
                "integration verification",
            ],
        }


"""Configuration defaults for agent coding orchestration."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CodingOrchestrationConfig:
    heartbeat_interval_seconds: int = 30
    stale_after_seconds: int = 90
    blocked_timeout_seconds: int = 300
    hard_timeout_seconds: int = 900
    max_attempts: int = 3
    default_auto_continue_budget: int = 1
    heartbeat_message_max_len: int = 500


CODING_CONFIG = CodingOrchestrationConfig()

ELIGIBLE_NODE_STATUSES = frozenset({
    "pending",
    "ready",
    "failed_retryable",
    "needs_review_retryable",
})

ACTIVE_RUN_STATUSES = frozenset({
    "queued",
    "running",
    "waiting_tool",
    "retrying",
    "blocked",
})

TERMINAL_RUN_STATUSES = frozenset({
    "completed",
    "failed",
    "timed_out",
    "cancelled",
})

HEARTBEAT_ALLOWED_STATUSES = frozenset({
    "running",
    "waiting_tool",
    "retrying",
    "blocked",
})

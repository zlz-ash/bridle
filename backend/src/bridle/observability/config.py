"""Observability configuration from environment."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ObservabilityConfig:
    enabled: bool
    provider: str
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_host: str

    @classmethod
    def from_env(cls) -> ObservabilityConfig:
        enabled_raw = os.getenv("BRIDLE_OBSERVABILITY_ENABLED", "").strip().lower()
        public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
        secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
        host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").strip().rstrip("/")

        has_langfuse_keys = bool(public_key and secret_key)
        enabled = enabled_raw in {"1", "true", "yes"} or has_langfuse_keys
        provider = "langfuse" if enabled and has_langfuse_keys else "noop"
        if enabled_raw in {"0", "false", "no"}:
            enabled = False
            provider = "noop"

        return cls(
            enabled=enabled,
            provider=provider,
            langfuse_public_key=public_key,
            langfuse_secret_key=secret_key,
            langfuse_host=host,
        )

    @classmethod
    def disabled(cls) -> ObservabilityConfig:
        return cls(
            enabled=False,
            provider="noop",
            langfuse_public_key="",
            langfuse_secret_key="",
            langfuse_host="",
        )

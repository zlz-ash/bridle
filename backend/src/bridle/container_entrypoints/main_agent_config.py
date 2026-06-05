"""Environment configuration for main-agent container."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class MainAgentConfig:
    session_id: str
    plan_id: str
    backend_url: str
    api_key: str
    model: str
    provider: str

    @classmethod
    def from_env(cls) -> MainAgentConfig:
        session_id = os.environ["BRIDLE_SESSION_ID"]
        plan_id = os.environ["BRIDLE_PLAN_ID"]
        backend_url = os.environ.get("BRIDLE_BACKEND_URL", "http://host.docker.internal:8900").rstrip("/")
        return cls(
            session_id=session_id,
            plan_id=plan_id,
            backend_url=backend_url,
            api_key=os.environ.get("BRIDLE_AGENT_API_KEY", ""),
            model=os.environ.get("BRIDLE_AGENT_MODEL", ""),
            provider=os.environ.get("BRIDLE_AGENT_PROVIDER", "deepseek"),
        )

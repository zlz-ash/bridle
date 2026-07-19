"""Agent provider abstraction, factory, and configured stub.

V1 constraints:
- Default provider is 'fake' -no network, deterministic output.
- ConfiguredStubProvider validates the config/timeout/error chain without real LLM.
- Proxy defaults to http://127.0.0.1:7890 per project rules.
- API keys are never logged.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from bridle.agent.runtime.schemas import AgentContext, AgentProposalSchema, FilePatchSchema

logger = logging.getLogger("bridle")


# ---------------------------------------------------------------------------
# AgentProvider protocol
# ---------------------------------------------------------------------------


class AgentProvider(Protocol):
    """Protocol that all agent providers must satisfy."""

    name: str

    async def generate(self, context: AgentContext) -> AgentProposalSchema:
        ...

    async def optimize_memory(self, summary: str, evicted: list[dict[str, Any]]) -> str:
        ...


# ---------------------------------------------------------------------------
# Fake provider (default, no network)
# ---------------------------------------------------------------------------


class FakeAgentProvider:
    """Default provider -deterministic, no I/O, no network.

    Implements AgentProvider protocol.
    """

    name = "fake"

    async def generate(self, context: AgentContext) -> AgentProposalSchema:
        instruction = context.instruction
        allowed_files = context.allowed_files
        accessible = context.accessible_context

        summary = self._build_summary(instruction, allowed_files, accessible)
        file_patches = self._build_file_patches(allowed_files, instruction)
        tests_to_run = self._build_tests(context.tests)

        return AgentProposalSchema(
            terminal_status="completed",
            reason="",
            summary=summary,
            file_patches=file_patches,
            tests_to_run=tests_to_run,
        )

    async def optimize_memory(self, summary: str, evicted: list[dict[str, Any]]) -> str:
        """Produce a deterministic local summary without tools or network I/O."""
        parts = [summary.strip()] if summary.strip() else []
        parts.extend(
            f"{item.get('role', 'unknown')}: {str(item.get('content', '')).strip()}"
            for item in evicted
            if str(item.get("content", "")).strip()
        )
        result = " | ".join(parts) or "No prior conversation retained"
        logger.info(
            "agent_memory_optimizer_completed",
            extra={
                "action": "agent_memory_optimizer_completed",
                "status": "completed",
                "detail": {"provider": self.name, "evicted_count": len(evicted)},
            },
        )
        return result

    @staticmethod
    def _build_summary(instruction: str, allowed_files: list[str], accessible: dict) -> str:
        file_list = ", ".join(allowed_files) if allowed_files else "no files"
        iface_nodes = [a.get("node_id", "") for a in accessible.get("accessible", []) if "error" not in a]
        iface_info = f" with interfaces from {', '.join(iface_nodes)}" if iface_nodes else ""
        return f"[DRY-RUN] Proposal for: {instruction}. Allowed files: [{file_list}]{iface_info}."

    @staticmethod
    def _build_file_patches(allowed_files: list[str], instruction: str) -> list[FilePatchSchema]:
        if not allowed_files:
            return []
        patches = []
        for path in allowed_files:
            digest = hashlib.sha256(f"{instruction}:{path}".encode()).hexdigest()[:16]
            stub_line = f"# [DRY-RUN stub diff {digest}]"
            diff = (
                "--- /dev/null\n"
                f"+++ b/{path}\n"
                "@@ -0,0 +1,1 @@\n"
                f"+{stub_line}\n"
            )
            patches.append(FilePatchSchema(path=path, change_type="add", diff=diff))
        return patches

    @staticmethod
    def _build_tests(node_tests: list[str]) -> list[str]:
        return [str(t) for t in node_tests if str(t).strip()]


# ---------------------------------------------------------------------------
# Configured stub provider (tests config/timeout/error chain)
# ---------------------------------------------------------------------------


class ConfiguredStubProvider:
    """Provider that validates the full configuration chain without real LLM.

    Accepts model, timeout, and proxy config. Produces deterministic output
    like FakeAgentProvider, but tagged with the configured model name.
    """

    name = "configured_stub"

    def __init__(self, model: str, timeout_seconds: int, proxy: str | None) -> None:
        self._model = model
        self._timeout = timeout_seconds
        self._proxy = proxy

    async def generate(self, context: AgentContext) -> AgentProposalSchema:
        start = time.monotonic()
        # Simulate async work respecting timeout
        await asyncio.sleep(0)
        elapsed = time.monotonic() - start
        if elapsed > self._timeout:
            raise TimeoutError(
                f"ConfiguredStubProvider timed out after {self._timeout}s "
                f"(elapsed: {elapsed:.3f}s)"
            )

        instruction = context.instruction
        allowed_files = context.allowed_files

        summary = f"[STUB:{self._model}] Proposal for: {instruction}. Files: {len(allowed_files)}."
        file_patches = []
        for path in allowed_files:
            digest = hashlib.sha256(f"stub:{self._model}:{instruction}:{path}".encode()).hexdigest()[:16]
            file_patches.append(FilePatchSchema(
                path=path,
                change_type="modify",
                diff=f"[STUB:{self._model} diff {digest}]",
            ))

        tests_to_run = [str(t) for t in context.tests if str(t).strip()]

        return AgentProposalSchema(
            terminal_status="completed",
            reason="",
            summary=summary,
            file_patches=file_patches,
            tests_to_run=tests_to_run,
        )

    async def optimize_memory(self, summary: str, evicted: list[dict[str, Any]]) -> str:
        result = await FakeAgentProvider().optimize_memory(summary, evicted)
        return f"[STUB:{self._model}] {result}"


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


class AgentProviderFactory:
    """Creates agent providers based on environment configuration.

    Env vars:
    - BRIDLE_AGENT_PROVIDER: provider name (default: "fake")
    - BRIDLE_AGENT_MODEL: model name (default: "unknown")
    - BRIDLE_AGENT_API_KEY: API key (required for non-fake providers)
    - BRIDLE_AGENT_TIMEOUT_SECONDS: timeout (default: 120)
    - HTTPS_PROXY: proxy URL (default: http://127.0.0.1:7890)
    - BRIDLE_DEEPSEEK_STRICT_TOOLS: use beta strict tools (default: false)
    - BRIDLE_DEEPSEEK_MAX_WALL_SECONDS: wall-clock budget per generate (default: 300)
    """

    DEFAULT_PROXY = "http://127.0.0.1:7890"
    DEFAULT_TIMEOUT = 120
    DEFAULT_MODEL = "unknown"
    DEFAULT_MAX_WALL_SECONDS = 300.0

    @staticmethod
    def create(
        context: AgentContext | None = None,
        *,
        budget_override: dict[str, float] | None = None,
        runtime_tool_handlers: dict[
            str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
        ] | None = None,
        test_backend: Any | None = None,
    ) -> AgentProvider:
        """Create a provider based on current environment config.

        Args:
            context: AgentContext for tool registry (deepseek path requires it).
            budget_override: Optional per-call wall-clock watchdog override.
        """
        cfg = AgentProviderFactory.get_config()
        provider_name = cfg["provider"]
        api_key = cfg["api_key"]
        model = cfg["model"]
        timeout = cfg["timeout_seconds"]
        proxy = cfg["proxy"]

        if provider_name == "fake":
            return FakeAgentProvider()

        # Non-fake provider requires API key
        if not api_key:
            logger.warning(
                "agent_provider_fallback",
                extra={
                    "action": "agent_provider_fallback",
                    "status": "fallback",
                    "detail": {
                        "configured_provider": provider_name,
                        "reason": "Missing BRIDLE_AGENT_API_KEY",
                    },
                },
            )
            return FakeAgentProvider()

        if provider_name == "configured_stub":
            return ConfiguredStubProvider(model=model, timeout_seconds=timeout, proxy=proxy)

        if provider_name == "deepseek" or provider_name == "openai_compatible":
            from bridle.agent.providers.deepseek_agent_provider import DeepSeekAgentProvider
            from bridle.agent.providers.deepseek_client import DEEPSEEK_BETA_BASE, DEEPSEEK_DEFAULT_BASE
            from bridle.agent.providers.openai_client import HttpOpenAICompatibleClient
            from bridle.agent.tools.registry import AgentToolRegistry

            if context is None:
                logger.warning(
                    "agent_provider_fallback",
                    extra={
                        "action": "agent_provider_fallback",
                        "status": "fallback",
                        "detail": {
                            "configured_provider": provider_name,
                            "reason": "Provider requires AgentContext for sandbox registry",
                        },
                    },
                )
                return FakeAgentProvider()

            if cfg["deepseek_strict_tools"]:
                base_url = cfg["beta_base_url"] or DEEPSEEK_BETA_BASE
            else:
                base_url = cfg["base_url"] or DEEPSEEK_DEFAULT_BASE
            registry = AgentToolRegistry.from_context(
                context,
                runtime_handlers=runtime_tool_handlers,
                test_backend=test_backend,
            )
            client = HttpOpenAICompatibleClient(api_key=api_key, base_url=base_url, proxy=proxy)
            snap = context.tool_capabilities.get("sandbox", {}) if context.tool_capabilities else {}
            override = budget_override or {}
            max_wall_seconds = float(override.get("max_wall_seconds", cfg["deepseek_max_wall_seconds"]))
            return DeepSeekAgentProvider(
                client=client,
                model=model,
                max_wall_seconds=max_wall_seconds,
                registry=registry,
                strict_tools=cfg["deepseek_strict_tools"],
                timeout_seconds=float(timeout),
                run_id=str(snap.get("run_id")),
                node_id=str(snap.get("node_id")),
            )

        # Unknown provider ->fallback to fake
        logger.warning(
            "agent_provider_fallback",
            extra={
                "action": "agent_provider_fallback",
                "status": "fallback",
                "detail": {
                    "configured_provider": provider_name,
                    "reason": f"Unknown provider '{provider_name}'",
                },
            },
        )
        return FakeAgentProvider()

    @staticmethod
    def create_memory_optimizer() -> Callable[[str, list[dict[str, Any]]], Awaitable[str]]:
        """Create the configured provider's tool-free memory optimizer entry."""
        cfg = AgentProviderFactory.get_config()
        provider_name = cfg["provider"]
        if provider_name == "configured_stub":
            return ConfiguredStubProvider(
                model=cfg["model"],
                timeout_seconds=cfg["timeout_seconds"],
                proxy=cfg["proxy"],
            ).optimize_memory
        if provider_name not in {"deepseek", "openai_compatible"} or not cfg["api_key"]:
            return FakeAgentProvider().optimize_memory

        from bridle.agent.providers.deepseek_agent_provider import DeepSeekAgentProvider
        from bridle.agent.providers.deepseek_client import DEEPSEEK_BETA_BASE, DEEPSEEK_DEFAULT_BASE
        from bridle.agent.providers.openai_client import HttpOpenAICompatibleClient

        base_url = (
            cfg["beta_base_url"] or DEEPSEEK_BETA_BASE
            if cfg["deepseek_strict_tools"]
            else cfg["base_url"] or DEEPSEEK_DEFAULT_BASE
        )
        client = HttpOpenAICompatibleClient(
            api_key=cfg["api_key"],
            base_url=base_url,
            proxy=cfg["proxy"],
        )
        return DeepSeekAgentProvider(
            client=client,
            model=cfg["model"],
            max_wall_seconds=cfg["deepseek_max_wall_seconds"],
            registry=None,
            strict_tools=cfg["deepseek_strict_tools"],
            timeout_seconds=float(cfg["timeout_seconds"]),
        ).optimize_memory

    @staticmethod
    def get_config() -> dict:
        """Read provider configuration from environment."""
        strict_raw = os.getenv("BRIDLE_DEEPSEEK_STRICT_TOOLS", "false").lower()
        return {
            "provider": os.getenv("BRIDLE_AGENT_PROVIDER", "fake"),
            "model": os.getenv("BRIDLE_AGENT_MODEL", AgentProviderFactory.DEFAULT_MODEL),
            "api_key": os.getenv("BRIDLE_AGENT_API_KEY", ""),
            "timeout_seconds": int(
                os.getenv("BRIDLE_AGENT_TIMEOUT_SECONDS", str(AgentProviderFactory.DEFAULT_TIMEOUT))
            ),
            "proxy": os.getenv("HTTPS_PROXY", AgentProviderFactory.DEFAULT_PROXY),
            "deepseek_strict_tools": strict_raw in ("1", "true", "yes"),
            "deepseek_max_wall_seconds": float(
                os.getenv(
                    "BRIDLE_DEEPSEEK_MAX_WALL_SECONDS",
                    str(AgentProviderFactory.DEFAULT_MAX_WALL_SECONDS),
                )
            ),
            "base_url": os.getenv("BRIDLE_AGENT_BASE_URL", ""),
            "beta_base_url": os.getenv("BRIDLE_AGENT_BETA_BASE_URL", ""),
        }


"""Agent provider abstraction, factory, and configured stub.

V1 constraints:
- Default provider is 'fake' — no network, deterministic output.
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
from typing import Protocol

from bridle.schemas.proposal import AgentContext, AgentProposalSchema, FilePatchSchema

logger = logging.getLogger("bridle")


# ---------------------------------------------------------------------------
# AgentProvider protocol
# ---------------------------------------------------------------------------


class AgentProvider(Protocol):
    """Protocol that all agent providers must satisfy."""

    name: str

    async def generate(self, context: AgentContext) -> AgentProposalSchema:
        ...


# ---------------------------------------------------------------------------
# Fake provider (default, no network)
# ---------------------------------------------------------------------------


class FakeAgentProvider:
    """Default provider — deterministic, no I/O, no network.

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
            summary=summary,
            file_patches=file_patches,
            tests_to_run=tests_to_run,
        )

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
            diff = (
                f"--- a/{path}\n+++ b/{path}\n"
                f"@@ -1,1 +1,1 @@\n"
                f" [DRY-RUN stub diff {digest}]\n"
            )
            patches.append(FilePatchSchema(path=path, change_type="modify", diff=diff))
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
            summary=summary,
            file_patches=file_patches,
            tests_to_run=tests_to_run,
        )


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
    - BRIDLE_DEEPSEEK_MAX_TOOL_ROUNDS: tool loop limit (default: 8)
    """

    DEFAULT_PROXY = "http://127.0.0.1:7890"
    DEFAULT_TIMEOUT = 120
    DEFAULT_MODEL = "unknown"
    DEFAULT_MAX_TOOL_ROUNDS = 8

    @staticmethod
    def create(context: AgentContext | None = None) -> AgentProvider:
        """Create a provider based on current environment config."""
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

        if provider_name == "deepseek":
            from bridle.engine.agent_tool_registry import AgentToolRegistry
            from bridle.engine.deepseek_agent_provider import DeepSeekAgentProvider
            from bridle.engine.deepseek_client import DEEPSEEK_BETA_BASE, DEEPSEEK_DEFAULT_BASE, HttpDeepSeekClient

            if context is None:
                logger.warning(
                    "agent_provider_fallback",
                    extra={
                        "action": "agent_provider_fallback",
                        "status": "fallback",
                        "detail": {
                            "configured_provider": provider_name,
                            "reason": "DeepSeek requires AgentContext for sandbox registry",
                        },
                    },
                )
                return FakeAgentProvider()

            base_url = DEEPSEEK_BETA_BASE if cfg["deepseek_strict_tools"] else DEEPSEEK_DEFAULT_BASE
            registry = AgentToolRegistry.from_context(context)
            client = HttpDeepSeekClient(api_key=api_key, base_url=base_url, proxy=proxy)
            snap = context.tool_capabilities.get("sandbox", {}) if context.tool_capabilities else {}
            return DeepSeekAgentProvider(
                client=client,
                model=model,
                max_tool_rounds=cfg["deepseek_max_tool_rounds"],
                registry=registry,
                strict_tools=cfg["deepseek_strict_tools"],
                timeout_seconds=float(timeout),
                run_id=str(snap.get("run_id")),
                node_id=str(snap.get("node_id")),
            )

        # Unknown provider → fallback to fake
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
    def get_config() -> dict:
        """Read provider configuration from environment."""
        strict_raw = os.getenv("BRIDLE_DEEPSEEK_STRICT_TOOLS", "false").lower()
        return {
            "provider": os.getenv("BRIDLE_AGENT_PROVIDER", "fake"),
            "model": os.getenv("BRIDLE_AGENT_MODEL", AgentProviderFactory.DEFAULT_MODEL),
            "api_key": os.getenv("BRIDLE_AGENT_API_KEY", ""),
            "timeout_seconds": int(os.getenv("BRIDLE_AGENT_TIMEOUT_SECONDS", str(AgentProviderFactory.DEFAULT_TIMEOUT))),
            "proxy": os.getenv("HTTPS_PROXY", AgentProviderFactory.DEFAULT_PROXY),
            "deepseek_strict_tools": strict_raw in ("1", "true", "yes"),
            "deepseek_max_tool_rounds": int(
                os.getenv("BRIDLE_DEEPSEEK_MAX_TOOL_ROUNDS", str(AgentProviderFactory.DEFAULT_MAX_TOOL_ROUNDS))
            ),
        }

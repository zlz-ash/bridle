"""Observability facade — business modules must use this entry point."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from bridle.observability.config import ObservabilityConfig
from bridle.observability.context import current_obs_context, obs_context_scope
from bridle.observability.langfuse_adapter import LangfuseObservabilityAdapter
from bridle.observability.noop_adapter import NoopObservabilityAdapter, NoopSpanHandle, NoopTraceHandle
from bridle.observability.schema import GenerationRecord, ObservabilityContext, ToolCallRecord

logger = logging.getLogger("bridle.observability")

_global_facade: ObservabilityFacade | None = None


def _build_adapter(config: ObservabilityConfig) -> NoopObservabilityAdapter | LangfuseObservabilityAdapter:
    if not config.enabled or config.provider == "noop":
        return NoopObservabilityAdapter()
    if config.provider == "langfuse":
        installed_version = "unknown"
        try:
            import langfuse as langfuse_module

            installed_version = str(getattr(langfuse_module, "__version__", "unknown"))
        except Exception:
            pass
        try:
            adapter = LangfuseObservabilityAdapter(config)
        except RuntimeError as exc:
            logger.warning(
                "observability_langfuse_init_failed",
                extra={
                    "detail": {
                        "provider": "langfuse",
                        "installed_version": installed_version,
                        "reason": str(exc),
                    },
                },
            )
            return NoopObservabilityAdapter()
        except Exception:
            logger.exception(
                "observability_langfuse_init_failed",
                extra={"detail": {"provider": "langfuse", "installed_version": installed_version}},
            )
            return NoopObservabilityAdapter()

        logger.info(
            "observability_langfuse_ready",
            extra={
                "detail": {
                    "provider": "langfuse",
                    "host": config.langfuse_host,
                    "installed_version": installed_version,
                },
            },
        )
        return adapter
    logger.warning("observability_unknown_provider", extra={"detail": {"provider": config.provider}})
    return NoopObservabilityAdapter()


class ObservabilityFacade:
    def __init__(
        self,
        config: ObservabilityConfig,
        *,
        adapter: NoopObservabilityAdapter | LangfuseObservabilityAdapter | None = None,
    ) -> None:
        self._config = config
        self._adapter = adapter if adapter is not None else _build_adapter(config)

    @property
    def adapter(self) -> NoopObservabilityAdapter | LangfuseObservabilityAdapter:
        return self._adapter

    def current_context(self) -> ObservabilityContext:
        return current_obs_context()

    @contextmanager
    def bind_context(self, ctx: ObservabilityContext) -> Iterator[None]:
        with obs_context_scope(ctx):
            yield

    def start_trace(self, name: str, **metadata: Any) -> NoopTraceHandle | Any:
        merged = {**self.current_context().to_metadata(), **metadata}
        try:
            return self._adapter.start_trace(name, **merged)
        except Exception:
            logger.exception(
                "observability_start_trace_failed",
                extra={"detail": {"name": name, "provider": "langfuse"}},
            )
            return NoopTraceHandle(name=name, metadata=merged)

    def start_span(self, name: str, **metadata: Any) -> NoopSpanHandle | Any:
        merged = {**self.current_context().to_metadata(), **metadata}
        try:
            return self._adapter.start_span(name, **merged)
        except Exception:
            logger.exception(
                "observability_start_span_failed",
                extra={"detail": {"name": name, "provider": "langfuse"}},
            )
            return NoopSpanHandle(name=name, metadata=merged)

    def record_generation(
        self,
        *,
        name: str = "model.generation",
        model: str,
        input_summary: dict[str, Any],
        output_summary: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        duration_ms: int | None = None,
        status: str = "completed",
        error_code: str | None = None,
        prompt_lineage: Any | None = None,
    ) -> None:
        record = GenerationRecord(
            name=name,
            model=model,
            input_summary=input_summary,
            output_summary=output_summary,
            metadata={**self.current_context().to_metadata(), **(metadata or {})},
            usage=usage or {},
            duration_ms=duration_ms,
            status=status,
            error_code=error_code,
            prompt_lineage=prompt_lineage,
        )
        try:
            self._adapter.record_generation(record)
        except Exception:
            logger.exception(
                "observability_record_generation_failed",
                extra={"detail": {"provider": "langfuse"}},
            )

    def record_tool_call(
        self,
        *,
        tool_name: str,
        input_summary: dict[str, Any],
        output_summary: dict[str, Any],
        duration_ms: int | None = None,
        status: str = "completed",
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        record = ToolCallRecord(
            tool_name=tool_name,
            input_summary=input_summary,
            output_summary=output_summary,
            duration_ms=duration_ms,
            status=status,
            error_code=error_code,
            metadata={**self.current_context().to_metadata(), **(metadata or {})},
        )
        try:
            self._adapter.record_tool_call(record)
        except Exception:
            logger.exception(
                "observability_record_tool_call_failed",
                extra={"detail": {"provider": "langfuse"}},
            )

    def flush(self) -> None:
        try:
            self._adapter.flush()
        except Exception:
            logger.exception(
                "observability_flush_failed",
                extra={"detail": {"provider": "langfuse"}},
            )


def get_observability() -> ObservabilityFacade:
    global _global_facade
    if _global_facade is None:
        _global_facade = ObservabilityFacade(ObservabilityConfig.from_env())
    return _global_facade


def reset_observability() -> None:
    global _global_facade
    _global_facade = None

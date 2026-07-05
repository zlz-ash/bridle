"""Langfuse v4 observability adapter — sole Langfuse SDK import site.

Design notes (v4 / OpenTelemetry-based SDK):

* Root observations are created via ``client.start_observation(...)``.
* Child observations are created via the **parent handle**'s
  ``.start_observation(...)``; we do NOT use ``start_as_current_observation``
  because its context-manager / OTel-context-token semantics do not survive
  cross-task or cross-thread ``end()`` calls (Bridle's SSE pipeline switches
  asyncio tasks between ``start_trace`` and ``handle.end``).
* The explicit parent chain is propagated through a project-owned ContextVar
  (``current_active_langfuse_trace``), not through the SDK's OTel auto-context.
* ``Observation.end()`` in v4 takes only ``end_time``; status / error_code are
  carried via ``update(metadata=...)`` immediately before ``end()``.
* ``__init__`` validates SDK method presence both on the client and on a
  short-lived probe observation. v4 spans are buffered locally and only emitted
  on ``flush()``, so the probe opens no network socket.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from langfuse import Langfuse  # module-level import: tests monkeypatch this name

from bridle.observability.config import ObservabilityConfig
from bridle.observability.context import (
    clear_active_langfuse_trace_if,
    current_active_langfuse_trace,
    set_active_langfuse_trace,
)
from bridle.observability.schema import GenerationRecord, ToolCallRecord

logger = logging.getLogger("bridle.observability")

_REQUIRED_CLIENT_METHODS = ("start_observation", "flush")
_REQUIRED_OBSERVATION_METHODS = ("start_observation", "update", "end", "create_event")
_PROBE_NAME = "_bridle_sdk_probe_"

# Langfuse v4 reads trace-level fields (session.id / user.id / langfuse.trace.name
# / langfuse.trace.tags) from OTel span attributes on the ROOT span, not from
# the observation metadata blob. The Sessions view, Trace Name column, and user
# aggregation are all driven by these attributes. The SDK only exposes a public
# setter via the ``*_current_*`` family, which depends on OTel current context
# — we run with detached observations on purpose, so we touch ``_otel_span``
# directly. This is the documented escape hatch for parent-chain users.
_TRACE_ATTR_SESSION_ID = "session.id"
_TRACE_ATTR_USER_ID = "user.id"
_TRACE_ATTR_TRACE_NAME = "langfuse.trace.name"
_TRACE_ATTR_TRACE_TAGS = "langfuse.trace.tags"


def _drop_internal_probe_spans(span: Any) -> bool:
    """Langfuse ``should_export_span`` callback — keep all non-probe spans.

    The adapter's ``__init__`` starts a short-lived probe observation named
    ``_bridle_sdk_probe_`` to validate the SDK without making network calls.
    The OTel exporter still picks it up and would surface it in the trace
    list, so we drop it at export time.
    """
    name = getattr(span, "name", None) or ""
    return not name.startswith(_PROBE_NAME)


def _apply_trace_attributes(
    obs: Any, *, meta: dict[str, Any], default_trace_name: str
) -> None:
    """Promote langfuse trace-level fields from ``meta`` to OTel span attrs.

    Safe on fakes / SDKs that don't expose ``_otel_span`` — early-returns.
    """
    otel = getattr(obs, "_otel_span", None)
    set_attribute = getattr(otel, "set_attribute", None)
    if not callable(set_attribute):
        return
    session_id = meta.get("session_id")
    if session_id:
        set_attribute(_TRACE_ATTR_SESSION_ID, str(session_id))
    user_id = meta.get("user_id")
    if user_id:
        set_attribute(_TRACE_ATTR_USER_ID, str(user_id))
    trace_name = meta.get("trace_name") or default_trace_name
    if trace_name:
        set_attribute(_TRACE_ATTR_TRACE_NAME, str(trace_name))
    tags = meta.get("tags")
    if isinstance(tags, (list, tuple)) and tags:
        set_attribute(_TRACE_ATTR_TRACE_TAGS, list(tags))


@dataclass
class LangfuseTraceHandle:
    _trace: Any  # v4 root observation handle (LangfuseSpan)
    name: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def trace_id(self) -> str:
        # v4 LangfuseSpan exposes ``trace_id`` directly; fall back to ``id``
        # then a literal so callers always get a non-empty string.
        return (
            getattr(self._trace, "trace_id", None)
            or str(getattr(self._trace, "id", ""))
            or "langfuse"
        )

    def start_span(self, name: str, **metadata: Any) -> LangfuseSpanHandle:
        merged = {**self.metadata, **metadata}
        span = self._trace.start_observation(as_type="span", name=name, metadata=merged)
        return LangfuseSpanHandle(_span=span, name=name, metadata=merged)

    def end(self, *, status: str = "completed", error_code: str | None = None) -> None:
        # v4 ``end()`` does not accept metadata; status/error_code must go via
        # ``update`` first. Always clear the ContextVar even if SDK calls raise.
        try:
            self._trace.update(metadata={"status": status, "error_code": error_code})
            self._trace.end()
        finally:
            clear_active_langfuse_trace_if(self._trace)


@dataclass
class LangfuseSpanHandle:
    _span: Any
    name: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def span_id(self) -> str:
        return str(getattr(self._span, "id", "") or "langfuse-span")

    def end(self, *, status: str = "completed", error_code: str | None = None) -> None:
        self._span.update(metadata={"status": status, "error_code": error_code})
        self._span.end()


class LangfuseObservabilityAdapter:
    def __init__(self, config: ObservabilityConfig) -> None:
        # ``should_export_span`` drops internal SDK probe spans before they
        # leave the process so the user-facing Langfuse UI never shows the
        # ``_bridle_sdk_probe_`` noise from adapter ``__init__``.
        self._client = Langfuse(
            public_key=config.langfuse_public_key,
            secret_key=config.langfuse_secret_key,
            host=config.langfuse_host,
            should_export_span=_drop_internal_probe_spans,
        )
        self._probe()

    def _probe(self) -> None:
        """Fail-fast SDK compatibility check. No network calls."""
        for name in _REQUIRED_CLIENT_METHODS:
            if not callable(getattr(self._client, name, None)):
                raise RuntimeError(f"langfuse SDK incompatible: missing {name}")
        probe = self._client.start_observation(as_type="span", name=_PROBE_NAME)
        try:
            for name in _REQUIRED_OBSERVATION_METHODS:
                if not callable(getattr(probe, name, None)):
                    raise RuntimeError(
                        f"langfuse SDK incompatible: missing observation.{name}"
                    )
        finally:
            end_fn = getattr(probe, "end", None)
            if callable(end_fn):
                try:
                    end_fn()
                except Exception:
                    # Swallow probe teardown errors — the real incompatibility
                    # above (if any) takes precedence.
                    pass

    def _ensure_root(
        self, *, fallback_name: str, metadata: dict[str, Any] | None
    ) -> Any:
        active = current_active_langfuse_trace()
        if active is not None:
            return active
        meta = dict(metadata or {})
        root = self._client.start_observation(
            as_type="span", name=fallback_name, metadata=meta
        )
        _apply_trace_attributes(root, meta=meta, default_trace_name=fallback_name)
        set_active_langfuse_trace(root)
        return root

    def start_trace(self, name: str, **metadata: Any) -> LangfuseTraceHandle:
        meta = dict(metadata)
        root = self._client.start_observation(
            as_type="span", name=name, metadata=meta
        )
        _apply_trace_attributes(root, meta=meta, default_trace_name=name)
        set_active_langfuse_trace(root)
        return LangfuseTraceHandle(_trace=root, name=name, metadata=meta)

    def start_span(self, name: str, **metadata: Any) -> LangfuseSpanHandle:
        meta = dict(metadata)
        parent = self._ensure_root(fallback_name="orphan", metadata=meta)
        span = parent.start_observation(as_type="span", name=name, metadata=meta)
        return LangfuseSpanHandle(_span=span, name=name, metadata=meta)

    def record_generation(self, record: GenerationRecord) -> None:
        parent = self._ensure_root(
            fallback_name=record.name, metadata=dict(record.metadata)
        )
        metadata = dict(record.metadata)
        metadata.update(
            {
                "usage": record.usage,
                "duration_ms": record.duration_ms,
                "status": record.status,
                "error_code": record.error_code,
            }
        )
        if record.prompt_lineage is not None:
            metadata.update(record.prompt_lineage.to_metadata())
        gen = parent.start_observation(
            as_type="generation",
            name=record.name,
            model=record.model,
            input=record.input_summary,
            output=record.output_summary,
            metadata=metadata,
        )
        gen.end()

    def record_tool_call(self, record: ToolCallRecord) -> None:
        parent = self._ensure_root(
            fallback_name=f"tool.{record.tool_name}",
            metadata=dict(record.metadata),
        )
        span = parent.start_observation(
            as_type="span",
            name=f"tool.{record.tool_name}",
            metadata=dict(record.metadata),
        )
        # Order: create_event → update → end. v4 ``end()`` takes no metadata,
        # so status/error_code must be set via update() before end().
        span.create_event(
            name="tool.result",
            metadata={
                "input_summary": record.input_summary,
                "output_summary": record.output_summary,
                "duration_ms": record.duration_ms,
                "status": record.status,
                "error_code": record.error_code,
            },
        )
        span.update(
            metadata={"status": record.status, "error_code": record.error_code}
        )
        span.end()

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception:
            logger.exception("langfuse_flush_failed")

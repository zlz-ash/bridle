"""Context propagation for observability and logging."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from bridle.observability.schema import ObservabilityContext

_obs_context: ContextVar[ObservabilityContext | None] = ContextVar("obs_context", default=None)
_log_context: ContextVar[dict[str, object] | None] = ContextVar("log_context", default=None)
_active_langfuse_trace: ContextVar[Any | None] = ContextVar("active_langfuse_trace", default=None)


def current_obs_context() -> ObservabilityContext:
    return _obs_context.get() or ObservabilityContext()


def set_obs_context(ctx: ObservabilityContext) -> None:
    _obs_context.set(ctx)


def reset_obs_context() -> None:
    _obs_context.set(None)


@contextmanager
def obs_context_scope(ctx: ObservabilityContext) -> Iterator[None]:
    token = _obs_context.set(ctx)
    try:
        yield
    finally:
        _obs_context.reset(token)


def bind_log_context(**fields: object) -> None:
    current = dict(_log_context.get() or {})
    current.update(fields)
    _log_context.set(current)


def current_log_context() -> dict[str, object]:
    obs = current_obs_context()
    merged: dict[str, object] = {}
    merged.update(obs.to_metadata())
    merged.update(_log_context.get() or {})
    return merged


def reset_log_context() -> None:
    _log_context.set(None)


def set_active_langfuse_trace(trace: Any | None) -> None:
    _active_langfuse_trace.set(trace)


def current_active_langfuse_trace() -> Any | None:
    return _active_langfuse_trace.get()


def clear_active_langfuse_trace_if(trace: Any) -> None:
    if current_active_langfuse_trace() is trace:
        _active_langfuse_trace.set(None)


@contextmanager
def active_langfuse_trace_scope(trace: Any) -> Iterator[None]:
    token = _active_langfuse_trace.set(trace)
    try:
        yield
    finally:
        _active_langfuse_trace.reset(token)

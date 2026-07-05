"""Logging facade — business modules should emit events through here."""
from __future__ import annotations

import logging
from typing import Any

from bridle.logging.jsonl_sink import JsonlLogSink
from bridle.logging.schema import LogEvent, LogLevel
from bridle.logging.sinks import LogSink
from bridle.logging.stdout_sink import StdoutLogSink
from bridle.observability.context import current_log_context

_global_facade: LoggingFacade | None = None


class LoggingFacade:
    def __init__(self, *, sinks: list[LogSink] | None = None) -> None:
        self._sinks = sinks if sinks is not None else [JsonlLogSink(), StdoutLogSink()]

    def emit(self, event: LogEvent) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:
                logging.getLogger("bridle.logging").exception(
                    "log_sink_emit_failed",
                    extra={"detail": {"action": event.action}},
                )

    def _build_event(
        self,
        action: str,
        status: str,
        level: LogLevel,
        *,
        detail: dict[str, Any] | None = None,
        **fields: Any,
    ) -> LogEvent:
        ctx = current_log_context()
        merged = {**ctx, **fields}
        return LogEvent(
            action=action,
            status=status,
            level=level,
            session_id=_as_str(merged.get("session_id")),
            run_id=_as_str(merged.get("run_id")),
            node_id=_as_str(merged.get("node_id")),
            plan_id=_as_str(merged.get("plan_id")),
            proposal_id=_as_str(merged.get("proposal_id")),
            provider=_as_str(merged.get("provider")),
            model=_as_str(merged.get("model")),
            phase=_as_str(merged.get("phase")),
            run_mode=_as_str(merged.get("run_mode")),
            workspace=_as_str(merged.get("workspace")),
            tool_name=_as_str(merged.get("tool_name")),
            prompt_name=_as_str(merged.get("prompt_name")),
            prompt_version=_as_str(merged.get("prompt_version")),
            error_code=_as_str(merged.get("error_code")),
            duration_ms=_as_int(merged.get("duration_ms")),
            exit_code=_as_int(merged.get("exit_code")),
            timed_out=_as_bool(merged.get("timed_out")),
            detail=dict(detail or {}),
        )

    def info_event(self, action: str, status: str, *, detail: dict[str, Any] | None = None, **fields: Any) -> None:
        self.emit(self._build_event(action, status, LogLevel.INFO, detail=detail, **fields))

    def warn_event(self, action: str, status: str, *, detail: dict[str, Any] | None = None, **fields: Any) -> None:
        self.emit(self._build_event(action, status, LogLevel.WARNING, detail=detail, **fields))

    def error_event(self, action: str, status: str, *, detail: dict[str, Any] | None = None, **fields: Any) -> None:
        self.emit(self._build_event(action, status, LogLevel.ERROR, detail=detail, **fields))


def _as_str(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _as_int(value: object | None) -> int | None:
    if value is None:
        return None
    return int(value)


def _as_bool(value: object | None) -> bool | None:
    if value is None:
        return None
    return bool(value)


def get_logging_facade() -> LoggingFacade:
    global _global_facade
    if _global_facade is None:
        _global_facade = LoggingFacade(sinks=[JsonlLogSink()])
    return _global_facade


def emit_event(
    action: str,
    status: str,
    *,
    task_id: str | None = None,
    node_id: str | None = None,
    run_id: str | None = None,
    duration_ms: int | None = None,
    detail: dict | None = None,
    facade: LoggingFacade | None = None,
) -> None:
    target = facade or get_logging_facade()
    merged_detail = dict(detail or {})
    if task_id is not None:
        merged_detail["task_id"] = task_id
    target.info_event(
        action,
        status,
        node_id=node_id,
        run_id=run_id,
        duration_ms=duration_ms,
        detail=merged_detail,
    )


def reset_logging_facade() -> None:
    global _global_facade
    _global_facade = None

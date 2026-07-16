from __future__ import annotations

import json
import logging
from io import StringIO

from bridle.logging.facade import LoggingFacade
from bridle.logging.jsonl import JSONLFormatter
from bridle.logging.jsonl_sink import JsonlLogSink
from bridle.logging.loki_sink import LokiLogSink
from bridle.logging.schema import LogEvent
from bridle.logging.stdout_sink import StdoutLogSink
from bridle.observability.context import bind_log_context, reset_log_context

RUNTIME_CORRELATION = {
    "trace_id": "trace-1",
    "message_id": "message-1",
    "project_id": "project-1",
    "agent_id": "agent-1",
    "generation": 7,
}
RUNTIME_KEYS = frozenset(RUNTIME_CORRELATION)


class CapturingSink:
    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    def emit(self, event: LogEvent) -> None:
        self.events.append(event)


class FailingSink:
    def emit(self, event: LogEvent) -> None:
        raise RuntimeError("secret source body")


def _assert_runtime_fields(payload: dict[str, object]) -> None:
    for key, expected in RUNTIME_CORRELATION.items():
        assert payload[key] == expected
    assert isinstance(payload["generation"], int)


def test_log_event_and_facade_preserve_runtime_correlation_fields() -> None:
    direct = LogEvent(action="runtime.direct", status="completed", **RUNTIME_CORRELATION)
    _assert_runtime_fields(direct.to_dict())
    assert RUNTIME_KEYS.isdisjoint(LogEvent(action="runtime.empty", status="completed").to_dict())

    explicit_sink = CapturingSink()
    LoggingFacade(sinks=[explicit_sink]).info_event(
        "runtime.explicit",
        "completed",
        **RUNTIME_CORRELATION,
    )
    _assert_runtime_fields(explicit_sink.events[0].to_dict())

    contextual_sink = CapturingSink()
    bind_log_context(**RUNTIME_CORRELATION)
    try:
        LoggingFacade(sinks=[contextual_sink]).info_event("runtime.context", "completed")
    finally:
        reset_log_context()
    _assert_runtime_fields(contextual_sink.events[0].to_dict())


def test_explicit_fields_override_runtime_correlation_context() -> None:
    sink = CapturingSink()
    bind_log_context(
        trace_id="context-trace",
        message_id="context-message",
        project_id="context-project",
        agent_id="context-agent",
        generation=1,
    )
    try:
        LoggingFacade(sinks=[sink]).info_event(
            "runtime.override",
            "completed",
            trace_id="explicit-trace",
            agent_id="explicit-agent",
            generation=9,
        )
    finally:
        reset_log_context()

    payload = sink.events[0].to_dict()
    assert payload["trace_id"] == "explicit-trace"
    assert payload["agent_id"] == "explicit-agent"
    assert payload["generation"] == 9
    assert payload["message_id"] == "context-message"
    assert payload["project_id"] == "context-project"


def test_stdout_sink_emits_runtime_correlation_fields() -> None:
    stream = StringIO()
    sink = StdoutLogSink(stream=stream)

    sink.emit(LogEvent(action="runtime.stdout", status="completed", **RUNTIME_CORRELATION))
    sink.emit(LogEvent(action="runtime.stdout.empty", status="completed"))

    populated, empty = stream.getvalue().splitlines()
    assert populated.startswith("[INFO] runtime.stdout status=completed ")
    for key, expected in RUNTIME_CORRELATION.items():
        assert f"{key}={expected}" in populated
    for key in RUNTIME_KEYS:
        assert f"{key}=" not in empty


def test_jsonl_logger_and_formatter_emit_runtime_correlation_fields(monkeypatch) -> None:
    stream = StringIO()
    logger = logging.Logger("runtime-jsonl", level=logging.INFO)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONLFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    monkeypatch.setattr("bridle.logging.jsonl.get_jsonl_logger", lambda: logger)
    sink = JsonlLogSink(use_logger=True)

    sink.emit(LogEvent(action="runtime.jsonl", status="completed", **RUNTIME_CORRELATION))
    sink.emit(LogEvent(action="runtime.jsonl.empty", status="completed"))

    populated, empty = [json.loads(line) for line in stream.getvalue().splitlines()]
    _assert_runtime_fields(populated)
    assert populated["action"] == "runtime.jsonl"
    assert populated["status"] == "completed"
    assert {"timestamp", "level", "logger"} <= populated.keys()
    assert RUNTIME_KEYS.isdisjoint(empty)


def test_loki_sink_log_record_contains_runtime_correlation_fields(caplog) -> None:
    sink = LokiLogSink(endpoint="http://127.0.0.1:3100", enabled=True)

    with caplog.at_level(logging.DEBUG, logger="bridle.logging.loki"):
        sink.emit(LogEvent(action="runtime.loki", status="completed", **RUNTIME_CORRELATION))
        sink.emit(LogEvent(action="runtime.loki.empty", status="completed"))

    events = [record.detail["event"] for record in caplog.records if record.msg == "loki_sink_emit"]
    assert len(events) == 2
    _assert_runtime_fields(events[0])
    assert RUNTIME_KEYS.isdisjoint(events[1])


def test_sink_failure_preserves_dispatch_and_business_result(caplog) -> None:
    captured = CapturingSink()
    facade = LoggingFacade(sinks=[FailingSink(), captured])

    def business_operation() -> str:
        facade.info_event(
            "runtime.business",
            "completed",
            detail={"safe": True},
            **RUNTIME_CORRELATION,
        )
        return "business-result"

    with caplog.at_level(logging.ERROR, logger="bridle.logging"):
        result = business_operation()

    assert result == "business-result"
    assert len(captured.events) == 1
    _assert_runtime_fields(captured.events[0].to_dict())
    assert "secret source body" not in caplog.text
    assert all(getattr(record, "detail", {}) == {"action": "runtime.business"} for record in caplog.records)

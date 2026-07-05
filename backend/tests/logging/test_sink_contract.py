"""Sink interface contract tests."""
from __future__ import annotations

import inspect

from bridle.logging.loki_sink import LokiLogSink
from bridle.logging.schema import LogEvent, LogLevel
from bridle.logging.sinks import LogSink


class TestSinkContract:
    def test_loki_sink_implements_protocol(self) -> None:
        sink = LokiLogSink(endpoint="http://127.0.0.1:3100", enabled=False)
        assert isinstance(sink, LogSink)
        sig = inspect.signature(sink.emit)
        assert "event" in sig.parameters

    def test_loki_sink_noop_when_disabled(self) -> None:
        sink = LokiLogSink(endpoint="http://127.0.0.1:3100", enabled=False)
        sink.emit(LogEvent(action="test.event", status="ok", level=LogLevel.INFO))

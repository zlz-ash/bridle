"""Tests for logging facade and sinks."""
from __future__ import annotations

import json
from io import StringIO

from bridle.logging.facade import LoggingFacade, emit_event
from bridle.logging.jsonl_sink import JsonlLogSink
from bridle.logging.schema import LogEvent, LogLevel
from bridle.logging.stdout_sink import StdoutLogSink
from bridle.observability.context import bind_log_context, reset_log_context


class TestLoggingFacade:
    def test_emit_structured_event(self) -> None:
        buf = StringIO()
        facade = LoggingFacade(sinks=[JsonlLogSink(stream=buf)])
        bind_log_context(session_id="s1", run_id="r1")
        try:
            facade.info_event("node_agent.run", "started", error_code=None)
        finally:
            reset_log_context()

        payload = json.loads(buf.getvalue().strip())
        assert payload["action"] == "node_agent.run"
        assert payload["status"] == "started"
        assert payload["session_id"] == "s1"
        assert payload["run_id"] == "r1"
        assert payload["level"] == "INFO"

    def test_log_event_compat_wrapper(self) -> None:
        buf = StringIO()
        facade = LoggingFacade(sinks=[JsonlLogSink(stream=buf)])
        emit_event(
            "sandbox_tool_completed",
            "completed",
            run_id="r1",
            node_id="n1",
            duration_ms=12,
            detail={"tool_name": "read"},
            facade=facade,
        )
        payload = json.loads(buf.getvalue().strip())
        assert payload["run_id"] == "r1"
        assert payload["node_id"] == "n1"
        assert payload["duration_ms"] == 12
        assert payload["detail"]["tool_name"] == "read"

    def test_stdout_sink_receives_event(self) -> None:
        buf = StringIO()
        sink = StdoutLogSink(stream=buf)
        sink.emit(
            LogEvent(
                action="main_agent.decision_round",
                status="completed",
                level=LogLevel.INFO,
                session_id="s1",
            )
        )
        line = buf.getvalue().strip()
        assert "main_agent.decision_round" in line
        assert "s1" in line


class TestLogEventSchema:
    def test_to_dict_includes_standard_fields(self) -> None:
        event = LogEvent(
            action="tool.run",
            status="failed",
            level=LogLevel.ERROR,
            session_id="s1",
            run_id="r1",
            node_id="n1",
            plan_id="p1",
            error_code="timeout",
            duration_ms=100,
        )
        data = event.to_dict()
        assert data["action"] == "tool.run"
        assert data["error_code"] == "timeout"
        assert data["plan_id"] == "p1"

"""Main-agent container logging setup."""
from __future__ import annotations

import io
import json
import logging

import pytest

from bridle.container_entrypoints.main_agent import configure_main_agent_logging
from bridle.logging.jsonl import JSONLFormatter


@pytest.fixture
def clean_root_logging():
    root = logging.getLogger()
    bridle = logging.getLogger("bridle")
    saved = (
        root.handlers[:],
        root.level,
        bridle.handlers[:],
        bridle.propagate,
        bridle.level,
    )
    root.handlers.clear()
    bridle.handlers.clear()
    bridle.propagate = True
    bridle.setLevel(logging.NOTSET)
    yield
    root.handlers.clear()
    root.handlers.extend(saved[0])
    root.setLevel(saved[1])
    bridle.handlers.clear()
    bridle.handlers.extend(saved[2])
    bridle.propagate = saved[3]
    bridle.setLevel(saved[4])


class TestMainAgentLogging:
    def test_jsonl_formatter_includes_dispatch_detail(self) -> None:
        formatter = JSONLFormatter()
        record = logging.LogRecord(
            name="bridle",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="main_agent_dispatch",
            args=(),
            exc_info=None,
        )
        record.detail = {
            "action": "select_node",
            "node_id": "uuid-1",
            "reply_len": 0,
            "reason": "",
        }
        payload = json.loads(formatter.format(record))
        assert payload["action"] == "main_agent_dispatch"
        assert payload["detail"]["action"] == "select_node"
        assert payload["detail"]["node_id"] == "uuid-1"

    def test_configure_logging_adds_jsonl_handler(self, clean_root_logging) -> None:
        stream = io.StringIO()
        configure_main_agent_logging(stream=stream)
        root = logging.getLogger()
        jsonl_handlers = [
            h for h in root.handlers if isinstance(getattr(h, "formatter", None), JSONLFormatter)
        ]
        assert len(jsonl_handlers) == 1

    def test_configure_logging_is_idempotent(self, clean_root_logging) -> None:
        stream = io.StringIO()
        configure_main_agent_logging(stream=stream)
        jsonl_count = sum(
            1
            for h in logging.getLogger().handlers
            if isinstance(getattr(h, "formatter", None), JSONLFormatter)
        )
        configure_main_agent_logging(stream=stream)
        assert (
            sum(
                1
                for h in logging.getLogger().handlers
                if isinstance(getattr(h, "formatter", None), JSONLFormatter)
            )
            == jsonl_count
        )

    def test_main_agent_logs_emit_valid_jsonl(self, clean_root_logging) -> None:
        stream = io.StringIO()
        configure_main_agent_logging(stream=stream)
        logging.getLogger("bridle").info(
            "main_agent_dispatch",
            extra={
                "detail": {
                    "action": "select_node",
                    "node_id": "uuid-1",
                    "reply_len": 12,
                    "reason": "ready",
                }
            },
        )
        lines = [line for line in stream.getvalue().splitlines() if line.strip()]
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["detail"]["node_id"] == "uuid-1"

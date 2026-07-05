"""JSONL logging sink."""
from __future__ import annotations

import json
from typing import TextIO

from bridle.logging.schema import LogEvent


class JsonlLogSink:
    def __init__(self, stream: TextIO | None = None, *, use_logger: bool | None = None) -> None:
        import sys

        self._stream = stream
        self._use_logger = use_logger if use_logger is not None else stream is None
        self._fallback_stream = stream or sys.stderr

    def emit(self, event: LogEvent) -> None:
        if self._use_logger:
            from bridle.logging.jsonl import get_jsonl_logger

            logger = get_jsonl_logger()
            logger.info(
                event.action,
                extra={
                    "action": event.action,
                    "status": event.status,
                    "node_id": event.node_id,
                    "run_id": event.run_id,
                    "duration_ms": event.duration_ms,
                    "detail": event.detail or None,
                },
            )
            return
        self._fallback_stream.write(
            json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n"
        )
        self._fallback_stream.flush()

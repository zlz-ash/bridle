"""Stdout logging sink."""
from __future__ import annotations

import json
from typing import TextIO

from bridle.logging.schema import LogEvent


class StdoutLogSink:
    def __init__(self, stream: TextIO | None = None) -> None:
        import sys

        self._stream = stream or sys.stdout

    def emit(self, event: LogEvent) -> None:
        payload = event.to_dict()
        self._stream.write(
            f"[{payload['level']}] {payload['action']} status={payload['status']} "
            f"session_id={payload.get('session_id')} run_id={payload.get('run_id')} "
            f"detail={json.dumps(payload.get('detail') or {}, ensure_ascii=False, default=str)}\n"
        )
        self._stream.flush()

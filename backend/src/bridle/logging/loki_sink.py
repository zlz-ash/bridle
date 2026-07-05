"""Loki sink stub — future remote logging backend."""
from __future__ import annotations

import logging

from bridle.logging.schema import LogEvent

logger = logging.getLogger("bridle.logging.loki")


class LokiLogSink:
    def __init__(self, *, endpoint: str, enabled: bool = False) -> None:
        self._endpoint = endpoint
        self._enabled = enabled

    def emit(self, event: LogEvent) -> None:
        if not self._enabled:
            return
        logger.debug(
            "loki_sink_emit",
            extra={"detail": {"endpoint": self._endpoint, "event": event.to_dict()}},
        )

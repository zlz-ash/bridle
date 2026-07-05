"""Logging sink protocol."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from bridle.logging.schema import LogEvent


@runtime_checkable
class LogSink(Protocol):
    def emit(self, event: LogEvent) -> None: ...

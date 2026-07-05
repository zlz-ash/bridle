"""JSONL log formatter and handler."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from bridle.logging.facade import emit_event as _emit_event


class JSONLFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "action": getattr(record, "action", record.getMessage()),
            "status": getattr(record, "status", "unknown"),
        }

        for field in ("task_id", "node_id", "plan_node_id", "run_id", "duration_ms"):
            value = getattr(record, field, None)
            if value is not None:
                log_entry[field] = value

        detail = getattr(record, "detail", None)
        if detail is not None:
            log_entry["detail"] = detail

        return json.dumps(log_entry, ensure_ascii=False, default=str)


def get_jsonl_logger(name: str = "bridle") -> logging.Logger:
    """Get a logger configured with JSONL output."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JSONLFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def log_event(
    action: str,
    status: str,
    *,
    task_id: str | None = None,
    node_id: str | None = None,
    run_id: str | None = None,
    duration_ms: int | None = None,
    detail: dict | None = None,
) -> None:
    """Emit a structured log event via logging facade."""
    _emit_event(
        action,
        status,
        task_id=task_id,
        node_id=node_id,
        run_id=run_id,
        duration_ms=duration_ms,
        detail=detail,
    )

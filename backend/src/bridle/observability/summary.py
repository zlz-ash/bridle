"""Text summarization and redaction for observability payloads."""
from __future__ import annotations

from typing import Any


def summarize_text(value: str, *, limit: int = 500) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def summarize_mapping(data: dict[str, Any], *, limit: int = 500) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str):
            out[key] = summarize_text(value, limit=limit)
        elif isinstance(value, list):
            out[key] = {"count": len(value)}
        elif isinstance(value, dict):
            out[key] = {"keys": list(value.keys())[:20]}
        else:
            out[key] = value
    return out

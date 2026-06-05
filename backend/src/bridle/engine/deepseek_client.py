"""DeepSeek HTTP client — backward-compatible re-exports.

All logic has been moved to openai_client.py which is provider-agnostic.
This module re-exports the generic types under their old names so that
existing imports continue to work.
"""
from __future__ import annotations

from bridle.engine.openai_client import (
    HttpOpenAICompatibleClient as HttpDeepSeekClient,
)
from bridle.engine.openai_client import (
    LLMHttpError as DeepSeekHttpError,
)
from bridle.engine.openai_client import (
    OpenAICompatibleClient as DeepSeekClient,
)

DEEPSEEK_DEFAULT_BASE = "https://api.deepseek.com"
DEEPSEEK_BETA_BASE = "https://api.deepseek.com/beta"

__all__ = [
    "DeepSeekClient",
    "HttpDeepSeekClient",
    "DeepSeekHttpError",
    "DEEPSEEK_DEFAULT_BASE",
    "DEEPSEEK_BETA_BASE",
]

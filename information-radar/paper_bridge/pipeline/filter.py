"""规则过滤：确定性排除无关内容。

业务契约（来自方案）：
- 排除抖音/TikTok 来源
- 排除纯概念、玩具编程题、纯 LLM 主观评分、无执行证据的多 Agent 角色扮演
- 规则是确定性的（不依赖 AI），命中即排除
- 返回 (保留, 丢弃) 两个列表，丢弃原因记录用于审计
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from loguru import logger

from paper_bridge.models import Item


@dataclass
class ExcludeRule:
    """来源黑名单中的条目（来源名或 URL 子串）。"""
    source_blacklist: list[str]
    title_contains: list[str]
    abstract_contains_any: list[str]


def load_exclude_rules(path: str | Path = "config/sources.yaml") -> ExcludeRule:
    """从 sources.yaml 的 exclude 段加载确定性排除规则。"""
    p = Path(path)
    if not p.exists():
        return ExcludeRule(source_blacklist=[], title_contains=[], abstract_contains_any=[])
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    excl = data.get("exclude", {}) or {}
    return ExcludeRule(
        source_blacklist=[s.lower() for s in excl.get("source_blacklist", [])],
        title_contains=[t.lower() for t in excl.get("title_contains", [])],
        abstract_contains_any=[a.lower() for a in excl.get("abstract_contains_any", [])],
    )


def _matches_blacklist(item: Item, rule: ExcludeRule) -> str | None:
    """检查来源黑名单（来源名或 URL）。返回命中规则描述，None=通过。"""
    text = f"{item.source_name} {item.url}".lower()
    for bl in rule.source_blacklist:
        if bl in text:
            return f"source_blacklist:{bl}"
    return None


def _matches_title(item: Item, rule: ExcludeRule) -> str | None:
    title_lower = item.title.lower()
    for t in rule.title_contains:
        if t in title_lower:
            return f"title_contains:{t}"
    return None


def _matches_abstract(item: Item, rule: ExcludeRule) -> str | None:
    if not item.abstract:
        return None
    abstract_lower = item.abstract.lower()
    for a in rule.abstract_contains_any:
        if a in abstract_lower:
            return f"abstract_contains_any:{a}"
    return None


def should_exclude(item: Item, rule: ExcludeRule) -> tuple[bool, str | None]:
    """判断条目是否应被确定性排除。

    Returns:
        (是否排除, 排除原因)。不排除时原因为 None。
    """
    for checker in (_matches_blacklist, _matches_title, _matches_abstract):
        reason = checker(item, rule)
        if reason is not None:
            return True, reason
    return False, None


def filter_items(
    items: list[Item], rule: ExcludeRule
) -> tuple[list[Item], list[tuple[Item, str]]]:
    """对一批条目应用确定性过滤。

    Returns:
        (保留的条目, 被丢弃的[(item, 原因)])
    """
    kept: list[Item] = []
    dropped: list[tuple[Item, str]] = []
    for it in items:
        excluded, reason = should_exclude(it, rule)
        if excluded:
            dropped.append((it, reason))  # type: ignore[arg-type]
            logger.debug("filtered out: {} reason={}", it.title[:50], reason)
        else:
            kept.append(it)
    logger.info(
        "rule filter: in={} kept={} dropped={}", len(items), len(kept), len(dropped)
    )
    return kept, dropped

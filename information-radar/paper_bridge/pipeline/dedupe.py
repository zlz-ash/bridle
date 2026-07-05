"""四键去重：DOI / arXiv ID / URL / 标题指纹（simhash）。

业务契约：任一非空键命中即视为重复。
- DOI：大小写/首尾空白归一化
- arXiv ID：去前缀、去版本号 vN
- URL：去末尾斜杠、去 utm/ref 跟踪参数
- 标题：NFKC + 小写 + 去标点 + 压缩空白 → 字符 3-gram shingle → simhash，海明距离 ≤ 5 视为相同
"""
from __future__ import annotations

from loguru import logger

from paper_bridge.models import Item, normalize_title

try:
    from simhash import Simhash

    _HAS_SIMHASH = True
except ImportError:  # pragma: no cover - 测试环境应装好依赖
    _HAS_SIMHASH = False

# 海明距离阈值：标题指纹相近到此阈值即视为重复。
# 经验值：字符 3-gram 下，单复数差异 hamming≈3-5；完全不同标题 hamming≈31+。
# 5 既能覆盖小差异（单复数/拼写），又与真正不同标题保持 26 的安全距离。
HAMMING_THRESHOLD = 5


def _char_shingles(text: str, n: int = 3) -> list[str]:
    """字符 n-gram shingle。短于 n 的文本整体作为一个 shingle。"""
    if len(text) < n:
        return [text] if text else []
    return [text[i : i + n] for i in range(len(text) - n + 1)]


def title_fingerprint(title: str) -> int | None:
    """标题 simhash 指纹（字符 3-gram）。空标题返回 None。

    用字符级 shingle 而非词级 token，使"Agents/Agent"等单字符差异产生很小的
    海明距离，同时真正不同的标题仍保持大距离。
    """
    norm = normalize_title(title)
    if not norm:
        return None
    if not _HAS_SIMHASH:  # pragma: no cover
        return None
    return Simhash(_char_shingles(norm)).value


def hamming(a: int, b: int) -> int:
    """两个 simhash 整数的海明距离。"""
    return bin(a ^ b).count("1")


def is_duplicate(
    item: Item, seen: list[Item]
) -> tuple[bool, str | None, Item | None]:
    """判断 item 是否与 seen 中任一条目重复。

    Returns:
        (是否重复, 命中的键名, 命中的对照条目)
        不重复时后两者为 None。
    """
    keys = item.dedup_keys()
    for s in seen:
        skeys = s.dedup_keys()
        # DOI
        if keys["doi"] and skeys["doi"] and keys["doi"] == skeys["doi"]:
            logger.debug("dedup hit doi: {} vs {}", keys["doi"], skeys["doi"])
            return True, "doi", s
        # arXiv ID
        if keys["arxiv_id"] and skeys["arxiv_id"] and keys["arxiv_id"] == skeys["arxiv_id"]:
            logger.debug("dedup hit arxiv: {} vs {}", keys["arxiv_id"], skeys["arxiv_id"])
            return True, "arxiv_id", s
        # URL
        if keys["url"] and skeys["url"] and keys["url"] == skeys["url"]:
            logger.debug("dedup hit url: {} vs {}", keys["url"], skeys["url"])
            return True, "url", s
        # 标题指纹
        fp_a = title_fingerprint(item.title)
        fp_b = title_fingerprint(s.title)
        if (
            fp_a is not None
            and fp_b is not None
            and hamming(fp_a, fp_b) <= HAMMING_THRESHOLD
        ):
            logger.debug(
                "dedup hit title_fp: hamming={} <= {}",
                hamming(fp_a, fp_b),
                HAMMING_THRESHOLD,
            )
            return True, "title_fingerprint", s
    return False, None, None


def dedupe_batch(items: list[Item]) -> tuple[list[Item], list[tuple[Item, str, Item]]]:
    """对一批条目做去重，返回 (去重后的条目, 被丢弃的[(item, 命中键, 对照条目)])。

    首次出现的条目保留，后续重复的被丢弃。批次内顺序保持稳定。
    """
    kept: list[Item] = []
    dropped: list[tuple[Item, str, Item]] = []
    for it in items:
        dup, key, hit = is_duplicate(it, kept)
        if dup:
            dropped.append((it, key, hit))  # type: ignore[arg-type]
        else:
            kept.append(it)
    logger.info("dedupe batch: in={} kept={} dropped={}", len(items), len(kept), len(dropped))
    return kept, dropped

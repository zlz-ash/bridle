"""去重业务契约测试：四键任一命中即重复。

业务契约（来自方案）：
- DOI、arXiv ID、URL 和标题指纹去重
- 大小写/版本号/跟踪参数差异不应产生假阴性
- 空键不参与匹配（避免两条无 DOI 的都被判重复）
"""
from __future__ import annotations

from paper_bridge.models import Item, normalize_title
from paper_bridge.pipeline.dedupe import (
    HAMMING_THRESHOLD,
    dedupe_batch,
    hamming,
    is_duplicate,
    title_fingerprint,
)


def make(**kw) -> Item:
    base = dict(source_type="arxiv", source_name="test", title="t", url="http://x")
    base.update(kw)
    return Item(**base)


# ---------- 标题归一化 ----------


class TestNormalizeTitle:
    def test_lowercases(self):
        assert normalize_title("Hello WORLD") == "hello world"

    def test_nfkc_fullwidth(self):
        # 全角字母应归一化为半角
        assert normalize_title("ＡＢＣ") == "abc"

    def test_strips_punctuation(self):
        assert normalize_title("Hello, World!") == "hello world"

    def test_compresses_whitespace(self):
        assert normalize_title("hello    world") == "hello world"


# ---------- 四键去重契约 ----------


class TestFourKeyDedupContract:
    def test_same_doi_is_duplicate_regardless_of_case(self):
        a = make(title="Alpha", url="http://a", doi="10.1145/XYZ.123")
        b = make(title="Beta", url="http://b", doi="10.1145/xyz.123")  # 大小写不同
        dup, key, _ = is_duplicate(b, [a])
        assert dup is True
        assert key == "doi"

    def test_same_arxiv_id_is_duplicate_without_version(self):
        a = make(title="Alpha", url="http://a", arxiv_id="2401.00001")
        b = make(title="Beta", url="http://b", arxiv_id="arXiv:2401.00001v3")
        dup, key, _ = is_duplicate(b, [a])
        assert dup is True
        assert key == "arxiv_id"

    def test_same_url_is_duplicate_after_trailing_slash(self):
        a = make(title="Alpha", url="http://example.com/x/")
        b = make(title="Beta", url="http://example.com/x")
        dup, key, _ = is_duplicate(b, [a])
        assert dup is True
        assert key == "url"

    def test_url_strips_utm_tracking(self):
        a = make(title="Alpha", url="http://example.com/x?utm_source=feed")
        b = make(title="Beta", url="http://example.com/x")
        dup, key, _ = is_duplicate(b, [a])
        assert dup is True
        assert key == "url"

    def test_similar_title_is_duplicate(self):
        # 仅一个字符差异（单复数），simhash 海明距离应 ≤ 阈值
        a = make(title="Repository-Level Coding Agents: A Survey", url="http://a")
        b = make(title="Repository-Level Coding Agent: A Survey", url="http://b")
        dup, key, _ = is_duplicate(b, [a])
        assert dup is True
        assert key == "title_fingerprint"

    def test_identical_title_is_duplicate(self):
        a = make(title="Same Title", url="http://a")
        b = make(title="Same Title", url="http://b")
        dup, key, _ = is_duplicate(b, [a])
        assert dup is True
        assert key == "title_fingerprint"

    def test_all_keys_different_is_not_duplicate(self):
        a = make(
            title="Alpha Beta",
            url="http://a.example.com/a",
            doi="10.1/a",
            arxiv_id="2401.00001",
        )
        b = make(
            title="Gamma Delta Epsilon",
            url="http://b.example.com/b",
            doi="10.1/b",
            arxiv_id="2401.00002",
        )
        dup, key, _ = is_duplicate(b, [a])
        assert dup is False
        assert key is None

    def test_empty_doi_does_not_match_empty_doi(self):
        # 两条都没有 DOI，不应因 None==None 而误判
        a = make(title="Alpha", url="http://a")
        b = make(title="Beta Gamma Delta", url="http://b")
        dup, _, _ = is_duplicate(b, [a])
        assert dup is False

    def test_completely_different_titles_far_apart(self):
        a = make(title="On the P vs NP Problem in Computational Complexity Theory", url="http://a")
        b = make(title="A Recipe for Chocolate Cake and Other Desserts", url="http://b")
        dup, key, _ = is_duplicate(b, [a])
        assert dup is False


# ---------- 批次去重 ----------


class TestBatchDedupe:
    def test_keeps_first_drops_subsequent(self):
        items = [
            make(title="Paper A", url="http://a", doi="10.1/a"),
            make(title="Paper A (copy)", url="http://b", doi="10.1/a"),  # DOI 重复
            make(title="Paper B", url="http://c", doi="10.1/b"),
            make(title="Paper C", url="http://d", doi="10.1/c"),
        ]
        kept, dropped = dedupe_batch(items)
        assert len(kept) == 3
        assert len(dropped) == 1
        assert dropped[0][1] == "doi"

    def test_empty_batch(self):
        kept, dropped = dedupe_batch([])
        assert kept == [] and dropped == []

    def test_preserves_order(self):
        items = [
            make(title="First", url="http://1", doi="10.1/1"),
            make(title="Second", url="http://2", doi="10.1/2"),
            make(title="Third", url="http://3", doi="10.1/3"),
        ]
        kept, _ = dedupe_batch(items)
        assert [k.title for k in kept] == ["First", "Second", "Third"]


# ---------- simhash 辅助 ----------


class TestSimhashHelpers:
    def test_hamming_identical_is_zero(self):
        assert hamming(0, 0) == 0
        assert hamming(12345, 12345) == 0

    def test_hamming_threshold_constant(self):
        # 字符 3-gram 下的阈值：覆盖单复数(≤5)，与完全不同标题(31+)保持安全距离
        assert HAMMING_THRESHOLD == 5

    def test_title_fingerprint_none_for_empty(self):
        assert title_fingerprint("") is None
        assert title_fingerprint("   ") is None

    def test_title_fingerprint_stable(self):
        fp1 = title_fingerprint("Some Interesting Paper Title")
        fp2 = title_fingerprint("Some Interesting Paper Title")
        assert fp1 == fp2

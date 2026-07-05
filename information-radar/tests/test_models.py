"""统一条目模型契约测试。"""
from __future__ import annotations

from datetime import datetime

from paper_bridge.models import Item


class TestItemContract:
    def test_minimal_required_fields(self):
        it = Item(source_type="blog", source_name="github", title="t", url="http://x")
        assert it.source_type == "blog"
        assert it.authors == []
        assert it.doi is None

    def test_doi_norm_lowercases_and_strips(self):
        it = Item(source_type="arxiv", source_name="s", title="t", url="http://x", doi="  10.1/ABC  ")
        assert it.doi_norm == "10.1/abc"

    def test_doi_norm_none_when_absent(self):
        it = Item(source_type="arxiv", source_name="s", title="t", url="http://x")
        assert it.doi_norm is None

    def test_arxiv_id_norm_strips_version_and_prefix(self):
        it = Item(
            source_type="arxiv", source_name="s", title="t", url="http://x",
            arxiv_id="arXiv:2401.00001v3",
        )
        assert it.arxiv_id_norm == "2401.00001"

    def test_arxiv_id_norm_none_when_absent(self):
        it = Item(source_type="arxiv", source_name="s", title="t", url="http://x")
        assert it.arxiv_id_norm is None

    def test_url_norm_strips_trailing_slash(self):
        it = Item(source_type="blog", source_name="s", title="t", url="http://example.com/x/")
        assert it.url_norm == "http://example.com/x"

    def test_url_norm_strips_utm(self):
        it = Item(
            source_type="blog", source_name="s", title="t",
            url="http://example.com/x?utm_source=feed&ref=newsletter",
        )
        assert it.url_norm == "http://example.com/x"

    def test_dedup_keys_returns_four_keys(self):
        it = Item(source_type="blog", source_name="s", title="Title", url="http://x", doi="10.1/a")
        keys = it.dedup_keys()
        assert set(keys.keys()) == {"doi", "arxiv_id", "url", "title"}

    def test_published_at_passthrough(self):
        ts = datetime(2026, 7, 5, 8, 0)
        it = Item(source_type="arxiv", source_name="s", title="t", url="http://x", published_at=ts)
        assert it.published_at == ts

    def test_raw_is_isolated(self):
        it = Item(source_type="blog", source_name="s", title="t", url="http://x", raw={"k": "v"})
        it2 = Item(source_type="blog", source_name="s", title="t", url="http://y")
        assert it.raw == {"k": "v"}
        assert it2.raw == {}  # 默认值不共享

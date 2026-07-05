"""RSS 来源采集契约测试。

业务契约：
- feedparser 解析后每个 entry 归一化为 Item（title/url/published_at/abstract）
- B站 URL 经 RSSHub 桥接：{rsshub}/bilibili/user/dynamic/{uid}
- 公众号 URL 经 WeWe-RSS 桥接
- 单来源失败（网络异常）不影响其他来源：factory 层捕获并隔离
- 无 entry 的 feed 返回空列表
"""
from __future__ import annotations

import httpx
import pytest
import respx

from paper_bridge.config import Settings
from paper_bridge.http_client import build_client
from paper_bridge.sources.base import feed_to_items
from paper_bridge.sources.bilibili import BilibiliSource
from paper_bridge.sources.blog import BlogRSSSource
from paper_bridge.sources.factory import build_rss_sources
from paper_bridge.sources.wechat_mp import WechatMpSource

ATOM_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test Blog</title>
  <entry>
    <title>First Post</title>
    <link href="http://example.com/first"/>
    <id>urn:uuid:1</id>
    <published>2026-07-04T08:00:00Z</published>
    <summary>Summary of first post</summary>
  </entry>
  <entry>
    <title>Second Post</title>
    <link href="http://example.com/second"/>
    <id>urn:uuid:2</id>
    <updated>2026-07-03T10:00:00Z</updated>
    <summary>Summary of second</summary>
  </entry>
</feed>
"""

EMPTY_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Empty</title></feed>
"""


class TestFeedParsingContract:
    def test_parses_entries_to_items(self):
        items = feed_to_items(ATOM_FEED, "blog", "Test Blog", "engineering")
        assert len(items) == 2
        assert items[0].title == "First Post"
        assert items[0].url == "http://example.com/first"
        assert items[0].abstract == "Summary of first post"
        assert items[0].category == "engineering"
        assert items[0].source_type == "blog"

    def test_published_at_parsed(self):
        items = feed_to_items(ATOM_FEED, "blog", "Test Blog")
        assert items[0].published_at is not None
        assert items[0].published_at.year == 2026

    def test_empty_feed_returns_empty_list(self):
        items = feed_to_items(EMPTY_FEED, "blog", "Empty")
        assert items == []

    def test_entry_without_link_is_skipped(self):
        feed = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
        <entry><title>No Link</title><id>x</id></entry></feed>"""
        items = feed_to_items(feed, "blog", "t")
        assert items == []

    def test_entry_without_title_uses_summary_prefix(self):
        feed = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
        <entry><link href="http://x/1"/><summary>A long summary that becomes title</summary></entry></feed>"""
        items = feed_to_items(feed, "blog", "t")
        assert len(items) == 1
        assert "summary" in items[0].title.lower()


class TestBlogSource:
    @respx.mock
    def test_fetch_returns_items(self):
        respx.get("http://blog.example.com/feed").mock(return_value=httpx.Response(200, text=ATOM_FEED))
        client = build_client(proxy=None)
        src = BlogRSSSource(name="Test", url="http://blog.example.com/feed", category="engineering")
        items = list(src.fetch(client))
        assert len(items) == 2
        assert items[0].source_name == "Test"
        client.close()

    @respx.mock
    def test_fetch_http_error_propagates(self):
        respx.get("http://blog.example.com/feed").mock(return_value=httpx.Response(500))
        client = build_client(proxy=None)
        src = BlogRSSSource(name="Test", url="http://blog.example.com/feed")
        with pytest.raises(httpx.HTTPStatusError):
            list(src.fetch(client))
        client.close()


class TestBilibiliSourceURLContract:
    def test_url_uses_rsshub_dynamic_template(self):
        s = BilibiliSource(name="UP", uid="12345", rsshub_url="http://rsshub:1200")
        assert s.url == "http://rsshub:1200/bilibili/user/dynamic/12345"

    def test_url_strips_trailing_slash_from_rsshub(self):
        s = BilibiliSource(name="UP", uid="99", rsshub_url="http://rsshub:1200/")
        assert s.url == "http://rsshub:1200/bilibili/user/dynamic/99"

    def test_source_type_is_bilibili(self):
        s = BilibiliSource(name="UP", uid="1", rsshub_url="http://r:1200")
        assert s.source_type == "bilibili"


class TestWechatMpSourceURLContract:
    def test_url_uses_wewe_feeds_template(self):
        s = WechatMpSource(name="MP", feed_id="abc123", wewe_rss_url="http://wewe:4000")
        assert s.url == "http://wewe:4000/feeds/abc123.atom"

    def test_source_type_is_wechat_mp(self):
        s = WechatMpSource(name="MP", feed_id="x", wewe_rss_url="http://w:4000")
        assert s.source_type == "wechat_mp"


class TestFactory:
    def test_builds_all_source_types(self):
        from paper_bridge.config import (
            BilibiliSource as CfgBili,
        )
        from paper_bridge.config import (
            BlogSource,
            SourcesConfig,
        )
        from paper_bridge.config import (
            WechatMpSource as CfgWechat,
        )

        cfg = SourcesConfig(
            blogs=[BlogSource(name="B1", url="http://b1/feed", category="engineering")],
            bilibili=[CfgBili(name="U1", uid="111", category="tech")],
            wechat_mp=[CfgWechat(name="W1", feed_id="wid", category="tech")],
        )
        settings = Settings(rsshub_url="http://rsshub:1200", wewe_rss_url="http://wewe:4000")
        sources = build_rss_sources(cfg, settings)
        assert len(sources) == 3
        types = {s.source_type for s in sources}
        assert types == {"blog", "bilibili", "wechat_mp"}

    def test_empty_config_returns_empty_list(self):
        from paper_bridge.config import SourcesConfig

        settings = Settings()
        assert build_rss_sources(SourcesConfig(), settings) == []


class TestSingleSourceFailureIsolation:
    """单来源失败不影响其他：采集器自身抛异常，调度层负责隔离。

    这里验证：A 失败时 B 仍可正常返回。隔离逻辑在 pipeline 编排层，
    采集器契约是"失败即抛异常，不吞错"。
    """

    @respx.mock
    def test_failing_source_raises_while_other_succeeds(self):
        respx.get("http://fail/feed").mock(return_value=httpx.Response(500))
        respx.get("http://ok/feed").mock(return_value=httpx.Response(200, text=ATOM_FEED))
        client = build_client(proxy=None)
        fail_src = BlogRSSSource(name="Fail", url="http://fail/feed")
        ok_src = BlogRSSSource(name="OK", url="http://ok/feed")

        results: dict[str, list] = {}
        errors: dict[str, Exception] = {}
        for src in [fail_src, ok_src]:
            try:
                results[src.name] = list(src.fetch(client))
            except Exception as e:
                errors[src.name] = e

        assert "Fail" in errors
        assert "OK" in results
        assert len(results["OK"]) == 2
        client.close()

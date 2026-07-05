"""论文来源采集契约测试。

业务契约（来自方案）：
- arXiv：采集 cs.SE/cs.AI/cs.CL，关键词过滤，提取 arXiv ID / DOI / PDF / 作者
- Semantic Scholar：按查询检索，填充 externalIds（DOI/ArXiv）、openAccessPdf、作者机构
- Crossref：按会议（ICSE/FSE/ASE/MSR）检索 proceedings-article，填充 DOI / 作者 / venue
- 单来源失败不影响其他：单个 query/category/venue 失败时跳过，其余正常
- 论文 Item 必须填充 source_type / venue / category=se_paper
"""
from __future__ import annotations

import httpx
import respx

from paper_bridge.http_client import build_client
from paper_bridge.sources.arxiv import ArxivSource
from paper_bridge.sources.crossref import CrossrefSource
from paper_bridge.sources.papers_factory import build_paper_sources
from paper_bridge.sources.semantic_scholar import SemanticScholarSource

# arXiv Atom feed 示例
ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>arXiv:cs.SE</title>
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <title>Repository-Level Coding Agents: An Empirical Study</title>
    <summary>We study coding agents operating at repository level.</summary>
    <published>2026-06-01T00:00:00Z</published>
    <author><name>Alice Chen</name></author>
    <author><name>Bob Smith</name></author>
    <link href="http://arxiv.org/pdf/2401.00001v1" type="application/pdf"/>
    <arxiv:doi xmlns:arxiv="http://arxiv.org/schemas/atom">10.1145/123.456</arxiv:doi>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.00002v1</id>
    <title>A Purely Theoretical Framework with No Implementation</title>
    <summary>This is a purely conceptual paper about nothing.</summary>
    <published>2026-06-02T00:00:00Z</published>
    <author><name>Carol Doe</name></author>
  </entry>
</feed>
"""

# Semantic Scholar 搜索响应
S2_RESPONSE = {
    "total": 2,
    "offset": 0,
    "data": [
        {
            "paperId": "abc123",
            "title": "Autonomous Bug Fixing Agent",
            "abstract": "We present an agent that fixes bugs autonomously.",
            "url": "https://www.semanticscholar.org/paper/abc123",
            "venue": "ICSE 2026",
            "year": 2026,
            "externalIds": {"DOI": "10.1145/fix.001", "ArXiv": "2601.00001"},
            "openAccessPdf": {"url": "https://example.com/paper.pdf", "status": "GREEN"},
            "authors": [
                {"name": "Dave Lee", "affiliations": ["ACME Labs"]},
                {"name": "Eve Wong", "affiliations": ["ACME Labs", "Univ X"]},
            ],
        },
        {
            "paperId": "def456",
            "title": "No DOI Paper",
            "url": "https://www.semanticscholar.org/paper/def456",
            "year": 2025,
            "externalIds": {},
            "authors": [{"name": "Frank"}],
        },
    ],
}

# Crossref 响应
CROSSREF_RESPONSE = {
    "message": {
        "items": [
            {
                "DOI": "10.1145/icse.001",
                "title": ["Test Generation via LLM at ICSE"],
                "URL": "https://doi.org/10.1145/icse.001",
                "author": [
                    {"given": "Grace", "family": "Hopper", "affiliation": [{"name": "Navy"}]}
                ],
                "published": {"date-parts": [[2026, 5, 15]]},
                "container-title": ["Proceedings of ICSE"],
                "abstract": "<jats:p>We generate tests using LLMs.</jats:p>",
                "link": [{"URL": "https://example.com/icse.pdf", "content-type": "application/pdf"}],
            },
            {
                "DOI": "10.1145/fse.002",
                "title": ["Code Review Automation at FSE"],
                "URL": "https://doi.org/10.1145/fse.002",
                "author": [{"given": "Alan", "family": "Turing"}],
                "published": {"date-parts": [[2026, 7]]},
                "container-title": ["Proceedings of FSE"],
            },
        ]
    }
}


class TestArxivSourceContract:
    @respx.mock
    def test_parses_entries_with_arxiv_id_and_pdf(self):
        respx.get("https://export.arxiv.org/api/query").mock(
            return_value=httpx.Response(200, text=ARXIV_FEED)
        )
        client = build_client(proxy=None)
        src = ArxivSource(categories=["cs.SE"], keywords=["coding agent", "repository"])
        items = list(src.fetch(client))
        # 关键词过滤：第一篇命中 "repository"，第二篇不命中
        assert len(items) == 1
        it = items[0]
        assert it.title.startswith("Repository-Level Coding Agents")
        assert it.arxiv_id == "2401.00001v1"
        assert it.doi == "10.1145/123.456"
        assert it.full_text_url == "http://arxiv.org/pdf/2401.00001v1"
        assert it.has_full_text is True
        assert it.authors == ["Alice Chen", "Bob Smith"]
        assert it.venue == "arXiv cs.SE"
        assert it.source_type == "arxiv"
        assert it.category == "se_paper"
        client.close()

    @respx.mock
    def test_no_keyword_filter_returns_all(self):
        respx.get("https://export.arxiv.org/api/query").mock(
            return_value=httpx.Response(200, text=ARXIV_FEED)
        )
        client = build_client(proxy=None)
        src = ArxivSource(categories=["cs.SE"], keywords=None)
        items = list(src.fetch(client))
        assert len(items) == 2
        client.close()

    @respx.mock
    def test_category_failure_isolated(self):
        # 第一个 category 500，第二个 200
        def handler(req):
            cat = req.url.params.get("search_query", "")
            if "cs.SE" in cat:
                return httpx.Response(500)
            return httpx.Response(200, text=ARXIV_FEED)

        respx.get("https://export.arxiv.org/api/query").mock(side_effect=handler)
        client = build_client(proxy=None)
        src = ArxivSource(categories=["cs.SE", "cs.AI"], keywords=None)
        items = list(src.fetch(client))
        # cs.SE 失败被跳过，cs.AI 返回 2 条
        assert len(items) == 2
        client.close()

    def test_url_property(self):
        src = ArxivSource(categories=["cs.SE", "cs.AI"])
        assert "cat:cs.SE" in src.url
        assert "cat:cs.AI" in src.url


class TestSemanticScholarContract:
    @respx.mock
    def test_parses_papers_with_external_ids(self):
        respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
            return_value=httpx.Response(200, json=S2_RESPONSE)
        )
        client = build_client(proxy=None)
        src = SemanticScholarSource(queries=["coding agent"], max_results_per_query=10)
        items = list(src.fetch(client))
        assert len(items) == 2

        it0 = items[0]
        assert it0.title == "Autonomous Bug Fixing Agent"
        assert it0.doi == "10.1145/fix.001"
        assert it0.arxiv_id == "2601.00001"
        assert it0.full_text_url == "https://example.com/paper.pdf"
        assert it0.has_full_text is True
        assert it0.authors == ["Dave Lee", "Eve Wong"]
        assert "ACME Labs" in it0.affiliations
        assert "Univ X" in it0.affiliations
        assert it0.venue == "ICSE 2026"
        assert it0.source_type == "semantic_scholar"
        client.close()

    @respx.mock
    def test_paper_without_doi_still_returned(self):
        respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
            return_value=httpx.Response(200, json=S2_RESPONSE)
        )
        client = build_client(proxy=None)
        src = SemanticScholarSource(queries=["test"])
        items = list(src.fetch(client))
        no_doi = [i for i in items if i.title == "No DOI Paper"]
        assert len(no_doi) == 1
        assert no_doi[0].doi is None
        assert no_doi[0].arxiv_id is None
        client.close()

    @respx.mock
    def test_query_failure_isolated(self):
        def handler(req):
            q = req.url.params.get("query", "")
            if "fail" in q:
                return httpx.Response(500)
            return httpx.Response(200, json=S2_RESPONSE)

        respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(side_effect=handler)
        client = build_client(proxy=None)
        src = SemanticScholarSource(queries=["fail", "ok"])
        items = list(src.fetch(client))
        assert len(items) == 2  # 只有 ok 查询返回
        client.close()

    @respx.mock
    def test_empty_results(self):
        respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
            return_value=httpx.Response(200, json={"total": 0, "data": []})
        )
        client = build_client(proxy=None)
        src = SemanticScholarSource(queries=["nothing"])
        items = list(src.fetch(client))
        assert items == []
        client.close()


class TestCrossrefContract:
    @respx.mock
    def test_parses_works_with_doi_and_venue(self):
        respx.get("https://api.crossref.org/works").mock(
            return_value=httpx.Response(200, json=CROSSREF_RESPONSE)
        )
        client = build_client(proxy=None)
        src = CrossrefSource(venues=["ICSE"], days_back=365)
        items = list(src.fetch(client))
        assert len(items) == 2

        it0 = items[0]
        assert it0.title == "Test Generation via LLM at ICSE"
        assert it0.doi == "10.1145/icse.001"
        assert it0.url == "https://doi.org/10.1145/icse.001"
        assert it0.authors == ["Grace Hopper"]
        assert "Navy" in it0.affiliations
        assert it0.venue == "Proceedings of ICSE"
        assert it0.full_text_url == "https://example.com/icse.pdf"
        assert it0.has_full_text is True
        assert it0.abstract == "We generate tests using LLMs."
        assert it0.source_type == "crossref"
        assert it0.category == "se_paper"
        client.close()

    @respx.mock
    def test_work_without_pdf_link(self):
        respx.get("https://api.crossref.org/works").mock(
            return_value=httpx.Response(200, json=CROSSREF_RESPONSE)
        )
        client = build_client(proxy=None)
        src = CrossrefSource(venues=["ICSE"])
        items = list(src.fetch(client))
        fse = [i for i in items if i.title == "Code Review Automation at FSE"]
        assert len(fse) == 1
        assert fse[0].full_text_url is None
        assert fse[0].has_full_text is False
        client.close()

    @respx.mock
    def test_venue_failure_isolated(self):
        def handler(req):
            v = req.url.params.get("query.bibliographic", "")
            if v == "ICSE":
                return httpx.Response(500)
            return httpx.Response(200, json=CROSSREF_RESPONSE)

        respx.get("https://api.crossref.org/works").mock(side_effect=handler)
        client = build_client(proxy=None)
        src = CrossrefSource(venues=["ICSE", "FSE"])
        items = list(src.fetch(client))
        assert len(items) == 2  # FSE 正常返回
        client.close()

    def test_from_date_filter(self):
        src = CrossrefSource(venues=["ICSE"], days_back=365)
        params = src._build_params("ICSE")
        assert "from-pub-date:" in params["filter"]
        assert "type:proceedings-article" in params["filter"]


class TestPapersFactory:
    def test_builds_all_three_source_types(self):
        cfg = {
            "arxiv": {"categories": ["cs.SE"], "keywords": ["agent"], "max_results_per_category": 10},
            "crossref": {"venues": ["ICSE"], "days_back": 180, "max_results_per_venue": 20},
            "semantic_scholar": {"queries": ["test"], "max_results_per_query": 5},
        }
        sources = build_paper_sources(cfg)
        assert len(sources) == 3
        types = {s.source_type for s in sources}
        assert types == {"arxiv", "crossref", "semantic_scholar"}

    def test_empty_config_returns_empty(self):
        assert build_paper_sources({}) == []

    def test_partial_config(self):
        sources = build_paper_sources({"arxiv": {"categories": ["cs.SE"]}})
        assert len(sources) == 1
        assert sources[0].source_type == "arxiv"

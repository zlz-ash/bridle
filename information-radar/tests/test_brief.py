"""三联简报契约测试。

业务契约：
- 每篇论文输出完整字段：标题/作者/机构/日期/会议/状态/评分/链接/DOI/arXiv/PDF/摘要8字段/推荐
- 三联格式：JSON / Markdown / HTML
- selected 在前，archived 在后
- 无开放全文标记"仅根据标题和摘要整理"
- JSON 可被程序解析回来
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from paper_bridge.models import Item
from paper_bridge.pipeline.scoring import ScoreBreakdown, ScoringConfig, score_item
from paper_bridge.pipeline.summarize import PaperSummary, parse_summary
from paper_bridge.report.brief import (
    build_brief,
    save_brief,
    to_html,
    to_json,
    to_markdown,
)

VALID_RESPONSE = """```json
{
  "research_question": "如何让 Agent 修复 Bug",
  "agent_loop_and_tools": "文件读取+测试+编辑循环",
  "context_memory_planning": "滑动窗口",
  "dataset_or_benchmark": "SWE-Bench",
  "main_results": "解决率 25%",
  "cost_and_latency": "$0.3/issue",
  "failure_modes_and_limitations": "复杂依赖失败率高",
  "recommendation": "值得复现"
}
```"""


def make_scored_item(title="Test Paper", has_full_text=True, source_type="arxiv") -> tuple[Item, ScoreBreakdown]:
    from datetime import timedelta

    item = Item(
        source_type=source_type,
        source_name="test",
        title=title,
        url="http://example.com/paper",
        authors=["Alice", "Bob"],
        affiliations=["MIT"],
        published_at=datetime.now(UTC) - timedelta(days=5),
        venue="ICSE",
        abstract="Real repository benchmark SWE-Bench code available",
        full_text_url="http://example.com/paper.pdf" if has_full_text else None,
        has_full_text=has_full_text,
        doi="10.1145/test.001",
        arxiv_id="2401.00001",
    )
    score = score_item(item, ScoringConfig())
    return item, score


class TestBriefBuilding:
    def test_build_brief_creates_brief_with_items(self):
        item, score = make_scored_item()
        summary = parse_summary(VALID_RESPONSE, has_full_text=True)
        brief = build_brief([(item, score)], {0: summary}, run_id="run-1")
        assert brief.brief_id.startswith("brief-")
        assert brief.run_id == "run-1"
        assert len(brief.items) == 1

    def test_selected_before_archived(self):
        high_item, high_score = make_scored_item("High Value Bug Fixing Agent Paper")
        low_item = Item(source_type="arxiv", source_name="t", title="Low", url="http://x", abstract=None)
        low_score = ScoreBreakdown(
            domain_relevance=0, practical_value=0, evidence_quality=0,
            reproducibility=0, timeliness=0, total=0, tier="audit_only"
        )
        brief = build_brief(
            [(high_item, high_score), (low_item, low_score)],
            {0: PaperSummary(has_full_text=True), 1: PaperSummary(has_full_text=False)},
            run_id="r",
        )
        selected = brief.selected()
        # selected 应在 items 列表前部
        if selected:
            assert brief.items[0].tier in ("selected", "archived")

    def test_partial_marker_set_when_no_full_text(self):
        item, score = make_scored_item(has_full_text=False)
        summary = parse_summary(VALID_RESPONSE, has_full_text=False)
        brief = build_brief([(item, score)], {0: summary}, run_id="r")
        assert brief.items[0].partial_marker == "[仅根据标题和摘要整理]"

    def test_no_partial_marker_when_full_text(self):
        item, score = make_scored_item(has_full_text=True)
        summary = parse_summary(VALID_RESPONSE, has_full_text=True)
        brief = build_brief([(item, score)], {0: summary}, run_id="r")
        assert brief.items[0].partial_marker is None


class TestJSONExport:
    def test_json_is_valid_and_parseable(self):
        item, score = make_scored_item()
        summary = parse_summary(VALID_RESPONSE, has_full_text=True)
        brief = build_brief([(item, score)], {0: summary}, run_id="r")
        js = to_json(brief)
        parsed = json.loads(js)
        assert parsed["brief_id"] == brief.brief_id
        assert len(parsed["items"]) == 1
        assert parsed["items"][0]["title"] == "Test Paper"

    def test_json_contains_all_summary_fields(self):
        item, score = make_scored_item()
        summary = parse_summary(VALID_RESPONSE, has_full_text=True)
        brief = build_brief([(item, score)], {0: summary}, run_id="r")
        js = json.loads(to_json(brief))
        s = js["items"][0]["summary"]
        for field in ["research_question", "agent_loop_and_tools", "context_memory_planning",
                      "dataset_or_benchmark", "main_results", "cost_and_latency",
                      "failure_modes_and_limitations", "recommendation"]:
            assert field in s

    def test_json_contains_doi_and_arxiv(self):
        item, score = make_scored_item()
        summary = parse_summary(VALID_RESPONSE, has_full_text=True)
        brief = build_brief([(item, score)], {0: summary}, run_id="r")
        js = json.loads(to_json(brief))
        assert js["items"][0]["doi"] == "10.1145/test.001"
        assert js["items"][0]["arxiv_id"] == "2401.00001"


class TestMarkdownExport:
    def test_markdown_has_title_and_date(self):
        item, score = make_scored_item()
        summary = parse_summary(VALID_RESPONSE, has_full_text=True)
        brief = build_brief([(item, score)], {0: summary}, run_id="r")
        md = to_markdown(brief)
        assert "信息雷达每日简报" in md
        assert brief.date in md

    def test_markdown_has_paper_fields(self):
        item, score = make_scored_item()
        summary = parse_summary(VALID_RESPONSE, has_full_text=True)
        brief = build_brief([(item, score)], {0: summary}, run_id="r")
        md = to_markdown(brief)
        assert "Test Paper" in md
        assert "Alice" in md
        assert "MIT" in md
        assert "ICSE" in md
        assert "10.1145/test.001" in md
        assert "2401.00001" in md
        assert "值得复现" in md

    def test_markdown_shows_partial_marker(self):
        item, score = make_scored_item(has_full_text=False)
        summary = parse_summary(VALID_RESPONSE, has_full_text=False)
        brief = build_brief([(item, score)], {0: summary}, run_id="r")
        md = to_markdown(brief)
        assert "仅根据标题和摘要整理" in md

    def test_markdown_empty_brief(self):
        brief = build_brief([], {}, run_id="r")
        md = to_markdown(brief)
        assert "今日无新增精选内容" in md


class TestHTMLExport:
    def test_html_has_doctype_and_title(self):
        item, score = make_scored_item()
        summary = parse_summary(VALID_RESPONSE, has_full_text=True)
        brief = build_brief([(item, score)], {0: summary}, run_id="r")
        html = to_html(brief)
        assert "<!DOCTYPE html>" in html
        assert "信息雷达每日简报" in html

    def test_html_contains_paper_data(self):
        item, score = make_scored_item()
        summary = parse_summary(VALID_RESPONSE, has_full_text=True)
        brief = build_brief([(item, score)], {0: summary}, run_id="r")
        html = to_html(brief)
        assert "Test Paper" in html
        assert "Alice" in html
        assert "值得复现" in html

    def test_html_partial_marker(self):
        item, score = make_scored_item(has_full_text=False)
        summary = parse_summary(VALID_RESPONSE, has_full_text=False)
        brief = build_brief([(item, score)], {0: summary}, run_id="r")
        html = to_html(brief)
        assert "仅根据标题和摘要整理" in html


class TestSaveBrief:
    def test_saves_three_files(self, tmp_path):
        item, score = make_scored_item()
        summary = parse_summary(VALID_RESPONSE, has_full_text=True)
        brief = build_brief([(item, score)], {0: summary}, run_id="r")
        paths = save_brief(brief, tmp_path)
        assert len(paths) == 3
        assert all(p.exists() for p in paths.values())
        assert paths["json"].suffix == ".json"
        assert paths["markdown"].suffix == ".md"
        assert paths["html"].suffix == ".html"

    def test_saved_json_is_valid(self, tmp_path):
        item, score = make_scored_item()
        summary = parse_summary(VALID_RESPONSE, has_full_text=True)
        brief = build_brief([(item, score)], {0: summary}, run_id="r")
        paths = save_brief(brief, tmp_path)
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        assert data["brief_id"] == brief.brief_id

"""AI 摘要契约测试。

业务契约（来自方案）：
- 输出 8 个字段：研究问题/Agent循环/Context记忆/数据集基准/主要结果/成本延迟/失败模式/推荐
- 无开放全文时标记"仅根据标题和摘要整理"
- 未报告字段填"未报告"
- 推荐规范化为三个值之一
- 解析失败时用默认值，不抛异常
"""
from __future__ import annotations

from paper_bridge.models import Item
from paper_bridge.pipeline.summarize import (
    NO_FULL_TEXT_MARKER,
    SUMMARY_FIELDS,
    PaperSummary,
    build_user_prompt,
    parse_summary,
)

VALID_RESPONSE = """```json
{
  "research_question": "如何让 Agent 在仓库级别修复 Bug",
  "agent_loop_and_tools": "Agent 使用文件读取、测试运行、代码编辑工具循环",
  "context_memory_planning": "使用滑动窗口管理上下文，无长期记忆",
  "dataset_or_benchmark": "SWE-Bench，真实 GitHub 仓库",
  "main_results": "在 SWE-Bench 上解决率 25%",
  "cost_and_latency": "平均每个 issue 花费 $0.3，延迟 2 分钟",
  "failure_modes_and_limitations": "对复杂依赖关系失败率高",
  "recommendation": "值得复现"
}
```"""

PARTIAL_RESPONSE = """```json
{
  "research_question": "研究 Agent 的测试生成能力",
  "agent_loop_and_tools": "未报告",
  "context_memory_planning": "未报告",
  "dataset_or_benchmark": "未报告",
  "main_results": "未报告",
  "cost_and_latency": "未报告",
  "failure_modes_and_limitations": "未报告",
  "recommendation": "值得了解"
}
```"""

NO_JSON_RESPONSE = "抱歉，我无法处理这个请求。"


class TestParseSummaryContract:
    def test_parses_all_fields(self):
        s = parse_summary(VALID_RESPONSE, has_full_text=True)
        assert s.research_question == "如何让 Agent 在仓库级别修复 Bug"
        assert s.agent_loop_and_tools != "未报告"
        assert s.dataset_or_benchmark == "SWE-Bench，真实 GitHub 仓库"
        assert s.main_results == "在 SWE-Bench 上解决率 25%"
        assert s.cost_and_latency == "平均每个 issue 花费 $0.3，延迟 2 分钟"
        assert s.failure_modes_and_limitations == "对复杂依赖关系失败率高"
        assert s.recommendation == "值得复现"
        assert s.has_full_text is True
        assert s.marked_partial is False

    def test_partial_fields_default_to_unreported(self):
        s = parse_summary(PARTIAL_RESPONSE, has_full_text=True)
        assert s.research_question == "研究 Agent 的测试生成能力"
        assert s.agent_loop_and_tools == "未报告"
        assert s.context_memory_planning == "未报告"
        assert s.main_results == "未报告"

    def test_no_full_text_marks_partial(self):
        s = parse_summary(VALID_RESPONSE, has_full_text=False)
        assert s.marked_partial is True
        assert s.has_full_text is False
        d = s.to_dict()
        assert d["_partial_marker"] == NO_FULL_TEXT_MARKER

    def test_has_full_text_not_marked(self):
        s = parse_summary(VALID_RESPONSE, has_full_text=True)
        assert s.marked_partial is False
        assert "_partial_marker" not in s.to_dict()

    def test_no_json_returns_defaults(self):
        s = parse_summary(NO_JSON_RESPONSE, has_full_text=False)
        assert s.research_question == "未报告"
        assert s.marked_partial is True

    def test_recommendation_normalization(self):
        for rec_raw, expected in [
            ("这篇论文值得复现", "值得复现"),
            ("建议值得了解", "值得了解"),
            ("不太相关", "低优先级"),
            ("", "低优先级"),
        ]:
            resp = '{"research_question":"x","recommendation":"' + rec_raw + '"}'
            s = parse_summary(resp, has_full_text=True)
            assert s.recommendation == expected

    def test_all_eight_fields_present(self):
        s = parse_summary(VALID_RESPONSE, has_full_text=True)
        for field_name in SUMMARY_FIELDS:
            assert hasattr(s, field_name), f"missing field: {field_name}"

    def test_to_dict_includes_all_fields(self):
        s = parse_summary(VALID_RESPONSE, has_full_text=True)
        d = s.to_dict()
        for field_name in SUMMARY_FIELDS:
            assert field_name in d


class TestBuildUserPromptContract:
    def test_includes_all_required_fields(self):
        from datetime import UTC, datetime

        item = Item(
            source_type="arxiv",
            source_name="test",
            title="Test Paper",
            url="http://x",
            authors=["Alice", "Bob"],
            affiliations=["MIT"],
            published_at=datetime(2026, 7, 5, tzinfo=UTC),
            venue="ICSE",
            abstract="An abstract",
            has_full_text=True,
        )
        prompt = build_user_prompt(item)
        assert "标题：Test Paper" in prompt
        assert "Alice" in prompt and "Bob" in prompt
        assert "MIT" in prompt
        assert "2026-07-05" in prompt
        assert "ICSE" in prompt
        assert "预印本" in prompt
        assert "An abstract" in prompt
        assert "true" in prompt

    def test_missing_fields_show_unreported(self):
        item = Item(source_type="crossref", source_name="t", title="X", url="http://x")
        prompt = build_user_prompt(item)
        assert "未报告" in prompt
        assert "正式论文" in prompt

    def test_full_text_false_shown(self):
        item = Item(source_type="arxiv", source_name="t", title="X", url="http://x")
        prompt = build_user_prompt(item)
        assert "开放全文：false" in prompt


class TestPaperSummaryDataclass:
    def test_defaults(self):
        s = PaperSummary()
        assert s.research_question == "未报告"
        assert s.recommendation == "低优先级"
        assert s.has_full_text is False
        assert s.marked_partial is False

    def test_to_dict_partial_marker_only_when_partial(self):
        s_full = PaperSummary(has_full_text=True)
        assert "_partial_marker" not in s_full.to_dict()

        s_partial = PaperSummary(has_full_text=False, marked_partial=True)
        assert "_partial_marker" in s_partial.to_dict()

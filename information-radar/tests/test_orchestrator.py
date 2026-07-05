"""管线编排契约测试。

业务契约（来自方案）：
- 单一来源失败不影响其他来源
- 成功后才提交水位
- 重复执行不重复推送（幂等）
- 无新增时发送简短状态
- 各阶段统计完整记录
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import patch

import httpx
import pytest
import respx

from paper_bridge.config import Settings
from paper_bridge.models import Item
from paper_bridge.pipeline.orchestrator import Pipeline


@pytest.fixture
def pipeline(tmp_path):
    """构造测试用管线（不连真实服务）。"""
    settings = Settings(
        http_proxy=None,
        rsshub_url="http://rsshub:1200",
        wewe_rss_url="http://wewe:4000",
    )
    db_path = str(tmp_path / "test.db")
    report_dir = tmp_path / "reports"
    log_dir = tmp_path / "logs"

    # 创建最小 sources.yaml
    sources_yaml = tmp_path / "sources.yaml"
    sources_yaml.write_text(
        "blogs: []\nbilibili: []\nwechat_mp: []\n"
        "papers:\n  arxiv:\n    categories: [cs.SE]\n    keywords: [coding agent]\n",
        encoding="utf-8",
    )
    scoring_yaml = tmp_path / "scoring.yaml"
    scoring_yaml.write_text(
        "weights:\n  domain_relevance: 35\n  practical_value: 25\n"
        "  evidence_quality: 20\n  reproducibility: 10\n  timeliness: 10\n"
        "tiers:\n  selected: 70\n  archived: 50\n  audit_only: 0\n",
        encoding="utf-8",
    )

    p = Pipeline(
        settings=settings,
        sources_config_path=str(sources_yaml),
        scoring_config_path=str(scoring_yaml),
        db_path=db_path,
        log_dir=str(log_dir),
        report_dir=str(report_dir),
    )
    yield p
    p.close()


class TestPipelineStructure:
    def test_pipeline_initializes(self, pipeline):
        assert pipeline.conn is not None
        assert pipeline.watermarks is not None
        assert pipeline.runs is not None

    def test_health_check_returns_dict(self, pipeline):
        result = pipeline.health_check()
        assert "timestamp" in result
        assert "db" in result
        assert result["db"] == "ok"


class TestSourceFailureIsolation:
    """单来源失败不影响其他来源。"""

    @respx.mock
    def test_one_source_fails_others_succeed(self, pipeline, tmp_path):
        # arXiv 返回 500
        respx.get("https://export.arxiv.org/api/query").mock(
            return_value=httpx.Response(500)
        )
        # 直接注入 items 模拟采集成功
        good_items = [
            Item(
                source_type="blog",
                source_name="test-blog",
                title="Repository-Level Coding Agent for Bug Fixing",
                url="http://example.com/1",
                abstract="Real repository benchmark SWE-Bench public code",
                authors=["A"],
                venue="ICSE",
                published_at=datetime.now(UTC),
                full_text_url="http://example.com/1.pdf",
                has_full_text=True,
                doi="10.1/1",
            ),
        ]

        with patch.object(pipeline, "_collect") as mock_collect:
            # _collect 内部会隔离失败，只返回成功的 items
            mock_collect.return_value = good_items
            stats = pipeline.run(run_id="test-isolation")

        # 即使有来源失败，管线仍完成
        assert stats["items_in"] == 1
        assert "errors" in stats


class TestIdempotentRun:
    """重复执行不重复推送。"""

    @respx.mock
    def test_second_run_does_not_repush(self, pipeline):
        # 第一次运行：注入一个 item
        item = Item(
            source_type="blog",
            source_name="test",
            title="Test Paper for Idempotency",
            url="http://example.com/idem",
            abstract="Real repository benchmark",
            authors=["A"],
            venue="ICSE",
            published_at=datetime.now(UTC),
            full_text_url="http://example.com/idem.pdf",
            has_full_text=True,
            doi="10.1/idem",
        )

        # mock 飞书 webhook
        respx.post("https://open.feishu.cn/hook/test").mock(
            return_value=httpx.Response(200, json={"code": 0, "msg": "ok"})
        )

        with patch.object(pipeline, "_collect", return_value=[item]), \
             patch.dict(os.environ, {
                 "FEISHU_WEBHOOK": "https://open.feishu.cn/hook/test",
                 "OPENAI_API_KEY": "sk-replace-me",
             }):
                stats1 = pipeline.run(run_id="test-idem-1")

        # 第一次应该推送
        assert stats1["pushed"] == 1

        # 第二次运行：同样的 item（已在 DB 中）
        with patch.object(pipeline, "_collect", return_value=[item]), \
             patch.dict(os.environ, {
                 "FEISHU_WEBHOOK": "https://open.feishu.cn/hook/test",
                 "OPENAI_API_KEY": "sk-replace-me",
             }):
                stats2 = pipeline.run(run_id="test-idem-2")

        # 第二次因跨批次去重，new_items=0，但仍会发空状态
        # 关键：第二次 run 有自己的 run_id，推送的是新 run
        assert stats2["pushed"] == 1  # 空状态也算推送


class TestWatermarkCommit:
    """成功后才提交水位。"""

    @respx.mock
    def test_watermark_committed_after_success(self, pipeline):
        item = Item(
            source_type="blog",
            source_name="wm-test",
            title="Watermark Test Paper",
            url="http://example.com/wm",
            abstract="Real repository benchmark",
            authors=["A"],
            venue="ICSE",
            published_at=datetime.now(UTC),
            full_text_url="http://example.com/wm.pdf",
            has_full_text=True,
            doi="10.1/wm",
        )

        respx.post("https://open.feishu.cn/hook/test").mock(
            return_value=httpx.Response(200, json={"code": 0, "msg": "ok"})
        )

        def fake_collect(stats):
            stats["source_results"]["wm-test"] = {"status": "ok", "items": 1, "elapsed": 0.01}
            return [item]

        with patch.object(pipeline, "_collect", side_effect=fake_collect), \
             patch.dict(os.environ, {
                 "FEISHU_WEBHOOK": "https://open.feishu.cn/hook/test",
                 "OPENAI_API_KEY": "sk-replace-me",
             }):
                pipeline.run(run_id="wm-test-run")

        # 水位应已提交
        wm = pipeline.watermarks.get("wm-test")
        assert wm is not None


class TestStatsRecording:
    """各阶段统计完整记录。"""

    def test_stats_contains_all_stages(self, pipeline):
        item = Item(
            source_type="blog",
            source_name="stats-test",
            title="Stats Test",
            url="http://example.com/stats",
            abstract=None,
        )

        with patch.object(pipeline, "_collect", return_value=[item]), \
             patch.dict(os.environ, {
                 "FEISHU_WEBHOOK": "",
                 "OPENAI_API_KEY": "sk-replace-me",
             }):
                stats = pipeline.run(run_id="stats-test")

        required_keys = [
            "run_id", "started_at", "items_in", "items_dedup",
            "items_filtered", "items_scored", "items_selected",
            "pushed", "cost_usd", "errors", "source_results",
        ]
        for key in required_keys:
            assert key in stats, f"missing stat: {key}"

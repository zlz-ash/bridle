"""飞书推送幂等契约测试。

业务契约（来自方案）：
- 重复执行不得重复发送（RunStore.is_pushed 拦截）
- 无新增时发送简短运行状态
- 故障通知用独立 webhook
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import httpx
import pytest
import respx

from paper_bridge.notify.feishu import FeishuNotifier
from paper_bridge.report.brief import Brief
from paper_bridge.storage.db import init_db
from paper_bridge.storage.watermark import RunStore


@pytest.fixture
def run_store(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    rs = RunStore(conn)
    yield rs
    conn.close()


@pytest.fixture
def brief():
    return Brief(
        brief_id="brief-test",
        run_id="run-test",
        date="2026-07-05",
        generated_at="2026-07-05T08:30:00",
        stats={"items_in": 10, "items_dedup": 2, "items_filtered": 3, "items_selected": 5},
        items=[],
    )


class TestIdempotentPush:
    """幂等推送：已推送的 run 不重发。"""

    @respx.mock
    def test_first_push_succeeds_and_marks(self, run_store, brief):
        respx.post("https://open.feishu.cn/hook/test").mock(
            return_value=httpx.Response(200, json={"code": 0, "msg": "ok"})
        )
        # 添加一个 selected 条目让 brief 非空
        from paper_bridge.report.brief import BriefItem
        brief.items = [BriefItem(title="Test", url="http://x", total_score=80, tier="selected")]
        run_store.start_run("run-test", datetime.now(UTC))

        notifier = FeishuNotifier(
            webhook="https://open.feishu.cn/hook/test",
            run_store=run_store,
            proxy=None,
        )
        ok = notifier.push_brief(brief, "run-test")
        assert ok is True
        assert run_store.is_pushed("run-test") is True
        notifier.close()

    @respx.mock
    def test_second_push_skipped(self, run_store, brief):
        """已推送的 run 再次调用应跳过，不发送 HTTP。"""
        from paper_bridge.report.brief import BriefItem
        brief.items = [BriefItem(title="Test", url="http://x", total_score=80, tier="selected")]
        run_store.start_run("run-test", datetime.now(UTC))
        run_store.mark_pushed("run-test", "feishu", 1)

        # mock 不应被调用（已推送应跳过）
        route = respx.post("https://open.feishu.cn/hook/test")
        notifier = FeishuNotifier(
            webhook="https://open.feishu.cn/hook/test",
            run_store=run_store,
            proxy=None,
        )
        ok = notifier.push_brief(brief, "run-test")
        assert ok is True
        assert route.call_count == 0  # 未发送 HTTP
        notifier.close()


class TestEmptyStatusPush:
    """无新增时发送简短状态。"""

    @respx.mock
    def test_empty_brief_pushes_status(self, run_store, brief):
        # brief.items 为空 → 无 selected/archived
        run_store.start_run("run-empty", datetime.now(UTC))
        route = respx.post("https://open.feishu.cn/hook/test").mock(
            return_value=httpx.Response(200, json={"code": 0, "msg": "ok"})
        )
        notifier = FeishuNotifier(
            webhook="https://open.feishu.cn/hook/test",
            run_store=run_store,
            proxy=None,
        )
        ok = notifier.push_brief(brief, "run-empty")
        assert ok is True
        assert route.call_count == 1
        assert run_store.is_pushed("run-empty") is True
        notifier.close()


class TestAlertWebhook:
    """故障通知用独立 webhook。"""

    @respx.mock
    def test_alert_uses_alert_webhook(self):
        alert_route = respx.post("https://open.feishu.cn/hook/alert").mock(
            return_value=httpx.Response(200, json={"code": 0, "msg": "ok"})
        )
        main_route = respx.post("https://open.feishu.cn/hook/main").mock(
            return_value=httpx.Response(200, json={"code": 0, "msg": "ok"})
        )
        notifier = FeishuNotifier(
            webhook="https://open.feishu.cn/hook/main",
            alert_webhook="https://open.feishu.cn/hook/alert",
            proxy=None,
        )
        ok = notifier.push_alert("测试故障")
        assert ok is True
        assert alert_route.call_count == 1
        assert main_route.call_count == 0  # 故障通知不走主 webhook
        notifier.close()

    @respx.mock
    def test_alert_defaults_to_main_webhook(self):
        """未设 alert_webhook 时走主 webhook。"""
        route = respx.post("https://open.feishu.cn/hook/main").mock(
            return_value=httpx.Response(200, json={"code": 0, "msg": "ok"})
        )
        notifier = FeishuNotifier(
            webhook="https://open.feishu.cn/hook/main",
            proxy=None,
        )
        ok = notifier.push_alert("测试故障")
        assert ok is True
        assert route.call_count == 1
        notifier.close()


class TestApiErrorHandling:
    @respx.mock
    def test_feishu_api_error_returns_false(self, run_store, brief):
        from paper_bridge.report.brief import BriefItem
        brief.items = [BriefItem(title="Test", url="http://x", total_score=80, tier="selected")]
        run_store.start_run("run-err", datetime.now(UTC))
        respx.post("https://open.feishu.cn/hook/test").mock(
            return_value=httpx.Response(200, json={"code": 19021, "msg": "invalid webhook"})
        )
        notifier = FeishuNotifier(
            webhook="https://open.feishu.cn/hook/test",
            run_store=run_store,
            proxy=None,
        )
        ok = notifier.push_brief(brief, "run-err")
        assert ok is False
        assert run_store.is_pushed("run-err") is False  # 失败不标记
        notifier.close()

    @respx.mock
    def test_http_error_returns_false(self, run_store, brief):
        from paper_bridge.report.brief import BriefItem
        brief.items = [BriefItem(title="Test", url="http://x", total_score=80, tier="selected")]
        run_store.start_run("run-http-err", datetime.now(UTC))
        respx.post("https://open.feishu.cn/hook/test").mock(
            return_value=httpx.Response(500)
        )
        notifier = FeishuNotifier(
            webhook="https://open.feishu.cn/hook/test",
            run_store=run_store,
            proxy=None,
        )
        ok = notifier.push_brief(brief, "run-http-err")
        assert ok is False
        assert run_store.is_pushed("run-http-err") is False
        notifier.close()

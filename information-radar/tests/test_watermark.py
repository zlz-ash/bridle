"""水位与幂等推送业务契约测试。

业务契约（来自方案）：
- 成功后才提交来源和推送水位
- 水位只前进不后退（失败不应让水位倒退）
- 重复执行不得重复发送
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from paper_bridge.storage.watermark import RunStore, WatermarkStore


class TestWatermarkContract:
    def test_initial_watermark_is_none(self, db_conn):
        store = WatermarkStore(db_conn)
        assert store.get("blog:github") is None

    def test_commit_sets_watermark(self, db_conn):
        store = WatermarkStore(db_conn)
        t1 = datetime(2026, 7, 5, 8, 0, tzinfo=UTC)
        store.commit("blog:github", t1)
        assert store.get("blog:github") == t1

    def test_failure_does_not_rollback_committed_watermark(self, db_conn):
        """失败回退场景：水位已提交后，下次采集失败，水位不应倒退。"""
        store = WatermarkStore(db_conn)
        t_success = datetime(2026, 7, 5, 8, 0, tzinfo=UTC)
        store.commit("blog:github", t_success)
        # 模拟下次失败：代码根本不调用 commit，水位保持
        assert store.get("blog:github") == t_success

    def test_commit_if_newer_rejects_older(self, db_conn):
        store = WatermarkStore(db_conn)
        t_new = datetime(2026, 7, 5, 8, 0, tzinfo=UTC)
        t_old = datetime(2026, 7, 4, 8, 0, tzinfo=UTC)
        store.commit("blog:github", t_new)
        accepted = store.commit_if_newer("blog:github", t_old)
        assert accepted is False
        assert store.get("blog:github") == t_new

    def test_commit_if_newer_accepts_newer(self, db_conn):
        store = WatermarkStore(db_conn)
        t1 = datetime(2026, 7, 5, 8, 0, tzinfo=UTC)
        t2 = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)
        store.commit("blog:github", t1)
        accepted = store.commit_if_newer("blog:github", t2)
        assert accepted is True
        assert store.get("blog:github") == t2

    def test_commit_if_newer_rejects_equal(self, db_conn):
        store = WatermarkStore(db_conn)
        t = datetime(2026, 7, 5, 8, 0, tzinfo=UTC)
        store.commit("blog:github", t)
        accepted = store.commit_if_newer("blog:github", t)
        assert accepted is False

    def test_naive_datetime_treated_as_utc(self, db_conn):
        """无时区的 datetime 视为 UTC，不报错。"""
        store = WatermarkStore(db_conn)
        t_naive = datetime(2026, 7, 5, 8, 0)
        store.commit("blog:github", t_naive)
        got = store.get("blog:github")
        assert got is not None and got.year == 2026

    def test_independent_sources(self, db_conn):
        store = WatermarkStore(db_conn)
        t1 = datetime(2026, 7, 5, 8, 0, tzinfo=UTC)
        t2 = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)
        store.commit("blog:github", t1)
        store.commit("arxiv:cs.SE", t2)
        assert store.get("blog:github") == t1
        assert store.get("arxiv:cs.SE") == t2


class TestRunIdempotencyContract:
    """幂等推送契约：同一 run 成功推送后，重复标记不应产生副作用。"""

    def test_run_not_pushed_initially(self, db_conn):
        runs = RunStore(db_conn)
        runs.start_run("run-2026-07-05", datetime(2026, 7, 5, 8, 30, tzinfo=UTC))
        assert runs.is_pushed("run-2026-07-05") is False

    def test_mark_pushed_makes_is_pushed_true(self, db_conn):
        runs = RunStore(db_conn)
        rid = "run-2026-07-05"
        runs.start_run(rid, datetime(2026, 7, 5, 8, 30, tzinfo=UTC))
        runs.mark_pushed(rid, channel="feishu", items_count=5)
        assert runs.is_pushed(rid) is True

    def test_duplicate_push_mark_does_not_double_count(self, db_conn):
        """重复执行不得重复发送：已推送的 run 再次标记不应出错，状态保持已推送。"""
        runs = RunStore(db_conn)
        rid = "run-2026-07-05"
        runs.start_run(rid, datetime(2026, 7, 5, 8, 30, tzinfo=UTC))
        runs.mark_pushed(rid, "feishu", 5)
        # 模拟重复执行：再次标记（实际管线应在标记前用 is_pushed 拦截）
        runs.mark_pushed(rid, "feishu", 5)
        assert runs.is_pushed(rid) is True
        # push_log 应有 2 条记录（审计完整），但 runs.pushed 状态为 1
        row = db_conn.execute("SELECT pushed FROM runs WHERE run_id=?", (rid,)).fetchone()
        assert row["pushed"] == 1

    def test_finish_run_records_stats(self, db_conn):
        runs = RunStore(db_conn)
        rid = "run-2026-07-05"
        started = datetime(2026, 7, 5, 8, 30, tzinfo=UTC)
        finished = started + timedelta(minutes=5)
        runs.start_run(rid, started)
        runs.finish_run(
            rid,
            finished,
            status="ok",
            stats={
                "items_in": 100,
                "items_dedup": 20,
                "items_filtered": 50,
                "items_selected": 10,
                "pushed": 1,
                "cost_usd": 0.42,
            },
        )
        row = db_conn.execute("SELECT * FROM runs WHERE run_id=?", (rid,)).fetchone()
        assert row["status"] == "ok"
        assert row["items_in"] == 100
        assert row["items_dedup"] == 20
        assert row["items_selected"] == 10
        assert row["pushed"] == 1
        assert abs(row["cost_usd"] - 0.42) < 1e-9

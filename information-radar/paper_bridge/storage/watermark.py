"""水位管理：每个来源的最后成功采集时间。

业务契约（幂等核心）：
- 仅在采集+处理+推送**全部成功后**才提交水位
- 水位只前进不后退：commit_if_newer 拒绝更旧的时间
- 重复执行同一天：runs 表已记录成功推送 → 不重发
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from loguru import logger


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class WatermarkStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, source_name: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT last_success_at FROM watermarks WHERE source_name=?", (source_name,)
        ).fetchone()
        if not row:
            return None
        return datetime.fromisoformat(row["last_success_at"])

    def commit(self, source_name: str, ts: datetime) -> None:
        """无条件提交水位（用于首次写入或显式推进）。"""
        ts_utc = _to_utc(ts)
        self.conn.execute(
            """
            INSERT INTO watermarks (source_name, last_success_at, updated_at)
            VALUES (?,?,?)
            ON CONFLICT(source_name) DO UPDATE SET
                last_success_at=excluded.last_success_at,
                updated_at=excluded.updated_at
            """,
            (source_name, ts_utc.isoformat(), datetime.now(UTC).isoformat()),
        )
        self.conn.commit()
        logger.info("watermark committed: {} -> {}", source_name, ts_utc.isoformat())

    def commit_if_newer(self, source_name: str, ts: datetime) -> bool:
        """仅当 ts 比当前水位更新时才提交。返回是否实际提交。

        这是水位**只前进不后退**的保证：失败回退不会让水位倒退。
        """
        ts_utc = _to_utc(ts)
        current = self.get(source_name)
        if current is not None and ts_utc <= _to_utc(current):
            logger.debug(
                "watermark not advanced (older/equal): {} current={} proposed={}",
                source_name,
                current.isoformat(),
                ts_utc.isoformat(),
            )
            return False
        self.commit(source_name, ts_utc)
        return True

    def _get_all_names(self) -> list[str]:
        """返回所有已记录水位的来源名（健康检查用）。"""
        rows = self.conn.execute("SELECT source_name FROM watermarks").fetchall()
        return [r["source_name"] for r in rows]


class RunStore:
    """运行批次记录：幂等推送的依据。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def start_run(self, run_id: str, started_at: datetime) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, status) VALUES (?,?, 'running')",
            (run_id, started_at.isoformat()),
        )
        self.conn.commit()

    def finish_run(
        self,
        run_id: str,
        finished_at: datetime,
        status: str,
        stats: dict[str, int | float | str | None] | None = None,
    ) -> None:
        stats = stats or {}
        self.conn.execute(
            """
            UPDATE runs SET finished_at=?, status=?,
                items_in=?, items_dedup=?, items_filtered=?, items_selected=?,
                pushed=?, cost_usd=?, error=?
            WHERE run_id=?
            """,
            (
                finished_at.isoformat(),
                status,
                stats.get("items_in", 0),
                stats.get("items_dedup", 0),
                stats.get("items_filtered", 0),
                stats.get("items_selected", 0),
                stats.get("pushed", 0),
                stats.get("cost_usd", 0.0),
                stats.get("error"),
                run_id,
            ),
        )
        self.conn.commit()

    def is_pushed(self, run_id: str) -> bool:
        """该 run 是否已成功推送（幂等：已推送则不重发）。"""
        row = self.conn.execute(
            "SELECT pushed FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row:
            return False
        return bool(row["pushed"])

    def mark_pushed(self, run_id: str, channel: str, items_count: int) -> None:
        import uuid

        push_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO push_log (push_id, run_id, pushed_at, channel, items_count, status) VALUES (?,?,?,?,?, 'ok')",
            (push_id, run_id, now, channel, items_count),
        )
        self.conn.execute("UPDATE runs SET pushed=1, push_run_id=? WHERE run_id=?", (push_id, run_id))
        self.conn.commit()
        logger.info("run {} marked pushed via {} ({} items)", run_id, channel, items_count)

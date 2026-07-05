"""SQLite 数据库初始化与连接管理。

三张表：
- items：去重后的条目（含四键索引，用于跨批次去重）
- watermarks：每个来源的最后成功水位（仅成功后才提交）
- runs：每次运行的批次记录（幂等推送依据）
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from paper_bridge.models import Item
from paper_bridge.pipeline.dedupe import title_fingerprint

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    doi TEXT,
    arxiv_id TEXT,
    title_fingerprint INTEGER,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_doi ON items(doi);
CREATE INDEX IF NOT EXISTS idx_items_arxiv ON items(arxiv_id);
CREATE INDEX IF NOT EXISTS idx_items_url ON items(url);
CREATE INDEX IF NOT EXISTS idx_items_fp ON items(title_fingerprint);

CREATE TABLE IF NOT EXISTS watermarks (
    source_name TEXT PRIMARY KEY,
    last_success_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    items_in INTEGER DEFAULT 0,
    items_dedup INTEGER DEFAULT 0,
    items_filtered INTEGER DEFAULT 0,
    items_selected INTEGER DEFAULT 0,
    pushed INTEGER DEFAULT 0,
    push_run_id TEXT,
    cost_usd REAL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS push_log (
    push_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    pushed_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    items_count INTEGER DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT
);
"""


def _to_signed_64(v: int | None) -> int | None:
    """无符号 64 位 → 有符号 64 位（SQLite INTEGER 是有符号的）。"""
    if v is None:
        return None
    if v >= 2**63:
        return v - 2**64
    return v


def _to_unsigned_64(v: int | None) -> int | None:
    """有符号 64 位 → 无符号 64 位（还原 simhash 原值用于海明距离）。"""
    if v is None:
        return None
    if v < 0:
        return v + 2**64
    return v


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    logger.info("db initialized: schema ready")


def connect(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def insert_item(conn: sqlite3.Connection, item: Item, first_seen_at: datetime | None = None) -> int | None:
    """插入条目。若四键任一已存在则跳过（跨批次去重），返回 id（None=已存在）。"""
    seen = first_seen_at or datetime.utcnow()
    keys = item.dedup_keys()
    fp = title_fingerprint(item.title)
    # 跨批次查重：四键任一命中即已存在
    existing = _find_existing(conn, keys, fp)
    if existing is not None:
        logger.debug("item already exists (id={}): {}", existing, item.title[:60])
        return None
    cur = conn.execute(
        """
        INSERT INTO items (source_type, source_name, title, url, doi, arxiv_id,
                           title_fingerprint, published_at, first_seen_at, raw_json)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            item.source_type,
            item.source_name,
            item.title,
            item.url,
            keys["doi"],
            keys["arxiv_id"],
            _to_signed_64(fp),
            item.published_at.isoformat() if item.published_at else None,
            seen.isoformat(),
            json.dumps(item.raw, ensure_ascii=False, default=str),
        ),
    )
    conn.commit()
    logger.debug("inserted item id={} title={}", cur.lastrowid, item.title[:60])
    return cur.lastrowid


def _find_existing(conn: sqlite3.Connection, keys: dict[str, Any], fp: int | None) -> int | None:
    if keys["doi"]:
        row = conn.execute("SELECT id FROM items WHERE doi=? LIMIT 1", (keys["doi"],)).fetchone()
        if row:
            return row["id"]
    if keys["arxiv_id"]:
        row = conn.execute(
            "SELECT id FROM items WHERE arxiv_id=? LIMIT 1", (keys["arxiv_id"],)
        ).fetchone()
        if row:
            return row["id"]
    if keys["url"]:
        row = conn.execute("SELECT id FROM items WHERE url=? LIMIT 1", (keys["url"],)).fetchone()
        if row:
            return row["id"]
    if fp is not None:
        # 标题指纹：海明距离 ≤ 阈值
        from paper_bridge.pipeline.dedupe import HAMMING_THRESHOLD, hamming

        rows = conn.execute("SELECT id, title_fingerprint FROM items WHERE title_fingerprint IS NOT NULL").fetchall()
        for r in rows:
            stored_fp = _to_unsigned_64(r["title_fingerprint"])
            if stored_fp is not None and hamming(fp, stored_fp) <= HAMMING_THRESHOLD:
                return r["id"]
    return None


def item_exists(conn: sqlite3.Connection, item: Item) -> bool:
    keys = item.dedup_keys()
    fp = title_fingerprint(item.title)
    return _find_existing(conn, keys, fp) is not None

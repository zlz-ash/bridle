"""pytest 共享夹具。"""
from __future__ import annotations

import sqlite3
import sys

import pytest
from loguru import logger

from paper_bridge.storage.db import init_db


@pytest.fixture(autouse=True)
def _quiet_logging():
    """测试时只保留控制台 WARNING 级别，不写文件，避免污染测试目录。"""
    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="{level} | {message}")
    yield


@pytest.fixture
def db_conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield conn
    conn.close()

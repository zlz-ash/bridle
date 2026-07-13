"""itsdangerous 结构层地图测试夹具。

这个文件只负责准备可复用的结构层输入：
- #11 用真实扫描结果和独立 JSON 地图做断言；
- #12 以后直接读取这份独立 JSON 地图作为语义层输入。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bridle.features.project_map import store as store_module
from bridle.features.project_map.store import ProjectPlanStore

REPO_ROOT = Path(__file__).resolve().parents[4]
ITSDANGEROUS_FIXTURE = REPO_ROOT / "external-fixtures" / "itsdangerous-2.2.0"
ITSDANGEROUS_STRUCTURE_MAP = (
    Path(__file__).resolve().parent / "fixtures" / "itsdangerous_structure_map.json"
)
COPY_IGNORE_PATTERNS = (
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "__pycache__",
)
PENDING_SEMANTIC_METADATA = {
    "semantic_scan_status": "pending",
    "semantic_scan_run_id": "",
    "semantic_scan_interrupted": "0",
    "semantic_scan_processed": "0",
    "semantic_scan_routed": "0",
    "semantic_scan_deferred": "0",
    "semantic_scan_remaining": "0",
}


def copy_itsdangerous_fixture(test_workspace: Path) -> Path:
    """前置：复制 pinned 源码库到隔离工作区，避免测试过程改到原始 fixture。"""
    assert ITSDANGEROUS_FIXTURE.is_dir(), (
        "Missing external fixture: expected external-fixtures/itsdangerous-2.2.0 "
        "checked out at tag 2.2.0."
    )
    target = test_workspace / "itsdangerous-2.2.0"
    shutil.copytree(
        ITSDANGEROUS_FIXTURE,
        target,
        ignore=shutil.ignore_patterns(*COPY_IGNORE_PATTERNS),
    )
    return target


def prepare_structure_scan_store(store: ProjectPlanStore) -> None:
    """前置：只创建地图数据库，不调用 initialize()，避免自动进入语义层。"""
    store.database_path.parent.mkdir(parents=True, exist_ok=True)
    with store._connect() as connection:
        connection.executescript(store_module._SCHEMA)
        store._initialize_metadata(connection)
        store._migrate_schema(connection)


def load_expected_structure_map() -> dict[str, Any]:
    """输入：读取独立结构层地图；它不是 #11 的输出，后续 #12 也应读这份文件。"""
    return json.loads(ITSDANGEROUS_STRUCTURE_MAP.read_text(encoding="utf-8"))


def read_structure_map(store: ProjectPlanStore) -> dict[str, Any]:
    """读取结构层地图并规范化随机字段，输出可和独立 JSON 稳定比较。"""
    return {
        "fixture": {
            "name": "itsdangerous",
            "version": "2.2.0",
            "source": "external-fixtures/itsdangerous-2.2.0",
        },
        "entities": sorted(_all_code_entities(store), key=_entity_order_key),
        "relations": _all_code_relations(store),
        "blind_spots": _all_blind_spots(store),
    }


def seed_store_from_structure_map(
    store: ProjectPlanStore,
    structure_map: dict[str, Any],
) -> None:
    """#12 前置：把独立结构地图写入 DB，作为语义层输入，而不是复用 #11 的运行结果。"""
    prepare_structure_scan_store(store)
    with store._connect() as connection:
        store._replace_code_map(
            connection,
            structure_map["entities"],
            structure_map["relations"],
        )
        connection.execute("DELETE FROM map_blind_spots")
        for spot in structure_map["blind_spots"]:
            connection.execute(
                "INSERT INTO map_blind_spots(id, kind, file_path, range, detail, source, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    spot["id"],
                    spot["kind"],
                    spot["file_path"],
                    json.dumps(spot.get("range") or {}, ensure_ascii=False),
                    json.dumps(spot.get("detail") or {}, ensure_ascii=False),
                    spot["source"],
                    spot["status"],
                ),
            )
        store._set_map_status(
            connection,
            "structure_ready",
            reason="fixture_structure_map_loaded",
        )
        for key, value in PENDING_SEMANTIC_METADATA.items():
            store._set_metadata(connection, key, value)


@pytest.fixture
def itsdangerous_project(test_workspace: Path) -> Iterator[Path]:
    """前置：提供隔离源码项目；后置清理由 test_workspace fixture 统一完成。"""
    project_root = copy_itsdangerous_fixture(test_workspace)
    yield project_root
    # 后置：这里不手动删除目录，避免和全局 test_workspace 生命周期重复清理。


@pytest.fixture
def expected_itsdangerous_structure_map() -> dict[str, Any]:
    """前置输入：加载人工固定下来的结构层地图，#11 和 #12 都只能依赖它。"""
    return load_expected_structure_map()


@pytest.fixture
def generated_itsdangerous_structure_store(
    itsdangerous_project: Path,
) -> Iterator[ProjectPlanStore]:
    """前置：真实跑结构层扫描；后置仍由 test_workspace 回收 DB 和源码副本。"""
    store = ProjectPlanStore(itsdangerous_project, project_id="itsdangerous-fixture")
    prepare_structure_scan_store(store)
    result = store.rescan_structure_only()
    assert result["scan_status"] == "structure_ready"
    assert result["can_chat"] is False
    _assert_store_stops_at_structure_layer(store)
    yield store
    # 后置：ProjectPlanStore 不持有长连接，数据库文件随 test_workspace 删除。


@pytest.fixture
def generated_itsdangerous_structure_map(
    generated_itsdangerous_structure_store: ProjectPlanStore,
) -> dict[str, Any]:
    """测试输入：读取本次真实扫描得到的结构层地图，只用于 #11 当前断言。"""
    return read_structure_map(generated_itsdangerous_structure_store)


def _assert_store_stops_at_structure_layer(store: ProjectPlanStore) -> None:
    """前置断言：结构层扫描完成后，语义层必须保持未开始。"""
    assert store.semantic_scan_status()["semantic_scan_status"] == "pending"
    assert store.list_semantic_annotations(limit=1)["items"] == []


def _all_code_entities(store: ProjectPlanStore) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = store.list_code_entities(cursor=cursor, limit=200)
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return items


def _all_code_relations(store: ProjectPlanStore) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = store.list_code_relations(cursor=cursor, limit=200)
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return sorted(items, key=_stable_json_key)


def _all_blind_spots(store: ProjectPlanStore) -> list[dict[str, Any]]:
    with store._connect() as connection:
        rows = connection.execute(
            "SELECT kind, file_path, range, detail, source, status "
            "FROM map_blind_spots ORDER BY file_path, kind, detail"
        ).fetchall()
    spots: list[dict[str, Any]] = []
    for row in rows:
        range_payload = json.loads(row["range"] or "{}")
        detail = json.loads(row["detail"] or "{}")
        spot = {
            "kind": str(row["kind"]),
            "file_path": str(row["file_path"]),
            "range": range_payload,
            "detail": detail,
            "source": str(row["source"]),
            "status": str(row["status"]),
        }
        spot["id"] = _stable_blind_spot_id(spot)
        spots.append(spot)
    return sorted(spots, key=_stable_json_key)


def _stable_blind_spot_id(spot: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "kind": spot["kind"],
            "file_path": spot["file_path"],
            "range": spot["range"],
            "detail": spot["detail"],
            "source": spot["source"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"blind-fixture-{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:16]}"


def _stable_json_key(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _entity_order_key(item: dict[str, Any]) -> tuple[str, str]:
    return str(item["path"]), str(item["id"])

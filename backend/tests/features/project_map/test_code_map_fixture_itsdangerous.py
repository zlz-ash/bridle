"""Issue #11：itsdangerous 结构层地图门禁。

这个用例只检测代码地图的结构层：
- 输入：独立维护的 itsdangerous 结构层地图 JSON；
- 执行：对 pinned itsdangerous 源码真实跑结构层扫描；
- 断言：扫描结果必须和独立 JSON 完全一致；
- 边界：本用例的运行输出不能作为 #12 语义层用例输入。
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from bridle.features.project_map.store import ProjectPlanStore

pytest_plugins = ("tests.features.project_map.itsdangerous_structure_fixture",)
pytestmark = pytest.mark.usefixtures("test_workspace")

EXPECTED_STRUCTURE_COUNTS = {
    "entities": 144,
    "relations": 232,
    "blind_spots": 28,
}


@pytest.fixture
def issue_11_structure_map_case(
    generated_itsdangerous_structure_store: ProjectPlanStore,
    generated_itsdangerous_structure_map: dict[str, Any],
    expected_itsdangerous_structure_map: dict[str, Any],
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """前置：准备 #11 的真实扫描结果和独立断言输入，并确认当前只停在结构层。"""
    assert _map_summary(expected_itsdangerous_structure_map) == EXPECTED_STRUCTURE_COUNTS
    assert _map_summary(generated_itsdangerous_structure_map) == EXPECTED_STRUCTURE_COUNTS
    _assert_structure_only_store(generated_itsdangerous_structure_store)

    yield generated_itsdangerous_structure_map, expected_itsdangerous_structure_map

    # 后置：再次确认测试没有触发语义层，也没有生成可被 #12 复用的运行态语义数据。
    _assert_structure_only_store(generated_itsdangerous_structure_store)


def test_itsdangerous_structure_map_matches_independent_fixture(
    issue_11_structure_map_case: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    """测试：真实结构层扫描结果必须匹配独立结构地图，#12 不能复用本测试输出。"""
    generated_itsdangerous_structure_map, expected_itsdangerous_structure_map = (
        issue_11_structure_map_case
    )

    # 测试：先比较摘要，失败时能快速判断是实体、关系还是 blind spot 漂移。
    assert _map_summary(generated_itsdangerous_structure_map) == _map_summary(
        expected_itsdangerous_structure_map
    )

    # 测试：完整比对独立结构地图；这里不把扫描结果写出给 #12 当输入。
    assert generated_itsdangerous_structure_map == expected_itsdangerous_structure_map


def _map_summary(structure_map: dict[str, Any]) -> dict[str, int]:
    return {
        "entities": len(structure_map["entities"]),
        "relations": len(structure_map["relations"]),
        "blind_spots": len(structure_map["blind_spots"]),
    }


def _assert_structure_only_store(store: ProjectPlanStore) -> None:
    """断言：#11 只完成结构层，不允许提前生成语义层状态。"""
    assert store.semantic_scan_status()["semantic_scan_status"] == "pending"
    assert store.list_semantic_annotations(limit=1)["items"] == []

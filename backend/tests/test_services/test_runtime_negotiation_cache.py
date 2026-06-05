"""In-process runtime negotiation cache (60s TTL)."""
from __future__ import annotations

import time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.schemas.plan import PlanImportSchema, PlanPatchSchema
from bridle.services.complexity_negotiation_service import (
    clear_runtime_negotiation_cache,
    get_runtime_negotiation_cache,
    set_runtime_negotiation_cache,
    _RUNTIME_CACHE_TTL_SECONDS,
    _RUNTIME_NEGOTIATION_CACHE,
)
from bridle.services.plan_service import PlanService


def _plan_payload(**overrides) -> dict:
    base = {
        "goal": "Test plan",
        "nodes": [
            {
                "id": "n1",
                "title": "Node",
                "goal": "Do something with clear acceptance criteria here",
                "node_type": "code_change",
                "depends_on": [],
                "files": ["src/main.py"],
                "tests": ["pytest tests/"],
                "metrics": {},
                "constraints": {"c": True},
                "review_checks": [],
                "expected_outputs": {},
                "estimated_minutes": 60,
            }
        ],
    }
    base.update(overrides)
    return base


def test_cache_hit_and_miss() -> None:
    clear_runtime_negotiation_cache()
    set_runtime_negotiation_cache("p1", {"renegotiated": True, "action": "expand"})
    assert get_runtime_negotiation_cache("p1") == {"renegotiated": True, "action": "expand"}
    assert get_runtime_negotiation_cache("p2") is None


def test_cache_expires_after_ttl(monkeypatch) -> None:
    clear_runtime_negotiation_cache("p1")
    set_runtime_negotiation_cache("p1", {"renegotiated": False})
    ts, _ = _RUNTIME_NEGOTIATION_CACHE["p1"]
    monkeypatch.setattr(
        time,
        "monotonic",
        lambda: ts + _RUNTIME_CACHE_TTL_SECONDS + 1,
    )
    assert get_runtime_negotiation_cache("p1") is None


@pytest.mark.asyncio
async def test_patch_current_clears_cache(db: AsyncSession) -> None:
    from bridle.models.task import TaskRecord

    clear_runtime_negotiation_cache()
    task = TaskRecord(title="Patch cache", status="planned")
    db.add(task)
    await db.flush()

    imported = await PlanService.import_plan(
        db,
        task.id,
        PlanImportSchema.model_validate(_plan_payload()),
    )
    plan_id = imported["plan_id"]
    set_runtime_negotiation_cache(plan_id, {"renegotiated": True, "action": "expand"})

    await PlanService.patch_current(
        db,
        PlanPatchSchema(update_nodes=[{"id": "n1", "title": "Renamed"}]),
    )

    assert get_runtime_negotiation_cache(plan_id) is None


@pytest.mark.asyncio
async def test_replace_plan_clears_both_caches(db: AsyncSession) -> None:
    from bridle.models.task import TaskRecord

    clear_runtime_negotiation_cache()
    task = TaskRecord(title="Replace cache", status="planned")
    db.add(task)
    await db.flush()

    first = await PlanService.import_plan(
        db,
        task.id,
        PlanImportSchema.model_validate(_plan_payload(goal="First")),
    )
    old_plan_id = first["plan_id"]
    set_runtime_negotiation_cache(old_plan_id, {"renegotiated": False})

    second = await PlanService.replace_plan(
        db,
        task.id,
        PlanImportSchema.model_validate(_plan_payload(goal="Second")),
    )
    new_plan_id = second["plan_id"]
    set_runtime_negotiation_cache(new_plan_id, {"renegotiated": True, "action": "merge"})

    await PlanService.replace_plan(
        db,
        task.id,
        PlanImportSchema.model_validate(_plan_payload(goal="Third")),
    )

    assert get_runtime_negotiation_cache(old_plan_id) is None
    assert get_runtime_negotiation_cache(new_plan_id) is None


@pytest.mark.asyncio
async def test_cache_cleared_on_plan_mutation(db: AsyncSession) -> None:
    from bridle.models.task import TaskRecord

    clear_runtime_negotiation_cache()
    task = TaskRecord(title="Import cache", status="planned")
    db.add(task)
    await db.flush()

    first = await PlanService.import_plan(
        db,
        task.id,
        PlanImportSchema.model_validate(_plan_payload(goal="First import")),
    )
    stale_plan_id = first["plan_id"]
    set_runtime_negotiation_cache(stale_plan_id, {"renegotiated": True})

    second = await PlanService.import_plan(
        db,
        task.id,
        PlanImportSchema.model_validate(_plan_payload(goal="Second import")),
    )

    assert get_runtime_negotiation_cache(stale_plan_id) is None
    assert get_runtime_negotiation_cache(second["plan_id"]) is None

"""Project-local plan patch and progressive read API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.deps import get_db
from bridle.features.project_map.patch_schemas import PlanPatchSchema
from bridle.features.project_map.plan_service import PlanService
from bridle.features.project_map.schemas import (
    ArbitrationResolveSchema,
    ExecutionRefreshSchema,
    InterfaceCandidateStatusSchema,
    ModuleCandidateStatusSchema,
)
from bridle.features.project_map.service import ProjectMapService

router = APIRouter(prefix="/projects/{project_id}/map", tags=["project-map"])


@router.patch("")
async def patch_map(
    project_id: str,
    data: PlanPatchSchema,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Apply the existing local node patch; project/schema input exits with changed IDs/change_seq."""
    return await PlanService.patch_current(db, project_id, data)


@router.get("/overview")
async def get_overview(project_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Read map summary; project ID input exits with roots, counts, scan state and watermark."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.overview()


@router.get("/children")
async def get_children(
    project_id: str,
    parent_id: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Page one hierarchy level; parent/cursor/limit input exits as stable child rows."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.children(parent_id=parent_id, cursor=cursor, limit=limit)


@router.get("/nodes/{node_id}")
async def get_node(project_id: str, node_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Read one plan node; project/node IDs input exit as structured fields and payload."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.get_node(node_id)


@router.get("/search")
async def search_nodes(
    project_id: str,
    query: str = Query(min_length=1, max_length=500),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Search project nodes; text/cursor/limit input exits as a bounded stable page."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.search(query, cursor=cursor, limit=limit)


@router.get("/subgraph/{node_id}")
async def get_subgraph(
    project_id: str,
    node_id: str,
    depth: int = Query(default=1, ge=0, le=5),
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Read a node neighborhood; center/depth/limit input exits with bounded nodes/edges."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.subgraph(node_id, depth=depth, limit=limit)


@router.get("/changes")
async def get_changes(
    project_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Read incremental events; watermark/limit input exits as ordered change_seq rows."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.changes(after_seq=after_seq, limit=limit)


@router.get("/path-slice")
async def get_path_slice(
    project_id: str,
    path: str = Query(min_length=1, max_length=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return entities, relations, and blind spots for one changed file path."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.path_slice(path)


@router.get("/code-relations")
async def list_code_relations(
    project_id: str,
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    kind: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Page code-map structural relations for semantic canvas edges."""
    store = await ProjectMapService.store_for(db, project_id)
    kinds = [kind] if kind else None
    return store.list_code_relations(cursor=cursor, limit=limit, kinds=kinds)


@router.get("/semantic-annotations")
async def list_semantic_annotations(
    project_id: str,
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Page AI semantic annotations linked to code entities."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.list_semantic_annotations(cursor=cursor, limit=limit)


@router.get("/code-entities")
async def list_code_entities(
    project_id: str,
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    kind: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Page semantic/code-map entities; optional kind filter (e.g. test)."""
    store = await ProjectMapService.store_for(db, project_id)
    page = store.list_code_entities(cursor=cursor, limit=limit)
    if kind:
        page["items"] = [item for item in page["items"] if item.get("kind") == kind]
    return page


@router.get("/blind-spots")
async def list_blind_spots(
    project_id: str,
    status: str = Query(default="open"),
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List map blind spots for bootstrap review and mapping-role seeds."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.map_blind_spots(status=status, max_nodes=limit)


@router.get("/boundaries")
async def list_boundaries(
    project_id: str,
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Top-N directory vs co-change conflicts and debt node hints."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.list_boundary_conflicts(limit=limit)


@router.post("/semantic-map/refresh")
async def refresh_semantic_map(project_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Regenerate module/interface candidates from deterministic structure evidence."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.refresh_semantic_map_candidates()


@router.get("/module-candidates")
async def list_module_candidates(
    project_id: str,
    status: str | None = None,
    include_files: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List module candidates; confirmed entries are execution-boundary eligible."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.list_module_candidates(status=status, include_files=include_files)


@router.post("/module-candidates/{candidate_id}/status")
async def set_module_candidate_status(
    project_id: str,
    candidate_id: str,
    data: ModuleCandidateStatusSchema,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Confirm/reject one module candidate; only confirmed candidates can become execution boundaries."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.set_module_candidate_status(candidate_id, status=data.status, actor=data.actor)


@router.get("/module-interface-candidates")
async def list_module_interface_candidates(
    project_id: str,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List interface candidates and generated mock metadata."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.list_module_interface_candidates(status=status)


@router.post("/module-interface-candidates/{candidate_id}/status")
async def set_module_interface_candidate_status(
    project_id: str,
    candidate_id: str,
    data: InterfaceCandidateStatusSchema,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Confirm/reject one interface candidate; confirmation publishes a declared module interface."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.set_module_interface_candidate_status(candidate_id, status=data.status, actor=data.actor)


@router.get("/interface-mocks")
async def list_interface_mock_artifacts(
    project_id: str,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List generated interface mock artifacts for container boundary checks."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.list_interface_mock_artifacts(status=status)


@router.get("/arbitration")
async def list_arbitration(project_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Read pending AI map objections; project input exits as human arbitration queue."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.list_arbitration_items()


@router.post("/arbitration/{objection_id}/resolve")
async def resolve_arbitration(
    project_id: str,
    objection_id: str,
    data: ArbitrationResolveSchema,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Resolve one AI objection; project/decision input exits with recomputed map readiness."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.resolve_objection(
        objection_id,
        decision=data.decision,
        resolution=data.resolution,
        actor=data.actor,
    )


@router.post("/execution-refresh")
async def record_execution_refresh(
    project_id: str,
    data: ExecutionRefreshSchema,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record execution completion; changed paths input exits after incremental map refresh."""
    store = await ProjectMapService.store_for(db, project_id)
    return store.record_execution_refresh(
        execution_node_id=data.execution_node_id,
        changed_paths=data.changed_paths,
        execution_summary=data.execution_summary,
        test_summary=data.test_summary,
    )


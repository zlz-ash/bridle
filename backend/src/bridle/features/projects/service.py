"""Project registration and `.bridle/plan.db` open lifecycle."""
from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.agent.runtime.project_registry import get_project_runtime_registry
from bridle.api.errors import ConflictError, NotFoundError, ValidationError
from bridle.features.project_map.store import ProjectPlanStore
from bridle.features.projects.schemas import ProjectReadSchema
from bridle.logging.facade import get_logging_facade
from bridle.models.project import ProjectRecord
from bridle.utils.datetime_util import utc_now_naive


class ProjectService:
    """Register canonical project paths; DB/path input exits as project history and local map state."""

    @staticmethod
    async def open_project(db: AsyncSession, raw_path: str) -> ProjectReadSchema:
        """Open or register one directory; path input returns one project and initializes plan.db."""
        started = time.perf_counter()
        root = Path(raw_path).expanduser().resolve()
        if not root.exists():
            raise NotFoundError(resource="project", message="Project path does not exist")
        if not root.is_dir():
            raise ValidationError(resource="project", message="Project path must be a directory")

        canonical = str(root)
        record = (
            await db.execute(select(ProjectRecord).where(ProjectRecord.path == canonical))
        ).scalar_one_or_none()
        if record is None:
            record = ProjectRecord(path=canonical, name=root.name, last_opened_at=utc_now_naive())
            db.add(record)
            await db.flush()
        else:
            record.last_opened_at = utc_now_naive()

        store = ProjectPlanStore(root, project_id=record.id)
        initialized = store.initialize()
        await db.commit()
        await db.refresh(record)
        await get_project_runtime_registry().ensure_started(record.id, root)
        get_logging_facade().info_event(
            "project_open",
            "completed",
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail={
                "project_id": record.id,
                "created": initialized["created"],
                "scan_status": initialized["scan_status"],
                "entity_count": initialized["entity_count"],
            },
        )
        return ProjectService._to_read(record, scan_status=initialized["scan_status"])

    @staticmethod
    async def list_projects(db: AsyncSession) -> list[ProjectReadSchema]:
        """List registered projects; DB input returns history ordered by latest open time."""
        records = (
            await db.execute(
                select(ProjectRecord).order_by(ProjectRecord.last_opened_at.desc(), ProjectRecord.id.desc())
            )
        ).scalars().all()
        return [ProjectService._to_read(record) for record in records]

    @staticmethod
    async def rescan_project(db: AsyncSession, project_id: str) -> dict:
        """Rescan one registered project; DB/ID input exits as refreshed local code-map status."""
        record = await ProjectService.get_record(db, project_id)
        root = Path(record.path)
        if not root.is_dir():
            raise ConflictError(
                resource="project",
                message="Project path is unavailable",
                error_code="project_unavailable_read_only",
            )
        store = ProjectPlanStore(root, project_id=record.id)
        store.initialize()
        result = store.rescan()
        if result.get("scan_status") == "structure_ready":
            readiness = store.run_semantic_scan()
            result = {"project_id": record.id, **readiness, "entity_count": store._count("code_entities")}
        else:
            result = {"project_id": record.id, **result}
        return result

    @staticmethod
    async def get_record(db: AsyncSession, project_id: str) -> ProjectRecord:
        """Load one project; DB/ID input returns ORM record or a not-found error."""
        record = (
            await db.execute(select(ProjectRecord).where(ProjectRecord.id == project_id))
        ).scalar_one_or_none()
        if record is None:
            raise NotFoundError(resource="project", details={"project_id": project_id})
        return record

    @staticmethod
    def _to_read(
        record: ProjectRecord,
        *,
        scan_status: str | None = None,
    ) -> ProjectReadSchema:
        """Serialize project state; record/status input returns availability and local scan state."""
        root = Path(record.path)
        status = scan_status
        readiness = {
            "can_chat": False,
            "can_edit_plan": False,
            "readiness_reason": "unavailable",
        }
        if status is None and (root / ".bridle" / "plan.db").is_file():
            overview = ProjectPlanStore(root, project_id=record.id).overview()
            status = overview["scan_status"]
            readiness = {
                "can_chat": overview["can_chat"],
                "can_edit_plan": overview["can_edit_plan"],
                "readiness_reason": overview["readiness_reason"],
            }
        elif status is not None:
            readiness = ProjectPlanStore(root, project_id=record.id).readiness(status)
        return ProjectReadSchema(
            id=record.id,
            path=record.path,
            name=record.name,
            available=root.is_dir(),
            scan_status=status or "unavailable",
            can_chat=readiness["can_chat"],
            can_edit_plan=readiness["can_edit_plan"],
            readiness_reason=readiness["readiness_reason"],
            last_opened_at=record.last_opened_at,
        )


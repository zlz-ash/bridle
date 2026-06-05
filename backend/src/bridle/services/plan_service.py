"""Plan service — business logic for the global current plan.

Responsibilities:
- Import plan (archive old, create new)
- Full replacement (PUT): archive + summary + new
- Partial update (PATCH): modify nodes without archiving
- current-plan.json file mirror
- plan-summary.json generation

Transaction boundary rules:
- DB is the primary truth source; files are mirrors.
- All mutations complete the DB transaction first.
- File writes happen after DB commit.
- If a file write fails, the DB state is still valid.
- Failed file writes are logged and retried on next read.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node import NodeRecord
from bridle.models.plan import PlanRecord
from bridle.models.run import RunRecord
from bridle.engine.interface_validator import InterfaceContractValidator
from bridle.engine.test_command_policy import TestCommandPolicy
from bridle.schemas.node import (
    ContainerPolicySchema,
    NodeReadSchema,
    pack_container_boundary,
    unpack_container_boundary,
)
from bridle.schemas.plan import (
    KeyNodeSummary,
    KeyTestResult,
    PlanImportSchema,
    PlanPatchSchema,
    PlanReadSchema,
    PlanSummarySchema,
)

logger = logging.getLogger("bridle")


class PlanService:
    # -----------------------------------------------------------------------
    # Import (POST)
    # -----------------------------------------------------------------------

    @staticmethod
    async def import_plan(db: AsyncSession, task_id: str, data: PlanImportSchema) -> dict:
        """Import a strict JSON plan as the global current plan.

        If a plan is already active, archive it (and all its nodes) before
        creating the new one.  Run/evidence/log records are preserved.
        """
        PlanService._validate_interface_contracts(data)
        PlanService._validate_test_commands(data)
        complexity_validation = await PlanService._ensure_import_complexity(data)

        current = await PlanService.get_current(db)
        old_plan_id = current.id if current is not None else None

        await PlanService._archive_current_plan(db)

        plan = PlanRecord(
            task_id=task_id,
            goal=data.goal,
            aggregate_files=[item.model_dump() for item in data.aggregate_files],
            status="active",
        )
        db.add(plan)
        await db.flush()

        nodes = await PlanService._create_nodes(db, plan.id, data)
        await db.commit()
        await db.refresh(plan)

        from bridle.services.complexity_negotiation_service import clear_runtime_negotiation_cache

        clear_runtime_negotiation_cache(old_plan_id)
        clear_runtime_negotiation_cache(plan.id)

        await PlanService._refresh_current_plan_file(db, plan.id)

        return {
            "plan_id": plan.id,
            "task_id": task_id,
            "goal": data.goal,
            "aggregate_files": [item.model_dump() for item in data.aggregate_files],
            "status": plan.status,
            "nodes": [n.model_dump() for n in nodes],
            "complexity_validation": complexity_validation,
        }

    # -----------------------------------------------------------------------
    # Full replacement (PUT)
    # -----------------------------------------------------------------------

    @staticmethod
    async def replace_plan(db: AsyncSession, task_id: str, data: PlanImportSchema) -> dict:
        """Full replacement of the current plan.

        1. Generate plan-summary.json for the old plan
        2. Archive the old plan + nodes
        3. Create new plan + nodes
        4. Write current-plan.json
        """
        summary = await PlanService._generate_summary(db)
        if summary is not None:
            PlanService._write_plan_summary_file(summary)

        PlanService._validate_interface_contracts(data)
        PlanService._validate_test_commands(data)
        complexity_validation = await PlanService._ensure_import_complexity(data)

        current = await PlanService.get_current(db)
        old_plan_id = current.id if current is not None else None

        await PlanService._archive_current_plan(db)

        plan = PlanRecord(
            task_id=task_id,
            goal=data.goal,
            aggregate_files=[item.model_dump() for item in data.aggregate_files],
            status="active",
        )
        db.add(plan)
        await db.flush()

        nodes = await PlanService._create_nodes(db, plan.id, data)
        await db.commit()
        await db.refresh(plan)

        from bridle.services.complexity_negotiation_service import clear_runtime_negotiation_cache

        clear_runtime_negotiation_cache(old_plan_id)
        clear_runtime_negotiation_cache(plan.id)

        await PlanService._refresh_current_plan_file(db, plan.id)

        result = {
            "plan_id": plan.id,
            "task_id": task_id,
            "goal": data.goal,
            "aggregate_files": [item.model_dump() for item in data.aggregate_files],
            "status": plan.status,
            "nodes": [n.model_dump() for n in nodes],
        }
        if summary is not None:
            result["replaced_summary"] = summary.model_dump()
        result["complexity_validation"] = complexity_validation
        return result

    # -----------------------------------------------------------------------
    # Partial update (PATCH)
    # -----------------------------------------------------------------------

    @staticmethod
    async def patch_current(db: AsyncSession, data: PlanPatchSchema) -> dict:
        """Partial update of the current plan without archiving.

        Supports: update_nodes, add_nodes, remove_node_ids, replace_dependencies.
        After changes, re-validates dependencies and rejects cycles.

        All mutations and validation run within a single try/except block.
        If validation fails, the transaction is rolled back so no partial
        changes land in the DB or file mirror.
        """
        current = await PlanService.get_current(db)
        if current is None:
            raise ValueError("No active plan")

        try:
            for node_id in data.remove_node_ids:
                result = await db.execute(
                    select(NodeRecord).where(
                        NodeRecord.plan_node_id == node_id,
                        NodeRecord.plan_id == current.id,
                    )
                )
                node = result.scalar_one_or_none()
                if node is not None:
                    node.status = "archived"
                    await PlanService._remove_from_dependencies(db, current.id, node_id)

            await db.flush()

            for upd in data.update_nodes:
                result = await db.execute(
                    select(NodeRecord).where(
                        NodeRecord.plan_node_id == upd.id,
                        NodeRecord.plan_id == current.id,
                    )
                )
                node = result.scalar_one_or_none()
                if node is not None:
                    if upd.node_type is not None:
                        node.node_type = upd.node_type
                    if upd.title is not None:
                        node.title = upd.title
                    if upd.goal is not None:
                        node.goal = upd.goal
                    if upd.tests is not None:
                        node.tests = upd.tests
                    if upd.metrics is not None:
                        node.metrics = upd.metrics
                    if upd.constraints is not None:
                        node.constraints = upd.constraints
                    if upd.review_checks is not None:
                        node.review_checks = upd.review_checks
                    if upd.expected_outputs is not None:
                        node.expected_outputs = upd.expected_outputs
                    if upd.interfaces is not None:
                        node.interfaces = upd.interfaces.model_dump()
                    if any(
                        value is not None
                        for value in (
                            upd.read_set,
                            upd.write_set,
                            upd.readonly_context,
                            upd.conflict_contributions,
                            upd.container_policy,
                        )
                    ):
                        node.constraints = PlanService._merge_boundary_update(node.constraints, upd)

                    if upd.node_type is not None:
                        block_reason = PlanService._check_node_validity(node)
                        if block_reason:
                            node.status = "blocked"
                        elif node.status == "blocked":
                            node.status = "pending"

            await db.flush()

            result = await db.execute(
                select(NodeRecord)
                .where(NodeRecord.plan_id == current.id, NodeRecord.status != "archived")
                .order_by(NodeRecord.order.desc())
                .limit(1)
            )
            last_node = result.scalar_one_or_none()
            next_order = (last_node.order + 1) if last_node else 0

            for i, node_data in enumerate(data.add_nodes):
                node = PlanService._node_record_from_import(
                    current.id,
                    next_order + i,
                    node_data,
                )
                db.add(node)

            await db.flush()

            for dep_rep in data.replace_dependencies:
                result = await db.execute(
                    select(NodeRecord).where(
                        NodeRecord.plan_node_id == dep_rep.node_id,
                        NodeRecord.plan_id == current.id,
                    )
                )
                node = result.scalar_one_or_none()
                if node is not None:
                    node.depends_on = dep_rep.depends_on

            await db.flush()

            await PlanService._validate_graph(db, current.id)
            await PlanService._validate_interfaces_for_plan(db, current.id)

            await db.commit()

            from bridle.services.complexity_negotiation_service import clear_runtime_negotiation_cache

            clear_runtime_negotiation_cache(current.id)

            await PlanService._refresh_current_plan_file(db, current.id)

            return await PlanService._get_current_plan_dict(db, current.id)

        except ValueError:
            await db.rollback()
            raise

    # -----------------------------------------------------------------------
    # Query
    # -----------------------------------------------------------------------

    @staticmethod
    async def get_current(db: AsyncSession) -> PlanReadSchema | None:
        """Return the workspace's single active plan, or None."""
        result = await db.execute(
            select(PlanRecord).where(PlanRecord.status == "active").limit(1)
        )
        plan = result.scalar_one_or_none()
        if plan is None:
            return None
        return PlanReadSchema.model_validate(plan)

    @staticmethod
    async def get_current_with_resync(db: AsyncSession) -> PlanReadSchema | None:
        """Return the active plan, resyncing current-plan.json if stale.

        If the JSON file mirror doesn't match the DB, rewrite it and
        log a plan_file_resynced event.
        """
        plan = await PlanService.get_current(db)
        if plan is None:
            return None

        await PlanService._resync_current_plan_file(db, plan.id)
        return plan

    @staticmethod
    async def get_summary(db: AsyncSession) -> dict | None:
        """Read the plan-summary.json file if it exists."""
        from bridle.config import get_config

        config = get_config()
        path = config.plan_summary_path
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # -----------------------------------------------------------------------
    # File mirror helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _write_current_plan_file(data: PlanImportSchema) -> None:
        """Write the current-plan.json file mirror.

        If the write fails, logs the error but does not raise —
        the DB is the primary truth source.
        """
        from bridle.config import get_config

        try:
            config = get_config()
            content = data.model_dump()
            config.current_plan_path.write_text(
                json.dumps(content, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            logger.exception(
                "Failed to write current-plan.json",
                extra={"action": "current_plan_write_failed", "status": "error"},
            )

    @staticmethod
    async def _refresh_current_plan_file(db: AsyncSession, plan_id: str) -> None:
        """Re-read the current plan from DB and write the file mirror.

        If the write fails, logs the error but does not raise —
        the DB is the primary truth source.
        """
        from bridle.config import get_config

        config = get_config()
        result = await db.execute(select(NodeRecord).where(NodeRecord.plan_id == plan_id, NodeRecord.status != "archived"))
        nodes = result.scalars().all()

        plan_result = await db.execute(select(PlanRecord).where(PlanRecord.id == plan_id))
        plan = plan_result.scalar_one()

        content: dict = {
            "goal": plan.goal,
            "aggregate_files": plan.aggregate_files or [],
            "nodes": [
                {
                    "id": n.plan_node_id,
                    "title": n.title,
                    "goal": n.goal,
                    "node_type": n.node_type,
                    "depends_on": n.depends_on,
                    "files": n.files,
                    "tests": n.tests,
                    "metrics": n.metrics,
                    "constraints": PlanService._read_clean_constraints(n.constraints),
                    "review_checks": n.review_checks,
                    "expected_outputs": n.expected_outputs,
                    "interfaces": n.interfaces,
                    **PlanService._read_boundary_fields(n.constraints),
                }
                for n in nodes
            ],
        }
        try:
            config.current_plan_path.write_text(
                json.dumps(content, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            logger.exception(
                "Failed to refresh current-plan.json from DB",
                extra={"action": "current_plan_refresh_failed", "status": "error", "detail": {"plan_id": plan_id}},
            )

    @staticmethod
    async def _resync_current_plan_file(db: AsyncSession, plan_id: str) -> None:
        """Check JSON mirror consistency and resync if stale.

        Compares the JSON file content with DB data. If they differ,
        rewrites the file from DB and logs a plan_file_resynced event.
        """
        from bridle.config import get_config

        config = get_config()
        path = config.current_plan_path

        result = await db.execute(select(NodeRecord).where(NodeRecord.plan_id == plan_id, NodeRecord.status != "archived"))
        nodes = result.scalars().all()

        plan_result = await db.execute(select(PlanRecord).where(PlanRecord.id == plan_id))
        plan = plan_result.scalar_one()

        expected = {
            "goal": plan.goal,
            "aggregate_files": plan.aggregate_files or [],
            "nodes": [
                {
                    "id": n.plan_node_id,
                    "title": n.title,
                    "goal": n.goal,
                    "node_type": n.node_type,
                    "depends_on": n.depends_on,
                    "files": n.files,
                    "tests": n.tests,
                    "metrics": n.metrics,
                    "constraints": PlanService._read_clean_constraints(n.constraints),
                    "review_checks": n.review_checks,
                    "expected_outputs": n.expected_outputs,
                    "interfaces": n.interfaces,
                    **PlanService._read_boundary_fields(n.constraints),
                }
                for n in nodes
            ],
        }

        expected_json = json.dumps(expected, indent=2, ensure_ascii=False)

        if path.exists():
            actual_json = path.read_text(encoding="utf-8")
            try:
                if json.loads(actual_json) == expected:
                    return
            except json.JSONDecodeError:
                pass
        else:
            actual_json = ""

        try:
            path.write_text(expected_json, encoding="utf-8")
        except Exception:
            logger.exception(
                "Failed to resync current-plan.json",
                extra={"action": "current_plan_resync_failed", "status": "error", "detail": {"plan_id": plan_id}},
            )
            return

        logger.info(
            "plan_file_resynced",
            extra={
                "plan_id": plan_id,
                "action": "plan_file_resynced",
                "status": "resynced",
                "detail": "current-plan.json resynced from DB",
            },
        )

    @staticmethod
    def _write_plan_summary_file(summary: PlanSummarySchema) -> None:
        """Write the plan-summary.json file.

        If the write fails, logs the error but does not raise —
        the new current plan is still valid.
        """
        from bridle.config import get_config

        try:
            config = get_config()
            config.plan_summary_path.write_text(
                json.dumps(summary.model_dump(), indent=2, default=str, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            logger.exception(
                "Failed to write plan-summary.json",
                extra={"action": "plan_summary_write_failed", "status": "error"},
            )

    # -----------------------------------------------------------------------
    # Interface contract validation
    # -----------------------------------------------------------------------

    @staticmethod
    def _validate_test_commands(data: PlanImportSchema) -> None:
        """Validate all node test commands against TestCommandPolicy."""
        for node in data.nodes:
            errors = TestCommandPolicy.validate_all(node.tests)
            if errors:
                raise ValueError("Test command policy failed: " + "; ".join(errors))

    @staticmethod
    def _validate_node_complexity(data: PlanImportSchema) -> list[dict]:
        from bridle.engine.node_complexity_policy import validate_plan_nodes

        return [item.to_dict() for item in validate_plan_nodes(data.nodes)]

    @staticmethod
    async def _ensure_import_complexity(data: PlanImportSchema) -> list[dict]:
        from bridle.services.complexity_negotiation_service import run_import_complexity_negotiation

        validations = await run_import_complexity_negotiation(data.nodes)
        return [item.to_dict() for item in validations]

    @staticmethod
    async def renegotiate_complexity(db: AsyncSession, plan_id: str) -> dict:
        """Run-time complexity negotiation (single round) and persist node changes."""
        from bridle.engine.node_complexity_policy import validate_plan_nodes
        from bridle.services.complexity_negotiation_service import (
            ComplexityNegotiationService,
            PlanComplexityFailedError,
            apply_negotiation_decision,
            get_runtime_negotiation_cache,
            set_runtime_negotiation_cache,
        )
        from bridle.logging.jsonl import log_event

        cached = get_runtime_negotiation_cache(plan_id)
        if cached is not None:
            log_event(
                "plan_negotiation_cache_hit",
                "completed",
                detail={"plan_id": plan_id},
            )
            return cached

        result = await db.execute(
            select(NodeRecord).where(
                NodeRecord.plan_id == plan_id,
                NodeRecord.status != "archived",
            )
        )
        records = list(result.scalars().all())
        if not records:
            raise ValueError("No nodes for plan")

        import_nodes = [PlanService._record_to_import_schema(r) for r in records]
        validations = validate_plan_nodes(import_nodes)
        failing = [v for v in validations if not v.ok]
        if not failing:
            payload = {"plan_id": plan_id, "renegotiated": False}
            set_runtime_negotiation_cache(plan_id, payload)
            return payload

        svc = ComplexityNegotiationService.default()
        decision = await svc.negotiate(
            plan_nodes=import_nodes,
            validation_issues=failing,
            round_index=0,
            max_rounds=1,
        )
        apply_negotiation_decision(import_nodes, decision)
        validations = validate_plan_nodes(import_nodes)
        still_failing = [v for v in validations if not v.ok]
        if still_failing:
            raise PlanComplexityFailedError(
                last_validations=still_failing,
                rounds_used=1,
            )

        await PlanService._sync_nodes_from_import_schemas(db, plan_id, import_nodes)
        await db.commit()
        await PlanService._refresh_current_plan_file(db, plan_id)
        log_event(
            "plan_renegotiated",
            "completed",
            detail={"plan_id": plan_id, "action": decision.action},
        )
        payload = {"plan_id": plan_id, "renegotiated": True, "action": decision.action}
        set_runtime_negotiation_cache(plan_id, payload)
        return payload

    @staticmethod
    def _record_to_import_schema(record: NodeRecord) -> NodeImportSchema:
        from bridle.schemas.node import NodeImportSchema, NodeInterfacesSchema

        constraints, boundary = unpack_container_boundary(record.constraints)
        policy_raw = boundary.get("container_policy") or {}
        metrics_dict = record.metrics if isinstance(record.metrics, dict) else {}
        estimate = (metrics_dict.get("complexity") or {}).get("estimate") or {}
        est_min = estimate.get("estimated_minutes")
        return NodeImportSchema(
            id=record.plan_node_id,
            title=record.title,
            goal=record.goal,
            node_type=record.node_type,  # type: ignore[arg-type]
            depends_on=list(record.depends_on or []),
            files=list(record.files or []),
            tests=list(record.tests or []),
            metrics=metrics_dict,
            constraints=constraints,
            review_checks=list(record.review_checks or []),
            expected_outputs=record.expected_outputs if isinstance(record.expected_outputs, dict) else {},
            interfaces=NodeInterfacesSchema.model_validate(record.interfaces or {}),
            read_set=list(boundary.get("read_set") or []),
            write_set=list(boundary.get("write_set") or []),
            readonly_context=list(boundary.get("readonly_context") or []),
            container_policy=ContainerPolicySchema.model_validate(policy_raw),
            estimated_minutes=int(est_min) if est_min is not None else None,
            acceptance_scope=estimate.get("acceptance_scope"),
        )

    @staticmethod
    async def _sync_nodes_from_import_schemas(
        db: AsyncSession,
        plan_id: str,
        import_nodes: list,
    ) -> None:
        """Persist negotiated nodes; skip running/completed records."""
        protected = frozenset({"running", "completed"})
        result = await db.execute(
            select(NodeRecord).where(
                NodeRecord.plan_id == plan_id,
                NodeRecord.status != "archived",
            )
        )
        existing = {n.plan_node_id: n for n in result.scalars().all()}
        new_ids = {n.id for n in import_nodes}

        for schema in import_nodes:
            record = existing.get(schema.id)
            if record is not None and record.status in protected:
                continue
            if record is None:
                order = max((n.order for n in existing.values()), default=-1) + 1
                db.add(PlanService._node_record_from_import(plan_id, order, schema))
                continue
            from bridle.engine.node_complexity_policy import validate_node_complexity

            record.title = schema.title
            record.goal = schema.goal
            record.node_type = schema.node_type
            record.depends_on = schema.depends_on
            record.files = schema.files
            record.tests = schema.tests
            record.metrics = PlanService._merge_node_metrics(schema, validate_node_complexity(schema).to_dict())
            record.constraints = PlanService._pack_node_constraints(schema)
            record.review_checks = schema.review_checks
            record.expected_outputs = schema.expected_outputs
            record.interfaces = schema.interfaces.model_dump()
            complexity = validate_node_complexity(schema).to_dict()
            record.status = "pending" if complexity.get("ok") else "blocked"

        for plan_node_id, record in existing.items():
            if plan_node_id in new_ids:
                continue
            if record.status in protected:
                continue
            if record.status in ("blocked", "pending"):
                record.status = "archived"

    @staticmethod
    def _merge_node_metrics(node_data, complexity: dict) -> dict | list:
        if isinstance(node_data.metrics, dict):
            metrics = dict(node_data.metrics)
        else:
            metrics = {}
        metrics["complexity"] = complexity
        return metrics

    @staticmethod
    def _validate_interface_contracts(data: PlanImportSchema) -> None:
        """Validate interface contracts across all nodes in an import/replace payload.

        Converts the Pydantic nodes to plain dicts and runs the validator.
        Raises ValueError if any contract violations are found.
        """
        node_dicts = [
            {
                "plan_node_id": n.id,
                "depends_on": n.depends_on,
                "interfaces": n.interfaces.model_dump(),
            }
            for n in data.nodes
        ]
        errors = InterfaceContractValidator.validate(node_dicts)
        if errors:
            raise ValueError("Interface contract validation failed: " + "; ".join(errors))

    @staticmethod
    async def _validate_interfaces_for_plan(db: AsyncSession, plan_id: str) -> None:
        """Validate interface contracts from the current DB state of a plan.

        Reads all active nodes from DB, builds dicts, and runs the validator.
        Raises ValueError if any contract violations are found.
        """
        result = await db.execute(
            select(NodeRecord).where(
                NodeRecord.plan_id == plan_id,
                NodeRecord.status != "archived",
            )
        )
        nodes = result.scalars().all()
        node_dicts = [
            {
                "plan_node_id": n.plan_node_id,
                "depends_on": n.depends_on,
                "interfaces": n.interfaces,
            }
            for n in nodes
        ]
        errors = InterfaceContractValidator.validate(node_dicts)
        if errors:
            raise ValueError("Interface contract validation failed: " + "; ".join(errors))

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    @staticmethod
    async def _create_nodes(db: AsyncSession, plan_id: str, data: PlanImportSchema) -> list[NodeReadSchema]:
        """Create nodes for a plan from import data.

        The NodeImportSchema.id (e.g. 'n1') is stored as plan_node_id
        so that PATCH operations can reference nodes by their plan-local ID.
        """
        nodes: list[NodeReadSchema] = []
        for i, node_data in enumerate(data.nodes):
            node = PlanService._node_record_from_import(plan_id, i, node_data)
            db.add(node)
            await db.flush()
            nodes.append(NodeReadSchema.model_validate(node))
        return nodes

    @staticmethod
    def _node_record_from_import(plan_id: str, order: int, node_data) -> NodeRecord:
        from bridle.engine.node_complexity_policy import validate_node_complexity

        complexity = validate_node_complexity(node_data).to_dict()
        node_status = "pending" if complexity.get("ok") else "blocked"
        return NodeRecord(
            plan_id=plan_id,
            plan_node_id=node_data.id,
            title=node_data.title,
            goal=node_data.goal,
            node_type=node_data.node_type,
            order=order,
            depends_on=node_data.depends_on,
            files=node_data.files,
            tests=node_data.tests,
            metrics=PlanService._merge_node_metrics(node_data, complexity),
            constraints=PlanService._pack_node_constraints(node_data),
            review_checks=node_data.review_checks,
            expected_outputs=node_data.expected_outputs,
            interfaces=node_data.interfaces.model_dump(),
            status=node_status,
        )

    @staticmethod
    def _check_node_validity(node: NodeRecord) -> str | None:
        """Check if a node meets the validity rules for its type.

        Returns a blocking reason string if invalid, None if valid.
        """
        if node.node_type == "micro":
            return None
        if not node.tests:
            return "Missing test definitions after type change"
        if not node.constraints:
            return "Missing constraint rules after type change"
        if node.node_type == "metric_validation" and not node.metrics:
            return "Missing metric definitions for metric_validation node"
        if node.node_type == "review_gate" and not node.review_checks:
            return "Missing review checks for review_gate node"
        return None

    @staticmethod
    async def _archive_current_plan(db: AsyncSession) -> None:
        """Archive the current active plan and all its nodes."""
        result = await db.execute(
            select(PlanRecord).where(PlanRecord.status == "active")
        )
        active_plans = result.scalars().all()

        for plan in active_plans:
            await db.execute(
                update(NodeRecord)
                .where(NodeRecord.plan_id == plan.id, NodeRecord.status != "archived")
                .values(status="archived")
            )
            plan.status = "archived"

        await db.flush()

    @staticmethod
    async def _remove_from_dependencies(db: AsyncSession, plan_id: str, removed_node_id: str) -> None:
        """Remove a node ID from all other nodes' depends_on lists in the plan."""
        result = await db.execute(
            select(NodeRecord).where(
                NodeRecord.plan_id == plan_id,
                NodeRecord.status != "archived",
            )
        )
        nodes = result.scalars().all()
        for node in nodes:
            if removed_node_id in node.depends_on:
                node.depends_on = [d for d in node.depends_on if d != removed_node_id]

    @staticmethod
    async def _validate_graph(db: AsyncSession, plan_id: str) -> None:
        """Validate the plan graph: check for unknown deps and cycles.

        Raises ValueError if validation fails.
        """
        result = await db.execute(
            select(NodeRecord).where(
                NodeRecord.plan_id == plan_id,
                NodeRecord.status != "archived",
            )
        )
        nodes = result.scalars().all()
        node_map = {n.plan_node_id: n for n in nodes}

        for node in nodes:
            for dep in node.depends_on:
                if dep not in node_map:
                    raise ValueError(f"Node {node.plan_node_id} depends on unknown node {dep}")

        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n.plan_node_id: WHITE for n in nodes}

        def dfs(node_key: str) -> bool:
            color[node_key] = GRAY
            node = node_map[node_key]
            for dep in node.depends_on:
                if dep not in color:
                    continue
                if color[dep] == GRAY:
                    return True
                if color[dep] == WHITE and dfs(dep):
                    return True
            color[node_key] = BLACK
            return False

        for node in nodes:
            if color[node.plan_node_id] == WHITE:
                if dfs(node.plan_node_id):
                    raise ValueError("Circular dependency detected in plan graph")

    @staticmethod
    async def _generate_summary(db: AsyncSession) -> PlanSummarySchema | None:
        """Generate a summary for the current plan before it gets archived."""
        result = await db.execute(
            select(PlanRecord).where(PlanRecord.status == "active").limit(1)
        )
        plan = result.scalar_one_or_none()
        if plan is None:
            return None

        n_result = await db.execute(
            select(NodeRecord).where(NodeRecord.plan_id == plan.id)
        )
        nodes = n_result.scalars().all()

        completed = [n for n in nodes if n.status == "completed"]
        failed = [n for n in nodes if n.status == "failed"]

        key_test_results: list[KeyTestResult] = []
        for node in nodes[:5]:
            r_result = await db.execute(
                select(RunRecord)
                .where(RunRecord.node_id == node.id)
                .order_by(RunRecord.started_at.desc())
                .limit(1)
            )
            run = r_result.scalar_one_or_none()
            if run is not None:
                key_test_results.append(KeyTestResult(
                    node_id=node.id,
                    node_title=node.title,
                    exit_code=run.exit_code,
                    duration_ms=run.duration_ms,
                ))

        return PlanSummarySchema(
            plan_id=plan.id,
            goal=plan.goal,
            task_id=plan.task_id,
            replaced_at=datetime.now(),
            final_status=plan.status,
            node_count=len(nodes),
            completed_count=len(completed),
            failed_count=len(failed),
            key_nodes=[KeyNodeSummary(id=n.plan_node_id, title=n.title, status=n.status, node_type=n.node_type) for n in nodes[:10]],
            key_test_results=key_test_results,
            key_metrics={},
        )

    @staticmethod
    async def _get_current_plan_dict(db: AsyncSession, plan_id: str) -> dict:
        """Build the full current plan response dict."""
        plan_result = await db.execute(select(PlanRecord).where(PlanRecord.id == plan_id))
        plan = plan_result.scalar_one()

        n_result = await db.execute(
            select(NodeRecord)
            .where(NodeRecord.plan_id == plan_id, NodeRecord.status != "archived")
            .order_by(NodeRecord.order)
        )
        nodes = [NodeReadSchema.model_validate(n) for n in n_result.scalars().all()]

        return {
            "plan_id": plan.id,
            "task_id": plan.task_id,
            "goal": plan.goal,
            "aggregate_files": plan.aggregate_files or [],
            "status": plan.status,
            "nodes": [n.model_dump() for n in nodes],
        }

    @staticmethod
    def _pack_node_constraints(node_data) -> dict | list:
        return pack_container_boundary(
            node_data.constraints,
            read_set=node_data.read_set,
            write_set=node_data.write_set,
            readonly_context=node_data.readonly_context,
            conflict_contributions=node_data.conflict_contributions,
            container_policy=node_data.container_policy,
        )

    @staticmethod
    def _read_boundary_fields(constraints: dict | list) -> dict:
        _clean_constraints, boundary = unpack_container_boundary(constraints)
        return {
            "read_set": boundary.get("read_set", []),
            "write_set": boundary.get("write_set", []),
            "readonly_context": boundary.get("readonly_context", []),
            "conflict_contributions": boundary.get("conflict_contributions", []),
            "container_policy": boundary.get("container_policy", {}),
        }

    @staticmethod
    def _read_clean_constraints(constraints: dict | list) -> dict | list:
        clean_constraints, _boundary = unpack_container_boundary(constraints)
        return clean_constraints

    @staticmethod
    def _merge_boundary_update(constraints: dict | list, update) -> dict | list:
        clean_constraints, boundary = unpack_container_boundary(constraints)
        if update.read_set is not None:
            boundary["read_set"] = update.read_set
        if update.write_set is not None:
            boundary["write_set"] = update.write_set
        if update.readonly_context is not None:
            boundary["readonly_context"] = update.readonly_context
        if update.conflict_contributions is not None:
            boundary["conflict_contributions"] = [
                item.model_dump() if hasattr(item, "model_dump") else item
                for item in update.conflict_contributions
            ]
        if update.container_policy is not None:
            policy = (
                update.container_policy
                if hasattr(update.container_policy, "model_dump")
                else ContainerPolicySchema(**update.container_policy)
            )
            boundary["container_policy"] = policy.model_dump()
        return pack_container_boundary(
            clean_constraints,
            read_set=boundary.get("read_set", []),
            write_set=boundary.get("write_set", []),
            readonly_context=boundary.get("readonly_context", []),
            conflict_contributions=[
                item if hasattr(item, "model_dump") else __import__(
                    "bridle.schemas.node", fromlist=["AggregateContributionSchema"]
                ).AggregateContributionSchema(**item)
                for item in boundary.get("conflict_contributions", [])
            ],
            container_policy=ContainerPolicySchema(**boundary.get("container_policy", {})),
        )

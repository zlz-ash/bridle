"""Node service — business logic for node queries and graph."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node import NodeRecord
from bridle.models.plan import PlanRecord
from bridle.events.bus import publish_event_safe
from bridle.schemas.node import NodeReadSchema

ALLOWED_NODE_STATUSES = frozenset({
    "pending",
    "ready",
    "blocked",
    "running",
    "completed",
    "failed",
    "failed_retryable",
    "missing_evidence",
    "needs_review",
    "needs_review_retryable",
    "archived",
})


class NodeService:
    @staticmethod
    async def get_by_id(db: AsyncSession, node_id: str) -> NodeReadSchema | None:
        """Get a node by ID — only returns non-archived nodes in an active plan."""
        result = await db.execute(
            select(NodeRecord)
            .join(PlanRecord, NodeRecord.plan_id == PlanRecord.id)
            .where(NodeRecord.id == node_id, PlanRecord.status == "active", NodeRecord.status != "archived")
        )
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return NodeReadSchema.model_validate(record)

    @staticmethod
    async def list_by_task(db: AsyncSession, task_id: str) -> list[NodeReadSchema]:
        """Get all active nodes for a task via its current plan."""
        result = await db.execute(
            select(NodeRecord)
            .join(PlanRecord, NodeRecord.plan_id == PlanRecord.id)
            .where(PlanRecord.task_id == task_id, PlanRecord.status == "active", NodeRecord.status != "archived")
            .order_by(NodeRecord.order)
        )
        return [NodeReadSchema.model_validate(r) for r in result.scalars().all()]

    @staticmethod
    async def get_graph(db: AsyncSession, task_id: str) -> dict:
        """Return a graph representation: nodes + edges (dependencies).

        Only includes nodes from the current active plan.
        If the task has no active plan, returns empty graph.

        Each edge includes an interface_contracts list showing what interfaces
        the target node consumes from the source node.
        """
        nodes = await NodeService.list_by_task(db, task_id)
        node_map = {n.plan_node_id: n for n in nodes}

        edges = []
        for node in nodes:
            for dep_id in node.depends_on:
                edge = {"source": dep_id, "target": node.plan_node_id}
                # Build interface_contracts: what does target consume from source?
                contracts = NodeService._build_edge_contracts(node, dep_id, node_map)
                if contracts:
                    edge["interface_contracts"] = contracts
                edges.append(edge)
        return {"nodes": [n.model_dump() for n in nodes], "edges": edges}

    @staticmethod
    def _build_edge_contracts(target_node, source_id: str, node_map: dict) -> list[dict]:
        """Build bidirectional interface contracts for a single dependency edge.

        Produces two directions:
        - source_to_target: what target consumes from source
        - target_to_source: what source consumes from target

        Each contract includes direction, consumer, provider, interface_name,
        fields, and endpoints.
        """
        target_ifaces = target_node.interfaces if isinstance(target_node.interfaces, dict) else {}
        target_consumes = target_ifaces.get("consumes", []) or []
        source_node = node_map.get(source_id)
        if source_node is None:
            return []
        source_ifaces = source_node.interfaces if isinstance(source_node.interfaces, dict) else {}
        source_exposes = source_ifaces.get("exposes", []) or []
        source_consumes = source_ifaces.get("consumes", []) or []
        target_exposes = target_ifaces.get("exposes", []) or []

        contracts = []

        # Direction 1: source_to_target — target consumes from source
        for consume in target_consumes:
            if consume.get("node_id") != source_id:
                continue
            iface_name = consume.get("interface_name", "")
            expose = None
            for exp in source_exposes:
                if exp.get("name") == iface_name:
                    expose = exp
                    break
            if expose is None:
                continue
            contracts.append({
                "direction": "source_to_target",
                "consumer": target_node.plan_node_id,
                "provider": source_id,
                "interface_name": iface_name,
                "fields": consume.get("fields", []),
                "endpoints": consume.get("endpoints", []),
            })

        # Direction 2: target_to_source — source consumes from target
        target_id = target_node.plan_node_id
        for consume in source_consumes:
            if consume.get("node_id") != target_id:
                continue
            iface_name = consume.get("interface_name", "")
            expose = None
            for exp in target_exposes:
                if exp.get("name") == iface_name:
                    expose = exp
                    break
            if expose is None:
                continue
            contracts.append({
                "direction": "target_to_source",
                "consumer": source_id,
                "provider": target_id,
                "interface_name": iface_name,
                "fields": consume.get("fields", []),
                "endpoints": consume.get("endpoints", []),
            })

        return contracts

    @staticmethod
    async def update_status(db: AsyncSession, node_id: str, status: str) -> NodeReadSchema | None:
        if status not in ALLOWED_NODE_STATUSES:
            raise ValueError(f"Unknown node status: {status}")
        result = await db.execute(select(NodeRecord).where(NodeRecord.id == node_id))
        record = result.scalar_one_or_none()
        if record is None:
            return None
        old_status = record.status
        record.status = status
        await db.commit()
        await db.refresh(record)
        if old_status != status:
            publish_event_safe(
                "node_status_changed",
                {
                    "node_id": record.id,
                    "plan_node_id": record.plan_node_id,
                    "old_status": old_status,
                    "new_status": status,
                },
            )
        return NodeReadSchema.model_validate(record)

    @staticmethod
    async def get_record_by_id(db: AsyncSession, node_id: str) -> NodeRecord | None:
        """Get the raw ORM record (for engine use). Does NOT filter by plan status."""
        result = await db.execute(select(NodeRecord).where(NodeRecord.id == node_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_accessible_context(db: AsyncSession, node_id: str) -> dict:
        """Return the interfaces context this node is allowed to access.

        Resolves each consume entry to the corresponding expose from the
        adjacent node. Returns a dict with accessible fields and endpoints.

        Adjacency is re-computed from the active plan at read time:
        only direct predecessors and successors are adjacent.
        Non-adjacent consumes return an error with no fields/endpoints.

        This is a reserved helper for future AgentGateway — no LLM calls.
        """
        node = await NodeService.get_by_id(db, node_id)
        if node is None:
            return {"node_id": node_id, "error": "Node not found"}

        interfaces = node.interfaces if isinstance(node.interfaces, dict) else {}
        consumes = interfaces.get("consumes", []) or []
        if not consumes:
            return {"node_id": node.plan_node_id, "accessible": []}

        result = await db.execute(
            select(NodeRecord)
            .join(PlanRecord, NodeRecord.plan_id == PlanRecord.id)
            .where(PlanRecord.status == "active", NodeRecord.status != "archived")
        )
        all_nodes = {n.plan_node_id: n for n in result.scalars().all()}

        # Build adjacency set: direct predecessors + direct successors
        adjacent: set[str] = set(node.depends_on or [])
        for nid, n in all_nodes.items():
            if node.plan_node_id in (n.depends_on or []):
                adjacent.add(nid)

        accessible = []
        for consume in consumes:
            target_id = consume.get("node_id", "")
            if target_id not in adjacent:
                accessible.append({
                    "node_id": target_id,
                    "interface_name": consume.get("interface_name", ""),
                    "error": "Not adjacent — cross-hop access denied",
                })
                continue

            target = all_nodes.get(target_id)
            if target is None:
                accessible.append({
                    "node_id": target_id,
                    "interface_name": consume.get("interface_name", ""),
                    "error": "Target node not found in active plan",
                })
                continue

            target_ifaces = target.interfaces if isinstance(target.interfaces, dict) else {}
            target_exposes = target_ifaces.get("exposes", []) or []

            expose = None
            for exp in target_exposes:
                if exp.get("name") == consume.get("interface_name"):
                    expose = exp
                    break

            if expose is None:
                accessible.append({
                    "node_id": target_id,
                    "interface_name": consume.get("interface_name", ""),
                    "error": "Requested interface not exposed",
                })
                continue

            req_fields = consume.get("fields", []) or []
            req_endpoints = consume.get("endpoints", []) or []

            resolved_fields = [f for f in expose.get("fields", []) or [] if f.get("name") in req_fields]
            resolved_endpoints = [e for e in expose.get("endpoints", []) or [] if e.get("name") in req_endpoints]

            accessible.append({
                "node_id": target_id,
                "interface_name": expose.get("name"),
                "fields": resolved_fields,
                "endpoints": resolved_endpoints,
            })

        return {"node_id": node.plan_node_id, "accessible": accessible}

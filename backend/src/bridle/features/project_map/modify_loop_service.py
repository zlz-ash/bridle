"""Modify loop: dispatch, consistency gate, drift detection, module interfaces."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bridle.api.errors import ConflictError, ValidationError

if TYPE_CHECKING:
    from bridle.agent.runtime.mailbox import AgentAddress
    from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
    from bridle.features.project_map.store import ProjectPlanStore

logger = logging.getLogger("bridle")
StageRunner = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

CONSISTENCY_GATE_ERROR = "consistency_gate_failed"
TDD_GATE_ERROR = "tdd_gate_failed"
DRIFT_STATUS = "drifted"

EXTENDED_NODE_STATUSES = frozenset(
    {
        "pending",
        "ready",
        "running",
        "completed",
        "failed",
        "blocked",
        "proposed",
        "ratified",
        "mapping",
        "executing",
        "verifying",
        "drifted",
    }
)

AUTO_ADOPT_CONFIDENCE = 0.9


class PlanNodeExecutionCoordinator:
    """Start durable node workflows in the background and forward terminal Mail once."""

    def __init__(
        self,
        store: ProjectPlanStore,
        mailbox: PersistentMailbox,
        *,
        owner: AgentAddress,
        stage_runner: StageRunner,
    ) -> None:
        self._store = store
        self._store.ensure_schema()
        self._mailbox = mailbox
        self._owner = owner
        self._stage_runner = stage_runner
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def execute_plan_node(self, node_id: str) -> dict[str, Any]:
        """Create/reuse durable waiting state, schedule work, and return without awaiting it."""
        execution = self._store.create_node_execution(
            node_id=node_id,
            owner_address=self._owner.to_uri(),
        )
        self._schedule(execution)
        return execution

    async def recover(self) -> list[str]:
        """Schedule every durable waiting execution after process restart."""
        recovered: list[str] = []
        for execution in self._store.list_active_node_executions():
            if self._schedule(execution):
                recovered.append(str(execution["execution_id"]))
        return recovered

    async def wait_for_idle(self) -> None:
        """Wait for currently scheduled workflow tasks; persistence remains authoritative."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()))

    def forward_completion_mail(self) -> int:
        """Idempotently forward pending terminal outbox rows to the persistent mailbox."""
        from bridle.agent.runtime.mailbox import AgentAddress, MailEnvelope

        forwarded = 0
        source = AgentAddress(self._owner.project_id, "node-workflow", 1)
        for item in self._store.list_pending_completion_outbox():
            envelope = MailEnvelope(
                message_id=item["message_id"],
                message_type="node-workflow-result",
                source=source,
                target=AgentAddress.parse(item["owner_address"]),
                payload=item["payload"],
            )
            result = self._mailbox.enqueue(envelope)
            if result.status in {"inserted", "existing"}:
                self._store.mark_completion_outbox_sent(item["wait_id"])
                forwarded += 1
                continue
            self._store.mark_completion_outbox_attempt(
                item["wait_id"],
                error_code=f"mailbox_{result.status}",
            )
        return forwarded

    def _schedule(self, execution: dict[str, Any]) -> bool:
        execution_id = str(execution["execution_id"])
        if execution["state"] != "waiting" or execution_id in self._tasks:
            return False
        task = asyncio.create_task(
            self._run(execution),
            name=f"node-workflow:{execution_id}",
        )
        self._tasks[execution_id] = task
        return True

    async def _run(self, execution: dict[str, Any]) -> None:
        execution_id = str(execution["execution_id"])
        try:
            result = await self._stage_runner(dict(execution))
            outcome = str(result.get("outcome") or "failed")
            phases = [str(value) for value in result.get("phases") or [] if str(value)]
            phase = phases[-1] if phases else str(execution["phase"])
            self._store.complete_execution(
                wait_id=str(execution["wait_id"]),
                outcome=outcome,
                result_ref=result.get("result_ref"),
                phase=phase,
            )
            logger.info(
                "node_workflow_finished",
                extra={
                    "action": "node_workflow_finished",
                    "status": outcome,
                    "node_id": execution["node_id"],
                    "detail": {
                        "execution_id": execution_id,
                        "wait_id": execution["wait_id"],
                        "phase": phase,
                    },
                },
            )
        except asyncio.CancelledError:
            self._store.complete_execution(
                wait_id=str(execution["wait_id"]),
                outcome="cancelled",
                result_ref=None,
            )
            raise
        except Exception as exc:
            self._store.complete_execution(
                wait_id=str(execution["wait_id"]),
                outcome="failed",
                result_ref=None,
            )
            logger.exception(
                "node_workflow_failed",
                extra={
                    "action": "node_workflow_failed",
                    "status": "failed",
                    "node_id": execution["node_id"],
                    "detail": {
                        "execution_id": execution_id,
                        "wait_id": execution["wait_id"],
                        "error_code": type(exc).__name__,
                    },
                },
            )
        finally:
            self._tasks.pop(execution_id, None)


class ModifyLoopService:
    """Plan-vs-semantic dispatch, dual gates, and drift."""

    @staticmethod
    def compare_plan_vs_semantic(
        connection,
        *,
        node_id: str,
        node_payload: dict[str, Any],
        code_entity_paths: set[str],
    ) -> bool:
        """Return True when code entities cover declared node files."""
        declared = set(node_payload.get("files") or [])
        if not declared:
            return True
        return declared.issubset(code_entity_paths)

    @staticmethod
    def list_divergent_nodes(connection) -> list[str]:
        """Nodes whose declared files are missing from the code map."""
        divergent: list[str] = []
        rows = connection.execute(
            "SELECT id, payload FROM plan_nodes WHERE archived = 0"
        ).fetchall()
        code_paths = {
            str(r["path"]).split("::", 1)[0]
            for r in connection.execute("SELECT path FROM code_entities").fetchall()
        }
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            declared = set(payload.get("files") or [])
            if declared and not declared.issubset(code_paths):
                divergent.append(str(row["id"]))
        return divergent

    @staticmethod
    def dispatch_child_agent(connection, *, node_id: str, target_role: str) -> dict[str, Any]:
        """Transition a node into mapping or executing for child agent work."""
        if target_role not in ("mapping", "executing"):
            raise ValidationError(
                resource="plan_node",
                message="Unsupported dispatch target role",
                details={"target_role": target_role},
            )
        row = connection.execute(
            "SELECT status FROM plan_nodes WHERE id = ? AND archived = 0", (node_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(resource="plan_node", message="Node not found", details={"node_id": node_id})
        connection.execute(
            "UPDATE plan_nodes SET status = ? WHERE id = ?",
            (target_role, node_id),
        )
        return {"node_id": node_id, "status": target_role, "dispatched_role": target_role}

    @staticmethod
    def check_consistency_gate(
        connection,
        *,
        node_id: str,
        exposed_symbols: set[str],
    ) -> None:
        """Reject when code exposes symbols not declared in module_interfaces."""
        rows = connection.execute(
            "SELECT symbol FROM module_interfaces WHERE from_module = ? OR to_module = ?",
            (node_id, node_id),
        ).fetchall()
        declared = {str(r["symbol"]) for r in rows}
        undeclared = exposed_symbols if not declared else exposed_symbols - declared
        if undeclared:
            raise ConflictError(
                resource="consistency_gate",
                message="Code exposes symbols not in module interface map",
                error_code=CONSISTENCY_GATE_ERROR,
                details={"node_id": node_id, "undeclared": sorted(undeclared)},
            )

    @staticmethod
    def mark_drifted(connection, node_ids: list[str]) -> list[str]:
        """Set drifted status on nodes diverging from plan."""
        updated: list[str] = []
        for node_id in node_ids:
            connection.execute(
                "UPDATE plan_nodes SET status = ? WHERE id = ? AND archived = 0",
                (DRIFT_STATUS, node_id),
            )
            updated.append(node_id)
        return updated

    @staticmethod
    def declare_interface(
        connection,
        *,
        from_module: str,
        to_module: str,
        symbol: str,
        signature: dict[str, Any],
        mock: dict[str, Any],
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        """Insert one module_interfaces row."""
        interface_id = f"iface-{uuid.uuid4().hex}"
        now = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO module_interfaces("
            "id, from_module, to_module, symbol, signature, mock, confidence, status, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, 'declared', ?)",
            (
                interface_id,
                from_module,
                to_module,
                symbol,
                json.dumps(signature, ensure_ascii=False),
                json.dumps(mock, ensure_ascii=False),
                float(confidence),
                now,
            ),
        )
        return {
            "id": interface_id,
            "from_module": from_module,
            "to_module": to_module,
            "symbol": symbol,
            "status": "declared",
        }

    @staticmethod
    def propose_annotation_decision(*, confidence: float, risk: str) -> str:
        """Route annotation to auto-adopt or objection queue."""
        if confidence >= AUTO_ADOPT_CONFIDENCE and risk == "low":
            return "auto_adopt"
        return "objection"

    @staticmethod
    def mock_readonly_paths(connection, *, node_id: str) -> list[str]:
        """Paths derived from module_interfaces mocks that agents may read but not patch."""
        rows = connection.execute(
            "SELECT mock FROM module_interfaces WHERE from_module = ? OR to_module = ?",
            (node_id, node_id),
        ).fetchall()
        paths: list[str] = []
        for row in rows:
            try:
                mock = json.loads(row["mock"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(mock, dict):
                path = mock.get("file_path")
                if isinstance(path, str) and path.strip():
                    paths.append(path.strip().replace("\\", "/"))
        return paths

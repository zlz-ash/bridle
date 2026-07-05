"""Modify loop: dispatch, consistency gate, drift detection, module interfaces."""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from bridle.api.errors import ConflictError, ValidationError

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
        if not declared:
            undeclared = exposed_symbols
        else:
            undeclared = exposed_symbols - declared
        if undeclared:
            raise ConflictError(
                resource="consistency_gate",
                message="Code exposes symbols not in module interface map",
                error_code=CONSISTENCY_GATE_ERROR,
                details={"node_id": node_id, "undeclared": sorted(undeclared)},
            )

    @staticmethod
    def check_tdd_gate(*, has_red: bool, has_green: bool) -> None:
        """Reject verifying when TDD preconditions are not met."""
        if not has_red:
            raise ConflictError(
                resource="tdd_gate",
                message="TDD gate requires a RED test run before implementation",
                error_code=TDD_GATE_ERROR,
                details={"has_red": has_red, "has_green": has_green},
            )
        if not has_green:
            raise ConflictError(
                resource="tdd_gate",
                message="TDD gate requires a GREEN test run before completion",
                error_code=TDD_GATE_ERROR,
                details={"has_red": has_red, "has_green": has_green},
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

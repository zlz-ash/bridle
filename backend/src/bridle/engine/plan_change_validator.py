"""PlanChangeValidator — validate plan change proposals before apply."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from bridle.engine.interface_validator import InterfaceContractValidator
from bridle.engine.test_command_policy import TestCommandPolicy
from bridle.schemas.plan import PlanImportSchema

SUPPORTED_OPERATIONS = frozenset({"update_node"})

ALLOWED_FIELD_KEYS = frozenset({
    "title",
    "goal",
    "node_type",
    "depends_on",
    "files",
    "tests",
    "metrics",
    "constraints",
    "review_checks",
    "expected_outputs",
    "interfaces",
    "read_set",
    "write_set",
    "readonly_context",
    "conflict_contributions",
    "container_policy",
})

FORBIDDEN_FIELD_KEYS = frozenset({
    "id",
    "plan_id",
    "plan_node_id",
    "status",
    "order",
    "created_at",
    "updated_at",
    "runs",
    "proposals",
})


class PlanChangeValidator:
    @staticmethod
    def validate_allowed_fields(fields: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        for key in fields:
            if key in FORBIDDEN_FIELD_KEYS:
                errors.append(f"Field '{key}' cannot be changed via plan change proposal")
            elif key not in ALLOWED_FIELD_KEYS:
                errors.append(f"Field '{key}' is not in allowlist")
        if "tests" in fields:
            errors.extend(PlanChangeValidator.validate_tests(fields["tests"]))
        if "files" in fields:
            errors.extend(PlanChangeValidator.validate_files(fields.get("files", [])))
        for key in ("read_set", "write_set", "readonly_context"):
            if key in fields:
                errors.extend(PlanChangeValidator.validate_files(fields.get(key, [])))
        return errors

    @staticmethod
    def validate_tests(tests: Any) -> list[str]:
        if not isinstance(tests, list):
            return ["tests must be a list of strings"]
        errors: list[str] = []
        for item in tests:
            if not isinstance(item, str) or not item.strip():
                errors.append("tests must contain non-empty strings")
        if errors:
            return errors
        return TestCommandPolicy.validate_all(tests)

    @staticmethod
    def validate_files(files: list[str]) -> list[str]:
        errors: list[str] = []
        if not isinstance(files, list):
            return ["files must be a list"]
        for f in files:
            s = str(f)
            if ".." in s or s.startswith("/") or (len(s) >= 2 and s[1] == ":"):
                errors.append(f"Invalid file path: {s}")
        return errors

    @staticmethod
    def validate_operation(operation: dict) -> list[str]:
        op = operation.get("operation")
        if op not in SUPPORTED_OPERATIONS:
            return [f"Unsupported operation: {op}"]
        return PlanChangeValidator.validate_allowed_fields(operation.get("fields") or {})

    @staticmethod
    def validate_change_set(change_set: list[dict]) -> list[str]:
        errors: list[str] = []
        for op in change_set:
            errors.extend(PlanChangeValidator.validate_operation(op))
        return errors

    @staticmethod
    def build_candidate_plan(
        goal: str,
        nodes: list[dict],
        change_set: list[dict],
    ) -> tuple[PlanImportSchema | None, list[str]]:
        """Apply change_set to in-memory node dicts and validate as PlanImportSchema."""
        by_id = {n["id"]: deepcopy(n) for n in nodes}
        errors: list[str] = []
        errors.extend(PlanChangeValidator.validate_change_set(change_set))
        if errors:
            return None, errors

        for op in change_set:
            operation = op.get("operation")
            node_id = op.get("node_id")
            if operation == "update_node" and node_id in by_id:
                by_id[node_id].update(op.get("fields") or {})

        candidate_nodes = list(by_id.values())
        try:
            plan_data = PlanImportSchema(goal=goal, nodes=candidate_nodes)
        except Exception as exc:
            return None, [str(exc)]

        node_dicts = [
            {
                "plan_node_id": n.id,
                "depends_on": n.depends_on,
                "interfaces": n.interfaces.model_dump(),
            }
            for n in plan_data.nodes
        ]
        iface_errors = InterfaceContractValidator.validate(node_dicts)
        if iface_errors:
            return None, iface_errors

        graph_errors = PlanChangeValidator.validate_graph(candidate_nodes)
        if graph_errors:
            return None, graph_errors

        return plan_data, []

    @staticmethod
    def validate_graph(nodes: list[dict]) -> list[str]:
        node_map = {n["id"]: n for n in nodes}
        for node in nodes:
            for dep in node.get("depends_on", []):
                if dep not in node_map:
                    return [f"Node {node['id']} depends on unknown node {dep}"]

        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n["id"]: WHITE for n in nodes}

        def dfs(key: str) -> bool:
            color[key] = GRAY
            for dep in node_map[key].get("depends_on", []):
                if dep not in color:
                    continue
                if color[dep] == GRAY:
                    return True
                if color[dep] == WHITE and dfs(dep):
                    return True
            color[key] = BLACK
            return False

        for node in nodes:
            nid = node["id"]
            if color[nid] == WHITE and dfs(nid):
                return ["Circular dependency detected in plan graph"]
        return []

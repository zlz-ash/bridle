"""Tests for Pydantic schemas."""
from __future__ import annotations

import pytest

from bridle.schemas.common import NodeType, TaskStatus, NodeStatus, PlanStatus
from bridle.schemas.task import TaskCreateSchema, TaskReadSchema
from bridle.schemas.plan import PlanImportSchema, NodeUpdateSchema
from bridle.schemas.node import (
    NodeImportSchema, NodeReadSchema,
    NodeInterfacesSchema, InterfaceExposeSchema, InterfaceConsumeSchema,
    InterfaceFieldSchema, InterfaceEndpointSchema,
)
from bridle.schemas.run import RunReadSchema
from bridle.schemas.evidence import EvidenceReadSchema


class TestTaskSchemas:
    def test_create_task_valid(self) -> None:
        s = TaskCreateSchema(title="My Task", goal="Do stuff")
        assert s.title == "My Task"
        assert s.goal == "Do stuff"
        assert s.status == "created"

    def test_create_task_minimal(self) -> None:
        s = TaskCreateSchema(title="Minimal")
        assert s.title == "Minimal"
        assert s.goal is None

    def test_create_task_empty_title_fails(self) -> None:
        with pytest.raises(Exception):
            TaskCreateSchema(title="")

    def test_create_task_extra_field_fails(self) -> None:
        with pytest.raises(Exception):
            TaskCreateSchema(title="T", unknown_field="x")

    def test_read_task_has_id(self) -> None:
        s = TaskReadSchema(
            id="abc", title="T", goal=None, status="created",
            created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
        )
        assert s.id == "abc"


class TestPlanImportSchema:
    def _make_node(self, **overrides) -> dict:
        base = dict(
            id="n1", title="Node 1", goal="G", node_type="code_change",
            depends_on=[], files=["a.py"], tests=["pytest"],
            metrics={"cov": 80}, constraints={"no_print": True},
            review_checks=["check1"], expected_outputs={"exit": 0},
        )
        base.update(overrides)
        return base

    def test_valid_plan(self) -> None:
        plan = PlanImportSchema(
            goal="Plan goal",
            nodes=[self._make_node()],
        )
        assert plan.goal == "Plan goal"
        assert len(plan.nodes) == 1

    def test_missing_required_field_fails(self) -> None:
        with pytest.raises(Exception):
            PlanImportSchema(nodes=[self._make_node()])

    def test_extra_field_fails(self) -> None:
        with pytest.raises(Exception):
            PlanImportSchema(
                goal="G",
                nodes=[self._make_node()], extra="nope",
            )

    def test_task_id_not_in_schema(self) -> None:
        """task_id and task_title are no longer in PlanImportSchema —
        the task is determined by the API path, not the payload."""
        with pytest.raises(Exception):
            PlanImportSchema(
                task_id="t1", task_title="Task", goal="G",
                nodes=[self._make_node()],
            )

    def test_depends_on_unknown_node_fails(self) -> None:
        with pytest.raises(Exception, match="unknown node"):
            PlanImportSchema(
                goal="G",
                nodes=[self._make_node(depends_on=["nonexistent"])],
            )

    def test_depends_on_valid_node_succeeds(self) -> None:
        plan = PlanImportSchema(
            goal="G",
            nodes=[
                self._make_node(id="n1", depends_on=[]),
                self._make_node(id="n2", depends_on=["n1"]),
            ],
        )
        assert len(plan.nodes) == 2

    def test_circular_dep_allowed_at_schema_level(self) -> None:
        """Schema only checks that deps reference existing nodes, not cycles."""
        plan = PlanImportSchema(
            goal="G",
            nodes=[
                self._make_node(id="n1", depends_on=["n2"]),
                self._make_node(id="n2", depends_on=["n1"]),
            ],
        )
        assert len(plan.nodes) == 2

    def test_invalid_node_type_fails(self) -> None:
        with pytest.raises(Exception):
            PlanImportSchema(
                goal="G",
                nodes=[self._make_node(node_type="invalid_type")],
            )

    def test_empty_nodes_fails(self) -> None:
        with pytest.raises(Exception):
            PlanImportSchema(
                goal="G", nodes=[],
            )

    def test_container_boundary_fields_are_accepted(self) -> None:
        plan = PlanImportSchema(
            goal="G",
            aggregate_files=[
                {
                    "target_path": "src/router.py",
                    "contribution_dir": ".bridle/aggregate/src/router.py",
                    "merge_strategy": "json_list",
                    "owner": "main_agent",
                    "contributors": ["n1"],
                    "validation": {"required_keys": ["route"]},
                }
            ],
            nodes=[
                self._make_node(
                    read_set=["src/context.py"],
                    write_set=["src/feature.py"],
                    readonly_context=["docs/api.md"],
                    conflict_contributions=[
                        {
                            "aggregate_target": "src/router.py",
                            "contribution_path": ".bridle/aggregate/src/router.py/n1.json",
                        }
                    ],
                    container_policy={
                        "network_mode": "bridge",
                        "env_allowlist": ["OPENAI_API_KEY"],
                        "timeout_seconds": 120,
                        "health_check": {"command": "python --version"},
                    },
                )
            ],
        )

        node = plan.nodes[0]
        assert node.read_set == ["src/context.py"]
        assert node.write_set == ["src/feature.py"]
        assert node.container_policy.network_mode == "bridge"
        assert plan.aggregate_files[0].target_path == "src/router.py"

    def test_container_boundary_rejects_absolute_paths(self) -> None:
        with pytest.raises(Exception, match="path"):
            PlanImportSchema(
                goal="G",
                nodes=[self._make_node(read_set=["/etc/passwd"])],
            )

    def test_container_policy_rejects_unknown_network_mode(self) -> None:
        with pytest.raises(Exception):
            PlanImportSchema(
                goal="G",
                nodes=[self._make_node(container_policy={"network_mode": "host"})],
            )

    def test_aggregate_files_reject_duplicate_targets(self) -> None:
        aggregate_file = {
            "target_path": "src/router.py",
            "contribution_dir": ".bridle/aggregate/src/router.py",
            "merge_strategy": "json_list",
            "owner": "main_agent",
            "contributors": ["n1"],
        }

        with pytest.raises(Exception, match=r"(?i)duplicate"):
            PlanImportSchema(
                goal="G",
                aggregate_files=[aggregate_file, aggregate_file],
                nodes=[self._make_node()],
            )

    def test_legacy_files_seed_read_and_write_sets(self) -> None:
        plan = PlanImportSchema(goal="G", nodes=[self._make_node(files=["src/a.py"])])

        assert plan.nodes[0].read_set == ["src/a.py"]
        assert plan.nodes[0].write_set == ["src/a.py"]

    def test_conflict_contribution_requires_declared_aggregate_target(self) -> None:
        with pytest.raises(Exception, match="aggregate"):
            PlanImportSchema(
                goal="G",
                nodes=[
                    self._make_node(
                        conflict_contributions=[
                            {
                                "aggregate_target": "src/router.py",
                                "contribution_path": ".bridle/aggregate/src/router.py/n1.json",
                            }
                        ],
                    )
                ],
            )

    def test_conflict_contribution_requires_declared_contributor(self) -> None:
        with pytest.raises(Exception, match="contributor"):
            PlanImportSchema(
                goal="G",
                aggregate_files=[
                    {
                        "target_path": "src/router.py",
                        "contribution_dir": ".bridle/aggregate/src/router.py",
                        "merge_strategy": "json_list",
                        "owner": "main_agent",
                        "contributors": ["n2"],
                    }
                ],
                nodes=[
                    self._make_node(
                        conflict_contributions=[
                            {
                                "aggregate_target": "src/router.py",
                                "contribution_path": ".bridle/aggregate/src/router.py/n1.json",
                            }
                        ],
                    )
                ],
            )


class TestNodeImportSchema:
    def test_valid_node(self) -> None:
        n = NodeImportSchema(
            id="n1", title="N", goal="G", node_type="test_validation",
            depends_on=[], files=[], tests=[], metrics={}, constraints=[],
            review_checks=[], expected_outputs={},
        )
        assert n.node_type == "test_validation"

    def test_missing_required_fields(self) -> None:
        with pytest.raises(Exception):
            NodeImportSchema(id="n1", title="N")


class TestInterfaceSchemas:
    """Tests for the new interfaces contract schemas."""

    def _make_field(self, **overrides) -> dict:
        base = {"name": "user_id", "type": "string", "required": True, "description": "Current user id"}
        base.update(overrides)
        return base

    def _make_endpoint(self, **overrides) -> dict:
        base = {"name": "get_current_user", "method": "GET", "path": "/users/me", "description": "Read current user"}
        base.update(overrides)
        return base

    def _make_expose(self, **overrides) -> dict:
        base = {
            "name": "auth_context",
            "fields": [self._make_field()],
            "endpoints": [self._make_endpoint()],
        }
        base.update(overrides)
        return base

    def _make_consume(self, **overrides) -> dict:
        base = {
            "node_id": "n1",
            "interface_name": "auth_context",
            "fields": ["user_id"],
            "endpoints": ["get_current_user"],
        }
        base.update(overrides)
        return base

    # --- InterfaceFieldSchema ---

    def test_field_valid(self) -> None:
        f = InterfaceFieldSchema(name="user_id", type="string", required=True, description="desc")
        assert f.name == "user_id"
        assert f.type == "string"
        assert f.required is True

    def test_field_name_empty_fails(self) -> None:
        with pytest.raises(Exception):
            InterfaceFieldSchema(name="", type="string")

    def test_field_type_empty_fails(self) -> None:
        with pytest.raises(Exception):
            InterfaceFieldSchema(name="x", type="")

    def test_field_name_not_snake_case_fails(self) -> None:
        with pytest.raises(Exception):
            InterfaceFieldSchema(name="NotSnake", type="string")

    # --- InterfaceEndpointSchema ---

    def test_endpoint_valid(self) -> None:
        ep = InterfaceEndpointSchema(name="get_user", method="GET", path="/users/me", description="desc")
        assert ep.name == "get_user"
        assert ep.method == "GET"

    def test_endpoint_name_empty_fails(self) -> None:
        with pytest.raises(Exception):
            InterfaceEndpointSchema(name="", method="GET", path="/x")

    def test_endpoint_path_empty_fails(self) -> None:
        with pytest.raises(Exception):
            InterfaceEndpointSchema(name="x", method="GET", path="")

    def test_endpoint_name_not_snake_case_fails(self) -> None:
        with pytest.raises(Exception):
            InterfaceEndpointSchema(name="GetUser", method="GET", path="/x")

    # --- InterfaceExposeSchema ---

    def test_expose_valid(self) -> None:
        exp = InterfaceExposeSchema(
            name="auth_context",
            fields=[InterfaceFieldSchema(name="user_id", type="string")],
            endpoints=[InterfaceEndpointSchema(name="get_user", method="GET", path="/x")],
        )
        assert exp.name == "auth_context"
        assert len(exp.fields) == 1
        assert len(exp.endpoints) == 1

    def test_expose_name_empty_fails(self) -> None:
        with pytest.raises(Exception):
            InterfaceExposeSchema(name="", fields=[], endpoints=[])

    def test_expose_name_not_snake_case_fails(self) -> None:
        with pytest.raises(Exception):
            InterfaceExposeSchema(name="AuthContext", fields=[], endpoints=[])

    def test_expose_duplicate_field_names_fails(self) -> None:
        with pytest.raises(Exception, match=r"(?i)duplicate"):
            InterfaceExposeSchema(
                name="x",
                fields=[
                    InterfaceFieldSchema(name="a", type="string"),
                    InterfaceFieldSchema(name="a", type="int"),
                ],
                endpoints=[],
            )

    def test_expose_duplicate_endpoint_names_fails(self) -> None:
        with pytest.raises(Exception, match=r"(?i)duplicate"):
            InterfaceExposeSchema(
                name="x",
                fields=[],
                endpoints=[
                    InterfaceEndpointSchema(name="a", method="GET", path="/x"),
                    InterfaceEndpointSchema(name="a", method="POST", path="/y"),
                ],
            )

    # --- InterfaceConsumeSchema ---

    def test_consume_valid(self) -> None:
        c = InterfaceConsumeSchema(
            node_id="n1", interface_name="auth_context",
            fields=["user_id"], endpoints=["get_current_user"],
        )
        assert c.node_id == "n1"
        assert c.interface_name == "auth_context"
        assert c.fields == ["user_id"]

    def test_consume_node_id_empty_fails(self) -> None:
        with pytest.raises(Exception):
            InterfaceConsumeSchema(node_id="", interface_name="x")

    def test_consume_interface_name_empty_fails(self) -> None:
        with pytest.raises(Exception):
            InterfaceConsumeSchema(node_id="n1", interface_name="")

    def test_consume_duplicate_fields_fails(self) -> None:
        with pytest.raises(Exception, match=r"(?i)duplicate"):
            InterfaceConsumeSchema(node_id="n1", interface_name="x", fields=["a", "a"])

    def test_consume_duplicate_endpoints_fails(self) -> None:
        with pytest.raises(Exception, match=r"(?i)duplicate"):
            InterfaceConsumeSchema(node_id="n1", interface_name="x", endpoints=["a", "a"])

    # --- NodeInterfacesSchema ---

    def test_interfaces_default(self) -> None:
        iface = NodeInterfacesSchema()
        assert iface.exposes == []
        assert iface.consumes == []

    def test_interfaces_with_data(self) -> None:
        iface = NodeInterfacesSchema(
            exposes=[self._make_expose()],
            consumes=[self._make_consume()],
        )
        assert len(iface.exposes) == 1
        assert len(iface.consumes) == 1

    def test_interfaces_duplicate_expose_names_fails(self) -> None:
        with pytest.raises(Exception, match=r"(?i)duplicate"):
            NodeInterfacesSchema(
                exposes=[
                    InterfaceExposeSchema(name="x", fields=[], endpoints=[]),
                    InterfaceExposeSchema(name="x", fields=[], endpoints=[]),
                ],
            )

    def test_interfaces_extra_field_fails(self) -> None:
        with pytest.raises(Exception):
            NodeInterfacesSchema(extra="nope")

    # --- NodeImportSchema with interfaces ---

    def test_node_import_with_interfaces(self) -> None:
        n = NodeImportSchema(
            id="n1", title="N", goal="G", node_type="code_change",
            interfaces=NodeInterfacesSchema(
                exposes=[self._make_expose()],
                consumes=[],
            ),
        )
        assert n.interfaces.exposes[0].name == "auth_context"

    def test_node_import_without_interfaces_gets_default(self) -> None:
        n = NodeImportSchema(
            id="n1", title="N", goal="G", node_type="test_validation",
            depends_on=[], files=[], tests=[], metrics={}, constraints=[],
            review_checks=[], expected_outputs={},
        )
        assert n.interfaces.exposes == []
        assert n.interfaces.consumes == []

    def test_plan_import_with_interfaces(self) -> None:
        plan = PlanImportSchema(
            goal="G",
            nodes=[
                {
                    "id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
                    "interfaces": {
                        "exposes": [{"name": "auth_context", "fields": [], "endpoints": []}],
                        "consumes": [],
                    },
                },
                {
                    "id": "n2", "title": "N2", "goal": "G2", "node_type": "test_validation",
                    "depends_on": ["n1"],
                    "interfaces": {
                        "exposes": [],
                        "consumes": [
                            {"node_id": "n1", "interface_name": "auth_context", "fields": [], "endpoints": []},
                        ],
                    },
                },
            ],
        )
        assert plan.nodes[0].interfaces.exposes[0].name == "auth_context"
        assert plan.nodes[1].interfaces.consumes[0].node_id == "n1"

    def test_plan_import_old_plan_no_interfaces_field_still_works(self) -> None:
        """Backward compatibility: plans without 'interfaces' field get default empty contract."""
        plan = PlanImportSchema(
            goal="G",
            nodes=[
                NodeImportSchema(id="n1", title="N1", goal="G1", node_type="code_change"),
            ],
        )
        assert plan.nodes[0].interfaces.exposes == []
        assert plan.nodes[0].interfaces.consumes == []


class TestNodeUpdateSchemaInterfaces:
    """Tests for NodeUpdateSchema with strong-typed interfaces."""

    def _make_expose(self, **overrides) -> dict:
        base = {
            "name": "auth_context",
            "fields": [{"name": "user_id", "type": "string"}],
            "endpoints": [{"name": "get_user", "method": "GET", "path": "/users/me"}],
        }
        base.update(overrides)
        return base

    def _make_consume(self, **overrides) -> dict:
        base = {
            "node_id": "n1",
            "interface_name": "auth_context",
            "fields": ["user_id"],
            "endpoints": ["get_user"],
        }
        base.update(overrides)
        return base

    def test_update_interfaces_valid(self) -> None:
        upd = NodeUpdateSchema(
            id="n1",
            interfaces={
                "exposes": [self._make_expose()],
                "consumes": [self._make_consume()],
            },
        )
        assert upd.interfaces is not None
        assert upd.interfaces.exposes[0].name == "auth_context"
        assert upd.interfaces.consumes[0].node_id == "n1"

    def test_update_interfaces_none_is_valid(self) -> None:
        upd = NodeUpdateSchema(id="n1", title="Updated")
        assert upd.interfaces is None

    def test_update_container_boundary_fields_valid(self) -> None:
        upd = NodeUpdateSchema(
            id="n1",
            read_set=["src/context.py"],
            write_set=["src/feature.py"],
            readonly_context=["docs/api.md"],
            conflict_contributions=[
                {
                    "aggregate_target": "src/router.py",
                    "contribution_path": ".bridle/aggregate/src/router.py/n1.json",
                }
            ],
            container_policy={"network_mode": "bridge", "timeout_seconds": 60},
        )

        assert upd.read_set == ["src/context.py"]
        assert upd.container_policy is not None
        assert upd.container_policy.timeout_seconds == 60

    def test_update_interfaces_null_exposes_consumes(self) -> None:
        upd = NodeUpdateSchema(
            id="n1",
            interfaces={"exposes": [], "consumes": []},
        )
        assert upd.interfaces is not None
        assert upd.interfaces.exposes == []
        assert upd.interfaces.consumes == []

    def test_update_interfaces_invalid_snake_case_expose_name_fails(self) -> None:
        with pytest.raises(Exception, match=r"(?i)snake_case"):
            NodeUpdateSchema(
                id="n1",
                interfaces={
                    "exposes": [self._make_expose(name="AuthContext")],
                    "consumes": [],
                },
            )

    def test_update_interfaces_duplicate_expose_names_fails(self) -> None:
        with pytest.raises(Exception, match=r"(?i)duplicate"):
            NodeUpdateSchema(
                id="n1",
                interfaces={
                    "exposes": [
                        self._make_expose(name="x"),
                        self._make_expose(name="x"),
                    ],
                    "consumes": [],
                },
            )

    def test_update_interfaces_duplicate_field_names_fails(self) -> None:
        with pytest.raises(Exception, match=r"(?i)duplicate"):
            NodeUpdateSchema(
                id="n1",
                interfaces={
                    "exposes": [
                        {
                            "name": "x",
                            "fields": [
                                {"name": "a", "type": "string"},
                                {"name": "a", "type": "int"},
                            ],
                            "endpoints": [],
                        }
                    ],
                    "consumes": [],
                },
            )

    def test_update_interfaces_duplicate_endpoint_names_fails(self) -> None:
        with pytest.raises(Exception, match=r"(?i)duplicate"):
            NodeUpdateSchema(
                id="n1",
                interfaces={
                    "exposes": [
                        {
                            "name": "x",
                            "fields": [],
                            "endpoints": [
                                {"name": "a", "method": "GET", "path": "/x"},
                                {"name": "a", "method": "POST", "path": "/y"},
                            ],
                        }
                    ],
                    "consumes": [],
                },
            )

    def test_update_interfaces_consume_duplicate_fields_fails(self) -> None:
        with pytest.raises(Exception, match=r"(?i)duplicate"):
            NodeUpdateSchema(
                id="n1",
                interfaces={
                    "exposes": [],
                    "consumes": [
                        {"node_id": "n1", "interface_name": "x", "fields": ["a", "a"]}
                    ],
                },
            )

    def test_update_interfaces_consume_duplicate_endpoints_fails(self) -> None:
        with pytest.raises(Exception, match=r"(?i)duplicate"):
            NodeUpdateSchema(
                id="n1",
                interfaces={
                    "exposes": [],
                    "consumes": [
                        {"node_id": "n1", "interface_name": "x", "endpoints": ["a", "a"]}
                    ],
                },
            )


class TestEnums:
    def test_node_types(self) -> None:
        assert NodeType.CODE_CHANGE == "code_change"
        assert NodeType.TEST_VALIDATION == "test_validation"
        assert NodeType.METRIC_VALIDATION == "metric_validation"
        assert NodeType.REVIEW_GATE == "review_gate"

    def test_task_statuses(self) -> None:
        assert TaskStatus.CREATED == "created"
        assert TaskStatus.PLANNED == "planned"
        assert TaskStatus.RUNNING == "running"
        assert TaskStatus.COMPLETED == "completed"
        assert TaskStatus.FAILED == "failed"

    def test_node_statuses(self) -> None:
        assert NodeStatus.PENDING == "pending"
        assert NodeStatus.BLOCKED == "blocked"
        assert NodeStatus.READY == "ready"
        assert NodeStatus.RUNNING == "running"
        assert NodeStatus.COMPLETED == "completed"
        assert NodeStatus.FAILED == "failed"
        assert NodeStatus.MISSING_EVIDENCE == "missing_evidence"
        assert NodeStatus.ARCHIVED == "archived"

    def test_plan_statuses(self) -> None:
        assert PlanStatus.DRAFT == "draft"
        assert PlanStatus.ACTIVE == "active"
        assert PlanStatus.COMPLETED == "completed"
        assert PlanStatus.FAILED == "failed"
        assert PlanStatus.ARCHIVED == "archived"

"""Tests for InterfaceContractValidator."""
from __future__ import annotations

import pytest

from bridle.schemas.node import (
    InterfaceConsumeSchema,
    InterfaceExposeSchema,
    InterfaceFieldSchema,
    InterfaceEndpointSchema,
    NodeInterfacesSchema,
)


def _make_field(name="user_id", field_type="string") -> InterfaceFieldSchema:
    return InterfaceFieldSchema(name=name, type=field_type)


def _make_endpoint(name="get_user", method="GET", path="/users/me") -> InterfaceEndpointSchema:
    return InterfaceEndpointSchema(name=name, method=method, path=path)


def _make_expose(name="auth_context", fields=None, endpoints=None) -> InterfaceExposeSchema:
    return InterfaceExposeSchema(
        name=name,
        fields=fields or [_make_field()],
        endpoints=endpoints or [_make_endpoint()],
    )


def _make_consume(node_id="n2", interface_name="auth_context", fields=None, endpoints=None) -> InterfaceConsumeSchema:
    return InterfaceConsumeSchema(
        node_id=node_id,
        interface_name=interface_name,
        fields=fields or ["user_id"],
        endpoints=endpoints or ["get_user"],
    )


def _make_node(plan_node_id, depends_on=None, interfaces=None) -> dict:
    return {
        "plan_node_id": plan_node_id,
        "depends_on": depends_on or [],
        "interfaces": interfaces or {"exposes": [], "consumes": []},
    }


class TestInterfaceContractValidator:
    """Unit tests for the validator logic."""

    def test_no_interfaces_is_valid(self) -> None:
        """Nodes without any interface contracts should pass validation."""
        from bridle.engine.interface_validator import InterfaceContractValidator

        nodes = [
            _make_node("n1"),
            _make_node("n2", depends_on=["n1"]),
        ]
        errors = InterfaceContractValidator.validate(nodes)
        assert errors == []

    def test_valid_consume_from_predecessor(self) -> None:
        """n2 consumes n1's exposed interface — n1 is a predecessor, so it's valid."""
        from bridle.engine.interface_validator import InterfaceContractValidator

        nodes = [
            _make_node("n1", interfaces={"exposes": [_make_expose().model_dump()], "consumes": []}),
            _make_node("n2", depends_on=["n1"], interfaces={"exposes": [], "consumes": [_make_consume(node_id="n1").model_dump()]}),
        ]
        errors = InterfaceContractValidator.validate(nodes)
        assert errors == []

    def test_valid_consume_from_successor(self) -> None:
        """n1 consumes n2's exposed interface — n2 depends on n1, so n2 is a successor."""
        from bridle.engine.interface_validator import InterfaceContractValidator

        nodes = [
            _make_node("n1", interfaces={"exposes": [], "consumes": [_make_consume(node_id="n2", interface_name="my_api").model_dump()]}),
            _make_node("n2", depends_on=["n1"], interfaces={"exposes": [_make_expose(name="my_api").model_dump()], "consumes": []}),
        ]
        errors = InterfaceContractValidator.validate(nodes)
        assert errors == []

    def test_consume_from_non_adjacent_fails(self) -> None:
        """n3 is not adjacent to n1 (n2 is between them) — should fail."""
        from bridle.engine.interface_validator import InterfaceContractValidator

        nodes = [
            _make_node("n1", interfaces={"exposes": [_make_expose().model_dump()], "consumes": []}),
            _make_node("n2", depends_on=["n1"]),
            _make_node("n3", depends_on=["n2"], interfaces={"exposes": [], "consumes": [_make_consume(node_id="n1").model_dump()]}),
        ]
        errors = InterfaceContractValidator.validate(nodes)
        assert len(errors) == 1
        assert "not adjacent" in errors[0]

    def test_consume_unknown_interface_fails(self) -> None:
        """n2 consumes an interface that n1 does not expose."""
        from bridle.engine.interface_validator import InterfaceContractValidator

        nodes = [
            _make_node("n1", interfaces={"exposes": [_make_expose(name="auth_context").model_dump()], "consumes": []}),
            _make_node("n2", depends_on=["n1"], interfaces={"exposes": [], "consumes": [_make_consume(node_id="n1", interface_name="unknown_api").model_dump()]}),
        ]
        errors = InterfaceContractValidator.validate(nodes)
        assert len(errors) == 1
        assert "does not expose" in errors[0] or "interface" in errors[0].lower()

    def test_consume_unknown_field_fails(self) -> None:
        """n2 requests a field not in n1's expose."""
        from bridle.engine.interface_validator import InterfaceContractValidator

        nodes = [
            _make_node("n1", interfaces={"exposes": [_make_expose(fields=[_make_field(name="user_id")]).model_dump()], "consumes": []}),
            _make_node("n2", depends_on=["n1"], interfaces={"exposes": [], "consumes": [_make_consume(node_id="n1", fields=["unknown_field"]).model_dump()]}),
        ]
        errors = InterfaceContractValidator.validate(nodes)
        assert len(errors) == 1
        assert "unknown_field" in errors[0] or "field" in errors[0].lower()

    def test_consume_unknown_endpoint_fails(self) -> None:
        """n2 requests an endpoint not in n1's expose."""
        from bridle.engine.interface_validator import InterfaceContractValidator

        nodes = [
            _make_node("n1", interfaces={"exposes": [_make_expose(endpoints=[_make_endpoint(name="get_user")]).model_dump()], "consumes": []}),
            _make_node("n2", depends_on=["n1"], interfaces={"exposes": [], "consumes": [_make_consume(node_id="n1", endpoints=["unknown_ep"]).model_dump()]}),
        ]
        errors = InterfaceContractValidator.validate(nodes)
        assert len(errors) == 1
        assert "unknown_ep" in errors[0] or "endpoint" in errors[0].lower()

    def test_consume_from_target_with_no_exposes_fails(self) -> None:
        """n2 tries to consume an interface from n1, but n1 has no interfaces exposed."""
        from bridle.engine.interface_validator import InterfaceContractValidator

        nodes = [
            _make_node("n1"),
            _make_node("n2", depends_on=["n1"], interfaces={"exposes": [], "consumes": [_make_consume(node_id="n1").model_dump()]}),
        ]
        errors = InterfaceContractValidator.validate(nodes)
        assert len(errors) >= 1

    def test_multiple_errors_collected(self) -> None:
        """All validation errors should be collected, not fail-fast."""
        from bridle.engine.interface_validator import InterfaceContractValidator

        nodes = [
            _make_node("n1", interfaces={"exposes": [_make_expose().model_dump()], "consumes": []}),
            _make_node("n2", depends_on=["n1"], interfaces={
                "exposes": [],
                "consumes": [
                    _make_consume(node_id="n1", interface_name="bad1").model_dump(),
                    _make_consume(node_id="n1", interface_name="bad2").model_dump(),
                ],
            }),
        ]
        errors = InterfaceContractValidator.validate(nodes)
        assert len(errors) == 2

    def test_empty_consume_list_is_valid(self) -> None:
        """Empty consumes list should always be valid."""
        from bridle.engine.interface_validator import InterfaceContractValidator

        nodes = [
            _make_node("n1", interfaces={"exposes": [_make_expose().model_dump()], "consumes": []}),
            _make_node("n2", depends_on=["n1"], interfaces={"exposes": [], "consumes": []}),
        ]
        errors = InterfaceContractValidator.validate(nodes)
        assert errors == []

    def test_expose_only_no_consumes_is_valid(self) -> None:
        """A node that only exposes (no consumes) is always valid."""
        from bridle.engine.interface_validator import InterfaceContractValidator

        nodes = [
            _make_node("n1", interfaces={"exposes": [_make_expose().model_dump()]}),
            _make_node("n2", depends_on=["n1"]),
        ]
        errors = InterfaceContractValidator.validate(nodes)
        assert errors == []


class TestAccessibleContext:
    """Tests for NodeService.get_accessible_context with adjacency enforcement."""

    @staticmethod
    def _expose_dict(name="auth_context"):
        return {"name": name, "fields": [{"name": "user_id", "type": "string"}], "endpoints": [{"name": "get_user", "method": "GET", "path": "/users/me"}]}

    @staticmethod
    def _consume_dict(node_id, interface_name="auth_context"):
        return {"node_id": node_id, "interface_name": interface_name, "fields": ["user_id"], "endpoints": ["get_user"]}

    @pytest.mark.asyncio
    async def test_adjacent_predecessor_accessible(self, db) -> None:
        """n2 consumes from n1 (predecessor via depends_on) → allowed."""
        from bridle.services.plan_service import PlanService
        from bridle.services.node_service import NodeService
        from bridle.schemas.plan import PlanImportSchema

        data = PlanImportSchema(
            goal="Test",
            nodes=[
                {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
                 "tests": ["pytest tests/ -q"],
                 "interfaces": {"exposes": [self._expose_dict()], "consumes": []}},
                {"id": "n2", "title": "N2", "goal": "G2", "node_type": "test_validation",
                 "depends_on": ["n1"],
                 "interfaces": {"exposes": [], "consumes": [self._consume_dict("n1")]}},
            ],
        )
        result = await PlanService.import_plan(db, "task-1", data)
        n2_record_id = result["nodes"][1]["id"]

        ctx = await NodeService.get_accessible_context(db, n2_record_id)
        assert "accessible" in ctx
        accessible = ctx["accessible"]
        assert len(accessible) == 1
        assert accessible[0]["interface_name"] == "auth_context"
        assert len(accessible[0]["fields"]) == 1

    @pytest.mark.asyncio
    async def test_adjacent_successor_accessible(self, db) -> None:
        """n1 consumes from n2 (successor — n2 depends on n1) → allowed."""
        from bridle.services.plan_service import PlanService
        from bridle.services.node_service import NodeService
        from bridle.schemas.plan import PlanImportSchema

        data = PlanImportSchema(
            goal="Test",
            nodes=[
                {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
                 "tests": ["pytest tests/ -q"],
                 "interfaces": {"exposes": [], "consumes": [self._consume_dict("n2", "review_api")]}},
                {"id": "n2", "title": "N2", "goal": "G2", "node_type": "test_validation",
                 "depends_on": ["n1"],
                 "interfaces": {"exposes": [self._expose_dict("review_api")], "consumes": []}},
            ],
        )
        result = await PlanService.import_plan(db, "task-2", data)
        n1_record_id = result["nodes"][0]["id"]

        ctx = await NodeService.get_accessible_context(db, n1_record_id)
        assert "accessible" in ctx
        accessible = ctx["accessible"]
        assert len(accessible) == 1
        assert accessible[0]["interface_name"] == "review_api"

    @pytest.mark.asyncio
    async def test_non_adjacent_consume_blocked(self, db) -> None:
        """n3 consumes from n1 (two hops away) → read-time defense returns error."""
        from bridle.services.plan_service import PlanService
        from bridle.services.node_service import NodeService
        from bridle.schemas.plan import PlanImportSchema
        from sqlalchemy import update
        from bridle.models.node import NodeRecord

        # Import a valid plan (n1 → n2 → n3, all adjacent consumes are fine)
        data = PlanImportSchema(
            goal="Test",
            nodes=[
                {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
                 "tests": ["pytest tests/ -q"],
                 "interfaces": {"exposes": [self._expose_dict()], "consumes": []}},
                {"id": "n2", "title": "N2", "goal": "G2", "node_type": "code_change",
                 "depends_on": ["n1"],
                 "tests": ["pytest tests/ -q"],
                 "interfaces": {"exposes": [], "consumes": [self._consume_dict("n1")]}},
                {"id": "n3", "title": "N3", "goal": "G3", "node_type": "test_validation",
                 "depends_on": ["n2"],
                 "interfaces": {"exposes": [], "consumes": []}},
            ],
        )
        result = await PlanService.import_plan(db, "task-3", data)
        n3_record_id = result["nodes"][2]["id"]

        # Inject dirty data: n3 consumes from n1 (cross-hop, bypassing validator)
        dirty_ifaces = {"exposes": [], "consumes": [self._consume_dict("n1")]}
        await db.execute(
            update(NodeRecord).where(NodeRecord.id == n3_record_id).values(interfaces=dirty_ifaces)
        )
        await db.commit()

        ctx = await NodeService.get_accessible_context(db, n3_record_id)
        assert "accessible" in ctx
        accessible = ctx["accessible"]
        assert len(accessible) == 1
        assert "error" in accessible[0]
        assert "not adjacent" in accessible[0]["error"].lower()

    @pytest.mark.asyncio
    async def test_dirty_data_non_adjacent_no_fields_leaked(self, db) -> None:
        """Even if DB has dirty consumes pointing to non-adjacent node, no fields/endpoints leak."""
        from bridle.services.plan_service import PlanService
        from bridle.services.node_service import NodeService
        from bridle.schemas.plan import PlanImportSchema

        data = PlanImportSchema(
            goal="Test",
            nodes=[
                {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
                 "tests": ["pytest tests/ -q"],
                 "interfaces": {"exposes": [self._expose_dict()], "consumes": []}},
                {"id": "n2", "title": "N2", "goal": "G2", "node_type": "code_change",
                 "tests": ["pytest tests/ -q"]},
            ],
        )
        result = await PlanService.import_plan(db, "task-4", data)
        n2_record_id = result["nodes"][1]["id"]

        # Directly inject dirty data: n2 consumes from n1 but no dependency edge
        from sqlalchemy import select, update
        from bridle.models.node import NodeRecord
        dirty_ifaces = {"exposes": [], "consumes": [self._consume_dict("n1")]}
        await db.execute(
            update(NodeRecord).where(NodeRecord.id == n2_record_id).values(interfaces=dirty_ifaces)
        )
        await db.commit()

        ctx = await NodeService.get_accessible_context(db, n2_record_id)
        assert "accessible" in ctx
        accessible = ctx["accessible"]
        assert len(accessible) == 1
        assert "error" in accessible[0]
        assert "not adjacent" in accessible[0]["error"].lower()
        # Must NOT return any fields or endpoints
        assert "fields" not in accessible[0]
        assert "endpoints" not in accessible[0]

"""Tests for ComplexityNegotiationService."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from bridle.engine.node_complexity_policy import NodeComplexityValidation, validate_plan_nodes
from bridle.schemas.node import NodeImportSchema
from bridle.schemas.complexity_negotiation import validate_negotiation_decision
from bridle.services.complexity_negotiation_service import (
    ComplexityNegotiationService,
    NegotiationDecisionRejected,
    NegotiationProtocolError,
    apply_negotiation_decision,
)


def _node(**overrides) -> NodeImportSchema:
    base = {
        "id": "n1",
        "title": "Node",
        "goal": "Implement feature with clear acceptance criteria for reviewers",
        "node_type": "code_change",
        "depends_on": [],
        "files": ["src/a.py"],
        "tests": ["pytest tests/ -q"],
        "metrics": {},
        "constraints": {"c": True},
        "review_checks": [],
        "expected_outputs": {"exit": 0},
        "estimated_minutes": 15,
    }
    base.update(overrides)
    return NodeImportSchema(**base)


@pytest.mark.asyncio
class TestComplexityNegotiationService:
    async def test_parses_merge_decision(self) -> None:
        client = AsyncMock()
        client.chat_completion.return_value = {
            "choices": [{"message": {"content": '```json\n{"action":"merge","merge":{"node_ids":["n1","n2"],"new_title":"Merged","new_goal":"Do both with clear acceptance criteria","new_estimated_minutes":90,"merged_files":["src/a.py"],"merged_depends_on":[]}}\n```'}}]
        }
        svc = ComplexityNegotiationService(client)
        nodes = [_node(id="n1"), _node(id="n2", title="N2")]
        failing = validate_plan_nodes(nodes)
        decision = await svc.negotiate(
            plan_nodes=nodes,
            validation_issues=[v for v in failing if not v.ok],
            round_index=0,
        )
        assert decision.action == "merge"
        assert decision.merge is not None
        assert decision.merge.node_ids == ["n1", "n2"]

    async def test_invalid_json_raises_protocol_error(self) -> None:
        client = AsyncMock()
        client.chat_completion.return_value = {
            "choices": [{"message": {"content": "not json at all"}}]
        }
        svc = ComplexityNegotiationService(client)
        with pytest.raises(NegotiationProtocolError):
            await svc.negotiate(
                plan_nodes=[_node()],
                validation_issues=validate_plan_nodes([_node()]),
                round_index=0,
            )

    async def test_unknown_action_raises(self) -> None:
        client = AsyncMock()
        client.chat_completion.return_value = {
            "choices": [{"message": {"content": '```json\n{"action":"unknown"}\n```'}}]
        }
        svc = ComplexityNegotiationService(client)
        with pytest.raises(NegotiationProtocolError):
            await svc.negotiate(
                plan_nodes=[_node()],
                validation_issues=validate_plan_nodes([_node()]),
                round_index=0,
            )

    async def test_accept_as_is_non_micro_rejected(self) -> None:
        client = AsyncMock()
        client.chat_completion.return_value = {
            "choices": [{"message": {"content": '```json\n{"action":"accept_as_is","accept_as_is":{"node_id":"n1","reason":"tiny"}}\n```'}}]
        }
        svc = ComplexityNegotiationService(client)
        with pytest.raises(NegotiationDecisionRejected):
            await svc.negotiate(
                plan_nodes=[_node(node_type="code_change")],
                validation_issues=validate_plan_nodes([_node()]),
                round_index=0,
            )


class TestApplyNegotiationDecision:
    def test_merge_removes_old_nodes(self) -> None:
        nodes = [_node(id="n1"), _node(id="n2", title="N2")]
        decision = validate_negotiation_decision(
            {
                "action": "merge",
                "merge": {
                    "node_ids": ["n1", "n2"],
                    "new_title": "Merged",
                    "new_goal": "Combined work with clear acceptance criteria for QA",
                    "new_estimated_minutes": 90,
                    "merged_files": ["src/a.py"],
                    "merged_depends_on": [],
                },
            }
        )
        apply_negotiation_decision(nodes, decision)
        assert len(nodes) == 1
        assert nodes[0].id == "n1"
        assert nodes[0].estimated_minutes == 90

    def test_expand_updates_node(self) -> None:
        nodes = [_node()]
        decision = validate_negotiation_decision(
            {
                "action": "expand",
                "expand": {
                    "node_id": "n1",
                    "new_goal": "Bigger scope with clear acceptance criteria",
                    "new_acceptance_scope": "Module X passes integration tests",
                    "new_estimated_minutes": 75,
                    "additional_files": ["src/b.py"],
                },
            }
        )
        apply_negotiation_decision(nodes, decision)
        assert nodes[0].estimated_minutes == 75
        assert "src/b.py" in nodes[0].files


class TestParseDecisionJson:
    def test_decision_with_missing_payload_rejected_at_parse(self) -> None:
        from bridle.services.complexity_negotiation_service import (
            NegotiationProtocolError,
            _parse_decision_json,
        )

        with pytest.raises(NegotiationProtocolError):
            _parse_decision_json('```json\n{"action":"merge","merge":null}\n```')

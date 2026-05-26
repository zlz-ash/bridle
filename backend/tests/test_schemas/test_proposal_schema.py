"""Tests for proposal schemas — strict validation for agent proposals."""
from __future__ import annotations

import pytest


class TestProposalCreateSchema:
    """Tests for ProposalCreateSchema (POST request body)."""

    def test_valid_create(self) -> None:
        from bridle.schemas.proposal import ProposalCreateSchema

        s = ProposalCreateSchema(instruction="Implement this node")
        assert s.instruction == "Implement this node"

    def test_empty_instruction_fails(self) -> None:
        from bridle.schemas.proposal import ProposalCreateSchema

        with pytest.raises(Exception):
            ProposalCreateSchema(instruction="")

    def test_extra_field_fails(self) -> None:
        from bridle.schemas.proposal import ProposalCreateSchema

        with pytest.raises(Exception):
            ProposalCreateSchema(instruction="X", unknown="nope")


class TestFilePatchSchema:
    """Tests for FilePatchSchema — strong-typed patch entries."""

    def test_valid_patch(self) -> None:
        from bridle.schemas.proposal import FilePatchSchema

        fp = FilePatchSchema(path="src/a.py", change_type="modify", diff="---\n+++\n")
        assert fp.path == "src/a.py"
        assert fp.change_type == "modify"
        assert fp.diff == "---\n+++\n"

    def test_path_empty_fails(self) -> None:
        from bridle.schemas.proposal import FilePatchSchema

        with pytest.raises(Exception):
            FilePatchSchema(path="", change_type="modify", diff="")

    def test_change_type_invalid_fails(self) -> None:
        from bridle.schemas.proposal import FilePatchSchema

        with pytest.raises(Exception):
            FilePatchSchema(path="a.py", change_type="delete", diff="")

    def test_change_type_modify_valid(self) -> None:
        from bridle.schemas.proposal import FilePatchSchema

        for ct in ("modify", "add", "remove"):
            fp = FilePatchSchema(path="a.py", change_type=ct, diff="")
            assert fp.change_type == ct

    def test_absolute_path_fails(self) -> None:
        from bridle.schemas.proposal import FilePatchSchema

        with pytest.raises(Exception):
            FilePatchSchema(path="C:\\secret.py", change_type="modify", diff="")

    def test_parent_traversal_fails(self) -> None:
        from bridle.schemas.proposal import FilePatchSchema

        with pytest.raises(Exception):
            FilePatchSchema(path="../secret.py", change_type="modify", diff="")

    def test_backslash_path_fails(self) -> None:
        from bridle.schemas.proposal import FilePatchSchema

        with pytest.raises(Exception):
            FilePatchSchema(path="src\\a.py", change_type="modify", diff="")

    def test_extra_field_fails(self) -> None:
        from bridle.schemas.proposal import FilePatchSchema

        with pytest.raises(Exception):
            FilePatchSchema(path="a.py", change_type="modify", diff="", unknown="x")


class TestAgentProposalSchema:
    """Tests for AgentProposalSchema — strong-typed provider output."""

    def test_valid_proposal(self) -> None:
        from bridle.schemas.proposal import AgentProposalSchema

        p = AgentProposalSchema(
            summary="A dry-run proposal",
            file_patches=[
                {"path": "src/a.py", "change_type": "modify", "diff": ""}
            ],
            tests_to_run=["pytest tests/"],
        )
        assert p.summary == "A dry-run proposal"
        assert len(p.file_patches) == 1
        assert len(p.tests_to_run) == 1

    def test_minimal_proposal_defaults(self) -> None:
        from bridle.schemas.proposal import AgentProposalSchema

        p = AgentProposalSchema(summary="Just summary")
        assert p.file_patches == []
        assert p.tests_to_run == []

    def test_empty_summary_fails(self) -> None:
        from bridle.schemas.proposal import AgentProposalSchema

        with pytest.raises(Exception):
            AgentProposalSchema(summary="")

    def test_missing_summary_fails(self) -> None:
        from bridle.schemas.proposal import AgentProposalSchema

        with pytest.raises(Exception):
            AgentProposalSchema()

    def test_extra_field_fails(self) -> None:
        from bridle.schemas.proposal import AgentProposalSchema

        with pytest.raises(Exception):
            AgentProposalSchema(summary="X", unknown="y")


class TestAgentContext:
    """Tests for AgentContext — provider input boundary."""

    def test_valid_context(self) -> None:
        from bridle.schemas.proposal import AgentContext

        ctx = AgentContext(
            instruction="Do X",
            node={"id": "n1", "title": "N1"},
            allowed_files=["src/a.py"],
            tests=["pytest"],
            metrics={},
            constraints={"c": True},
            review_checks=[],
            expected_outputs={},
            accessible_context={},
        )
        assert ctx.instruction == "Do X"
        assert ctx.allowed_files == ["src/a.py"]

    def test_empty_instruction_fails(self) -> None:
        from bridle.schemas.proposal import AgentContext

        with pytest.raises(Exception):
            AgentContext(
                instruction="",
                node={},
                allowed_files=[],
                tests=[],
                metrics={},
                constraints={},
                review_checks=[],
                expected_outputs={},
                accessible_context={},
            )

    def test_extra_field_fails(self) -> None:
        from bridle.schemas.proposal import AgentContext

        with pytest.raises(Exception):
            AgentContext(
                instruction="X",
                node={},
                allowed_files=[],
                tests=[],
                metrics={},
                constraints={},
                review_checks=[],
                expected_outputs={},
                accessible_context={},
                unknown="nope",
            )


class TestProposalReadSchema:
    """Tests for ProposalReadSchema — response shape compatibility."""

    def test_valid_response_shape(self) -> None:
        from bridle.schemas.proposal import ProposalReadSchema

        s = ProposalReadSchema(
            id="prop-uuid",
            node_id="node-uuid",
            plan_node_id="n2",
            status="proposed",
            instruction="Do X",
            allowed_files=["src/a.py"],
            accessible_context={"node_id": "n1", "accessible": []},
            proposal={
                "summary": "Dry-run proposal",
                "file_patches": [
                    {"path": "src/a.py", "change_type": "modify", "diff": ""}
                ],
                "tests_to_run": ["pytest tests/"],
            },
            source="agent",
            created_at="2026-05-17T00:00:00",
            updated_at="2026-05-17T00:00:00",
        )
        assert s.id == "prop-uuid"
        assert s.status == "proposed"
        assert s.source == "agent"
        assert len(s.allowed_files) == 1
        assert s.proposal["summary"] == "Dry-run proposal"

    def test_minimal_proposal(self) -> None:
        from bridle.schemas.proposal import ProposalReadSchema

        s = ProposalReadSchema(
            id="p1",
            node_id="n1",
            plan_node_id="n1",
            status="proposed",
            instruction="X",
            allowed_files=[],
            accessible_context={},
            proposal={"summary": "Minimal", "file_patches": [], "tests_to_run": []},
            source="agent",
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )
        assert s.proposal["summary"] == "Minimal"

"""Tests for proposal tests_to_run allowlist resolution."""
from __future__ import annotations

from bridle.agent.runtime.schemas import AgentProposalSchema
from bridle.agent.tools.proposal_test_validator import (
    resolve_allowed_test_commands,
    validate_proposal_tests_to_run,
)


class TestResolveAllowedTestCommands:
    def test_missing_key_falls_back_to_context_tests(self) -> None:
        allowed = resolve_allowed_test_commands({}, ["echo a", "echo b"])
        assert allowed == frozenset({"echo a", "echo b"})

    def test_explicit_empty_list_no_fallback(self) -> None:
        allowed = resolve_allowed_test_commands(
            {"allowed_test_commands": []},
            ["echo a"],
        )
        assert allowed == frozenset()

    def test_explicit_nonempty_list_no_fallback(self) -> None:
        allowed = resolve_allowed_test_commands(
            {"allowed_test_commands": ["echo ok"]},
            ["echo other"],
        )
        assert allowed == frozenset({"echo ok"})


class TestValidateProposalTestsToRun:
    def test_rejects_command_when_allowlist_empty(self) -> None:
        proposal = AgentProposalSchema(
            summary="s",
            file_patches=[],
            tests_to_run=["echo ok"],
        )
        errors = validate_proposal_tests_to_run(
            proposal,
            {"allowed_test_commands": []},
            ["echo ok"],
        )
        assert errors

    def test_rejects_policy_violation_in_allowlist(self) -> None:
        proposal = AgentProposalSchema(
            summary="s",
            file_patches=[],
            tests_to_run=["npm install foo"],
        )
        errors = validate_proposal_tests_to_run(
            proposal,
            {"allowed_test_commands": ["npm install foo"]},
            ["npm install foo"],
        )
        assert errors


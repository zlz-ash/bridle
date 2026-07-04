"""Shared validation for proposal tests_to_run against sandbox allowlist."""
from __future__ import annotations

from bridle.agent.tools.test_command_policy import TestCommandPolicy
from bridle.agent.runtime.schemas import AgentProposalSchema


def resolve_allowed_test_commands(
    sandbox_snapshot: dict,
    context_tests: list[str],
) -> frozenset[str]:
    """Resolve allowlisted test commands for the current run.

    If ``allowed_test_commands`` is present in the sandbox snapshot (including
    an explicit empty list), use it only -do not fall back to ``context.tests``.
    """
    if "allowed_test_commands" in sandbox_snapshot:
        raw = sandbox_snapshot["allowed_test_commands"]
        if not isinstance(raw, list):
            return frozenset()
        return frozenset(str(c).strip() for c in raw if str(c).strip())
    return frozenset(str(c).strip() for c in context_tests if str(c).strip())


def validate_proposal_tests_to_run(
    proposal: AgentProposalSchema,
    sandbox_snapshot: dict,
    context_tests: list[str],
) -> list[str]:
    """Return errors when tests_to_run violates allowlist or command policy."""
    allowed = resolve_allowed_test_commands(sandbox_snapshot, context_tests)
    errors: list[str] = []
    for cmd in proposal.tests_to_run:
        text = str(cmd).strip()
        if not text:
            continue
        if text not in allowed:
            errors.append(f"Command not in sandbox allowlist: {text}")
            continue
        errors.extend(TestCommandPolicy.validate(text))
    return errors


"""CapabilityPolicyService — manifest of allowed model intents."""
from __future__ import annotations


class CapabilityPolicyService:
    @staticmethod
    def session_capabilities() -> list[str]:
        return [
            "list_eligible_nodes",
            "select_node",
            "create_node_agent_run",
            "read_node_result",
            "propose_plan_change",
        ]

    @staticmethod
    def capability_manifest() -> dict:
        return CapabilityPolicyService.for_run(
            allowed_files=[],
            node_tests=[],
        )

    @staticmethod
    def for_run(
        *,
        allowed_files: list[str],
        node_tests: list[str],
        sandbox_snapshot: dict | None = None,
        network_allowed: bool = False,
    ) -> dict:
        """Per-run capability manifest for agent providers."""
        return {
            "capabilities": {
                "select_node": {"allowed": True, "requires_eligible_node": True},
                "create_proposal": {"allowed": True, "requires_node_context": True},
                "propose_plan_change": {"allowed": True, "requires_human_review": True},
                "apply_patch": {"allowed": False},
                "run_command": {"allowed": False},
                "run_allowed_tests": {
                    "allowed": True,
                    "commands": list(node_tests),
                },
            },
            "tool_capabilities": {
                "read_allowed_file": {"allowed": True, "paths": list(allowed_files)},
                "propose_file_patch": {"allowed": True, "paths": list(allowed_files)},
                "run_allowed_tests": {"allowed": True, "commands": list(node_tests)},
                "report_blocked": {"allowed": True},
                "apply_patch": {"allowed": False},
                "run_command": {"allowed": False},
                "web_search": {"allowed": network_allowed, "requires_network": True},
            },
            "sandbox": sandbox_snapshot or {},
        }

    @staticmethod
    def allowed_result_types() -> frozenset[str]:
        return frozenset({
            "proposal",
            "diagnosis",
            "test_suggestion",
            "blocked_report",
            "no_op",
        })

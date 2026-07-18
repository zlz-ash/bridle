"""Role authorization for the shared project agent tool set."""
from __future__ import annotations

from typing import Literal

from bridle.api.errors import ForbiddenError

RuntimeRole = Literal["planning", "executing", "mapping", "sidecar"]

_SHARED_TOOL_RULES: dict[str, frozenset[RuntimeRole]] = {
    "read_project_map": frozenset({"planning", "executing", "mapping", "sidecar"}),
    "patch_plan_nodes": frozenset({"planning"}),
    "propose_semantic_annotation": frozenset({"mapping"}),
    "dispatch_child_agent": frozenset({"planning"}),
    "run_command": frozenset({"executing"}),
    "execute_plan_node": frozenset({"executing"}),
    "report_blocked": frozenset({"planning", "executing", "mapping"}),
}


class RuntimeRolePolicy:
    """Authorize the common registry by role; role/tool input exits allowed or structured denied."""

    @staticmethod
    def manifest(role: RuntimeRole) -> dict[str, dict[str, bool]]:
        """Build frontend/model permissions; role input returns every shared tool and allowed flag."""
        return {
            tool_name: {"allowed": role in allowed_roles}
            for tool_name, allowed_roles in _SHARED_TOOL_RULES.items()
        }

    @staticmethod
    def require(role: RuntimeRole, tool_name: str) -> None:
        """Enforce one tool call; role/name input exits silently or raises fail-closed denial."""
        allowed_roles = _SHARED_TOOL_RULES.get(tool_name, frozenset())
        if role not in allowed_roles:
            raise ForbiddenError(
                resource="runtime_tool",
                message="Tool is not allowed for the current role",
                details={"role": role, "tool_name": tool_name},
                error_code="role_capability_denied",
            )

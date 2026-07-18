"""Authorization tests for shared planning/executing runtime tools."""
from __future__ import annotations

import pytest

from bridle.agent.runtime.role_policy import RuntimeRolePolicy
from bridle.api.errors import ForbiddenError


def test_roles_share_tool_names_but_have_different_permissions() -> None:
    """Compare role manifests; role input exits with identical names and different allowed flags."""
    planning = RuntimeRolePolicy.manifest("planning")
    executing = RuntimeRolePolicy.manifest("executing")

    assert set(planning) == set(executing)
    assert "switch_role" not in planning
    assert "child_agent_result_summary" not in planning
    assert planning["patch_plan_nodes"]["allowed"] is True
    assert planning["execute_plan_node"]["allowed"] is False
    assert planning["run_command"]["allowed"] is False
    assert executing["patch_plan_nodes"]["allowed"] is False
    assert executing["execute_plan_node"]["allowed"] is True
    assert executing["run_command"]["allowed"] is True


def test_policy_rejects_planning_code_write_with_structured_error() -> None:
    """Authorize a planning source write; role/tool input exits with role_capability_denied."""
    with pytest.raises(ForbiddenError) as error:
        RuntimeRolePolicy.require("planning", "execute_plan_node")

    assert error.value.api_error.code == "role_capability_denied"
    assert error.value.api_error.details == {
        "role": "planning",
        "tool_name": "execute_plan_node",
    }


def test_policy_allows_execution_and_rejects_unknown_tools() -> None:
    """Authorize executing/unknown tools; inputs exit allowed or fail closed with structured denial."""
    RuntimeRolePolicy.require("executing", "run_command")

    with pytest.raises(ForbiddenError) as error:
        RuntimeRolePolicy.require("executing", "switch_role")
    assert error.value.api_error.code == "role_capability_denied"


def test_sidecar_role_is_read_only() -> None:
    """Build sidecar manifest; role input exits with no write or execution tools visible."""
    sidecar = RuntimeRolePolicy.manifest("sidecar")

    assert sidecar["read_project_map"]["allowed"] is True
    assert sidecar["patch_plan_nodes"]["allowed"] is False
    assert sidecar["execute_plan_node"]["allowed"] is False
    assert sidecar["run_command"]["allowed"] is False


def test_mapping_role_can_read_map_and_propose_annotations_only() -> None:
    mapping = RuntimeRolePolicy.manifest("mapping")
    assert mapping["read_project_map"]["allowed"] is True
    assert mapping["propose_semantic_annotation"]["allowed"] is True
    assert mapping["execute_plan_node"]["allowed"] is False
    assert mapping["patch_plan_nodes"]["allowed"] is False


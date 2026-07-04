"""Authorization tests for shared planning/executing runtime tools."""
from __future__ import annotations

import pytest

from bridle.api.errors import ForbiddenError
from bridle.agent.runtime.role_policy import RuntimeRolePolicy


def test_roles_share_tool_names_but_have_different_permissions() -> None:
    """Compare role manifests; role input exits with identical names and different allowed flags."""
    planning = RuntimeRolePolicy.manifest("planning")
    executing = RuntimeRolePolicy.manifest("executing")

    assert set(planning) == set(executing)
    assert "switch_role" not in planning
    assert planning["patch_plan_nodes"]["allowed"] is True
    assert planning["read_workspace_file"]["allowed"] is True
    assert planning["propose_file_patch"]["allowed"] is False
    assert planning["run_allowed_tests"]["allowed"] is False
    assert planning["select_node"]["allowed"] is False
    assert executing["patch_plan_nodes"]["allowed"] is False
    assert executing["propose_file_patch"]["allowed"] is True
    assert executing["run_allowed_tests"]["allowed"] is True
    assert executing["select_node"]["allowed"] is True


def test_policy_rejects_planning_code_write_with_structured_error() -> None:
    """Authorize a planning source write; role/tool input exits with role_capability_denied."""
    with pytest.raises(ForbiddenError) as error:
        RuntimeRolePolicy.require("planning", "propose_file_patch")

    assert error.value.api_error.code == "role_capability_denied"
    assert error.value.api_error.details == {
        "role": "planning",
        "tool_name": "propose_file_patch",
    }


def test_policy_allows_execution_and_rejects_unknown_tools() -> None:
    """Authorize executing/unknown tools; inputs exit allowed or fail closed with structured denial."""
    RuntimeRolePolicy.require("executing", "run_allowed_tests")

    with pytest.raises(ForbiddenError) as error:
        RuntimeRolePolicy.require("executing", "switch_role")
    assert error.value.api_error.code == "role_capability_denied"


def test_sidecar_role_is_read_and_summary_only() -> None:
    """Build sidecar manifest; role input exits with no write or execution tools visible."""
    sidecar = RuntimeRolePolicy.manifest("sidecar")

    assert sidecar["read_project_map"]["allowed"] is True
    assert sidecar["read_workspace_file"]["allowed"] is True
    assert sidecar["search_code"]["allowed"] is True
    assert sidecar["child_agent_result_summary"]["allowed"] is True
    assert sidecar["patch_plan_nodes"]["allowed"] is False
    assert sidecar["propose_file_patch"]["allowed"] is False
    assert sidecar["run_allowed_tests"]["allowed"] is False
    assert sidecar["select_node"]["allowed"] is False


def test_mapping_role_can_read_map_and_propose_annotations_only() -> None:
    mapping = RuntimeRolePolicy.manifest("mapping")
    assert mapping["read_code_map"]["allowed"] is True
    assert mapping["propose_semantic_annotation"]["allowed"] is True
    assert mapping["propose_file_patch"]["allowed"] is False
    assert mapping["patch_plan_nodes"]["allowed"] is False


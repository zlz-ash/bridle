"""Tests for SandboxPolicy."""
from __future__ import annotations

from pathlib import Path

import pytest

from bridle.engine.sandbox_policy import SandboxPolicy


@pytest.fixture
def policy(test_workspace: Path) -> SandboxPolicy:
    return SandboxPolicy.for_run(
        run_id="run-1",
        node_id="node-1",
        workspace_root=test_workspace,
        allowed_files=["src/a.py", "backend/tests/foo.py"],
        node_tests=["pytest backend/tests/", "npm test"],
    )


class TestSandboxPolicyPaths:
    def test_allowed_file_permitted(self, policy: SandboxPolicy) -> None:
        assert policy.validate_read_path("src/a.py") == []
        assert policy.validate_patch_path("backend/tests/foo.py") == []

    def test_parent_traversal_rejected(self, policy: SandboxPolicy) -> None:
        errors = policy.validate_read_path("../secret.py")
        assert errors

    def test_c_drive_rejected(self, policy: SandboxPolicy) -> None:
        errors = policy.validate_read_path(r"C:\Windows\system.ini")
        assert any("C:" in e or "absolute" in e.lower() for e in errors)

    def test_outside_workspace_on_d_drive_rejected(self, policy: SandboxPolicy, test_workspace: Path) -> None:
        other = Path("D:/Other/project/x.py")
        if str(other.resolve()).lower().startswith(str(test_workspace.resolve()).lower()):
            pytest.skip("test workspace is on D:/Other")
        errors = policy.validate_read_path(r"D:\Other\x.py")
        assert errors

    def test_e_drive_rejected(self, policy: SandboxPolicy) -> None:
        errors = policy.validate_read_path(r"E:\tmp\a.py")
        assert errors

    def test_not_in_allowed_files_rejected(self, policy: SandboxPolicy) -> None:
        errors = policy.validate_read_path("src/other.py")
        assert any("allowed" in e.lower() for e in errors)


class TestSandboxPolicyDefaults:
    def test_network_dependency_env_disabled(self, policy: SandboxPolicy) -> None:
        assert policy.network_allowed is False
        assert policy.dependency_install_allowed is False
        assert policy.env_visible is False

    def test_default_timeout_60(self, policy: SandboxPolicy) -> None:
        assert policy.command_timeout_seconds == 60

    def test_timeout_over_global_cap_rejected(self, test_workspace: Path) -> None:
        with pytest.raises(ValueError):
            SandboxPolicy.for_run(
                run_id="r",
                node_id="n",
                workspace_root=test_workspace,
                allowed_files=[],
                node_tests=[],
                command_timeout_seconds=400,
            )


class TestSandboxPolicyCommands:
    def test_allowed_node_test_command(self, policy: SandboxPolicy) -> None:
        assert policy.validate_test_command("pytest backend/tests/") == []

    def test_rejects_npm_install(self, policy: SandboxPolicy) -> None:
        assert policy.validate_test_command("npm install lodash") != []

    def test_rejects_command_not_in_node_tests(self, policy: SandboxPolicy) -> None:
        assert policy.validate_test_command("pytest tests/") != []

    def test_rejects_powershell(self, policy: SandboxPolicy) -> None:
        assert policy.validate_test_command("powershell -Command echo hi") != []

    def test_requires_network_marker_does_not_bypass_network_denial(
        self,
        test_workspace: Path,
    ) -> None:
        command = "echo wget requires_network"
        p = SandboxPolicy.for_run(
            run_id="r",
            node_id="n",
            workspace_root=test_workspace,
            allowed_files=[],
            node_tests=[command],
        )
        errors = p.validate_test_command(command)
        assert any("Network access is disabled" in e for e in errors)

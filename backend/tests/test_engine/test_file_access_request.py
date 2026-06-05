"""Tests for file access request evaluation."""
from __future__ import annotations

from pathlib import Path

from bridle.engine.file_access_request import evaluate_file_access


class TestEvaluateFileAccess:
    def test_low_risk_init_auto_approves(self, test_workspace: Path) -> None:
        decision = evaluate_file_access(
            "src/__init__.py",
            workspace_root=test_workspace,
            allowed_files=frozenset({"src/main.py"}),
        )
        assert decision.risk_level == "low"
        assert decision.auto_approve is True
        assert decision.normalized_path == "src/__init__.py"

    def test_high_risk_outside_allowed_not_auto_approved(self, test_workspace: Path) -> None:
        decision = evaluate_file_access(
            "src/extra_module.py",
            workspace_root=test_workspace,
            allowed_files=frozenset({"src/main.py"}),
        )
        assert decision.risk_level == "high"
        assert decision.auto_approve is False

    def test_c_drive_is_high_risk(self, test_workspace: Path) -> None:
        decision = evaluate_file_access(
            "C:/Windows/evil.py",
            workspace_root=test_workspace,
            allowed_files=frozenset(),
        )
        assert decision.risk_level == "high"
        assert decision.auto_approve is False

    def test_already_allowed_is_auto_approve(self, test_workspace: Path) -> None:
        decision = evaluate_file_access(
            "src/main.py",
            workspace_root=test_workspace,
            allowed_files=frozenset({"src/main.py"}),
        )
        assert decision.auto_approve is True

    def test_fixture_path_is_low_risk(self, test_workspace: Path) -> None:
        decision = evaluate_file_access(
            "tests/fixtures/sample.json",
            workspace_root=test_workspace,
            allowed_files=frozenset({"tests/test_main.py"}),
        )
        assert decision.risk_level == "low"
        assert decision.auto_approve is True

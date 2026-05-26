"""Git checkpoint integration snapshot and rollback tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridle.services.git_checkpoint_service import GitCheckpointService


class TestGitCheckpointIntegrationFlow:
    def test_begin_snapshot_covers_declared_paths(self, test_workspace: Path) -> None:
        git_dir = test_workspace / ".git" / "refs" / "heads"
        git_dir.mkdir(parents=True, exist_ok=True)
        (test_workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "main").write_text("a" * 40 + "\n", encoding="utf-8")
        target = test_workspace / "src" / "router.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("[]\n", encoding="utf-8")

        service = GitCheckpointService(test_workspace)
        state = service.begin_integration(
            "session-1",
            snapshot_paths=["src/router.json", "src/a.py"],
        )

        assert state["phase"] == "pre_integration"
        snapshot = Path(state["snapshot_path"])
        assert (snapshot / "src" / "router.json").exists()

    def test_rollback_restores_pre_integration_files(self, test_workspace: Path) -> None:
        git_dir = test_workspace / ".git" / "refs" / "heads"
        git_dir.mkdir(parents=True, exist_ok=True)
        (test_workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "main").write_text("a" * 40 + "\n", encoding="utf-8")
        file_path = test_workspace / "src" / "a.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("before\n", encoding="utf-8")

        service = GitCheckpointService(test_workspace)
        service.begin_integration("session-1", snapshot_paths=["src/a.py"])
        file_path.write_text("after failed apply\n", encoding="utf-8")

        service.rollback_integration("session-1")

        assert file_path.read_text(encoding="utf-8") == "before\n"

    def test_rollback_removes_files_created_during_integration(
        self,
        test_workspace: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        git_dir = test_workspace / ".git" / "refs" / "heads"
        git_dir.mkdir(parents=True, exist_ok=True)
        (test_workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "main").write_text("a" * 40 + "\n", encoding="utf-8")

        service = GitCheckpointService(test_workspace)
        service.begin_integration("session-new", snapshot_paths=["src/new.py"])
        created = test_workspace / "src" / "new.py"
        created.parent.mkdir(parents=True, exist_ok=True)
        created.write_text("created\n", encoding="utf-8")

        service.rollback_integration("session-new")

        if created.exists():
            assert any(record.message == "path_delete_failed" for record in caplog.records)
        else:
            assert not created.exists()

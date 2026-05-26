"""Git baseline checkpoint helpers for integration flows."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from bridle.engine.git_workspace_policy import GitWorkspacePolicy
from bridle.schemas.node import _validate_workspace_relative_path

logger = logging.getLogger("bridle")


class GitCheckpointService:
    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.policy = GitWorkspacePolicy()
        self._rollback_root = self.workspace_root / ".aicoding" / "integration-rollback"
        self._state_root = self.workspace_root / ".aicoding" / "integration-state"

    def begin_integration(self, session_id: str, *, snapshot_paths: list[str]) -> dict[str, Any]:
        check = self.policy.evaluate(self.workspace_root)
        if not check.ok:
            raise ValueError(check.error_code or "git_preflight_failed")
        snapshot_dir, absent_paths = self._snapshot_paths(session_id, snapshot_paths)
        payload: dict[str, Any] = {
            "session_id": session_id,
            "phase": "pre_integration",
            "baseline_revision": check.baseline_revision,
            "snapshot_path": str(snapshot_dir),
            "absent_paths": absent_paths,
        }
        self._write_state(session_id, payload)
        logger.info(
            "integration_snapshot_created",
            extra={
                "action": "integration_snapshot_created",
                "status": "completed",
                "detail": {"session_id": session_id, "path_count": len(snapshot_paths)},
            },
        )
        return payload

    def commit_after_integration(
        self,
        session_id: str,
        *,
        commit_paths: list[str],
        message: str = "bridle: integrate node output",
    ) -> dict[str, Any]:
        check = self.policy.evaluate(self.workspace_root)
        if not check.ok:
            raise ValueError(check.error_code or "git_preflight_failed")
        before_revision = check.baseline_revision
        after_revision = self._git_commit(commit_paths, message=message) or before_revision
        payload: dict[str, Any] = {
            "session_id": session_id,
            "phase": "post_integration",
            "baseline_revision": after_revision,
            "before_revision": before_revision,
            "status": "committed",
        }
        checkpoint_path = self._checkpoint_path(session_id)
        checkpoint_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        self._clear_state(session_id)
        logger.info(
            "git_checkpoint_committed",
            extra={
                "action": "git_checkpoint_committed",
                "status": "completed",
                "detail": {"session_id": session_id, "baseline_revision": after_revision},
            },
        )
        return payload

    def rollback_integration(self, session_id: str) -> None:
        state = self._read_state(session_id)
        if not state or state.get("phase") != "pre_integration":
            return
        snapshot_path = Path(str(state.get("snapshot_path", "")))
        if not snapshot_path.exists():
            return
        for rel_path in state.get("absent_paths", []):
            normalized = _validate_workspace_relative_path(str(rel_path))
            target = self.workspace_root / normalized
            if target.exists():
                self._safe_remove_path(target)
        baseline_revision = state.get("baseline_revision")
        if baseline_revision:
            if self._git_available():
                subprocess.run(
                    ["git", "-C", str(self.workspace_root), "reset", "--hard", str(baseline_revision)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            else:
                self._write_fake_head(str(baseline_revision))
        for rel_path in self._list_snapshot_files(snapshot_path):
            source = snapshot_path / rel_path
            target = self.workspace_root / rel_path
            if source.is_dir():
                if target.exists():
                    self._safe_remove_path(target)
                shutil.copytree(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.exists():
                    shutil.copy2(source, target)
                elif target.exists():
                    self._safe_remove_path(target)
        self._clear_state(session_id)
        logger.info(
            "integration_rollback_completed",
            extra={
                "action": "integration_rollback_completed",
                "status": "completed",
                "detail": {"session_id": session_id},
            },
        )

    def create_checkpoint(
        self,
        session_id: str,
        *,
        after_integration: bool = False,
    ) -> dict[str, Any]:
        """Backward-compatible checkpoint metadata write."""
        check = self.policy.evaluate(self.workspace_root)
        if not check.ok:
            raise ValueError(check.error_code or "git_preflight_failed")
        payload: dict[str, Any] = {
            "session_id": session_id,
            "baseline_revision": check.baseline_revision,
            "status": "created",
            "after_integration": after_integration,
        }
        self._checkpoint_path(session_id).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return payload

    def rollback_last_integration(self, session_id: str) -> None:
        self.rollback_integration(session_id)

    def assert_baseline_matches(self, expected_revision: str) -> None:
        check = self.policy.evaluate(self.workspace_root)
        if not check.ok:
            raise ValueError(check.error_code or "git_preflight_failed")
        if check.baseline_revision != expected_revision:
            raise ValueError("git_baseline_mismatch")

    def _snapshot_paths(self, session_id: str, snapshot_paths: list[str]) -> tuple[Path, list[str]]:
        snapshot_dir = self._rollback_root / session_id
        if snapshot_dir.exists():
            self._safe_remove_path(snapshot_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        absent_paths: list[str] = []
        for rel in snapshot_paths:
            normalized = _validate_workspace_relative_path(rel)
            source = self.workspace_root / normalized
            target = snapshot_dir / normalized
            if not source.exists():
                absent_paths.append(normalized)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        return snapshot_dir, absent_paths

    def _list_snapshot_files(self, snapshot_dir: Path) -> list[str]:
        paths: list[str] = []
        for path in snapshot_dir.rglob("*"):
            if path.is_file():
                paths.append(path.relative_to(snapshot_dir).as_posix())
        return paths

    def _git_commit(self, commit_paths: list[str], *, message: str) -> str | None:
        if not self._git_available():
            return self._advance_fake_head()
        for rel in commit_paths:
            normalized = _validate_workspace_relative_path(rel)
            subprocess.run(
                ["git", "-C", str(self.workspace_root), "add", "--", normalized],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        result = subprocess.run(
            ["git", "-C", str(self.workspace_root), "commit", "-m", message, "--allow-empty"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 and "nothing to commit" not in (result.stdout + result.stderr):
            raise ValueError("git_commit_failed")
        rev = subprocess.run(
            ["git", "-C", str(self.workspace_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if rev.returncode == 0:
            revision = rev.stdout.strip()
            if len(revision) == 40:
                return revision
        return None

    def _advance_fake_head(self) -> str | None:
        check = self.policy.evaluate(self.workspace_root)
        if not check.ok or not check.baseline_revision:
            return None
        new_revision = "f" + check.baseline_revision[1:]
        self._write_fake_head(new_revision)
        return new_revision

    def _git_available(self) -> bool:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.workspace_root), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0 and result.stdout.strip() == "true"
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _write_fake_head(self, revision: str) -> None:
        git_dir = self.workspace_root / ".git" / "refs" / "heads"
        git_dir.mkdir(parents=True, exist_ok=True)
        (git_dir / "main").write_text(revision + "\n", encoding="utf-8")

    def _state_path(self, session_id: str) -> Path:
        self._state_root.mkdir(parents=True, exist_ok=True)
        return self._state_root / f"{session_id}.json"

    def _write_state(self, session_id: str, payload: dict[str, Any]) -> None:
        self._state_path(session_id).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _read_state(self, session_id: str) -> dict[str, Any] | None:
        path = self._state_path(session_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _clear_state(self, session_id: str) -> None:
        path = self._state_path(session_id)
        if not path.exists():
            return
        for attempt in range(3):
            try:
                path.unlink()
                return
            except PermissionError:
                if attempt == 2:
                    logger.warning(
                        "integration_state_cleanup_deferred",
                        extra={
                            "action": "integration_state_cleanup_deferred",
                            "status": "warning",
                            "detail": {"session_id": session_id, "path": str(path)},
                        },
                    )
                    return
                time.sleep(0.05)

    def _safe_remove_path(self, path: Path) -> None:
        for attempt in range(3):
            try:
                try:
                    path.chmod(0o666)
                    path.parent.chmod(0o777)
                except OSError:
                    pass
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                return
            except FileNotFoundError:
                return
            except (PermissionError, OSError):
                if attempt < 2:
                    time.sleep(0.05)

        pending_root = self.workspace_root / ".aicoding" / "delete-pending"
        pending_root.mkdir(parents=True, exist_ok=True)
        pending = pending_root / f"{path.name}.{uuid4().hex}"
        for attempt in range(3):
            try:
                path.replace(pending)
                logger.warning(
                    "path_delete_deferred",
                    extra={
                        "action": "path_delete_deferred",
                        "status": "warning",
                        "detail": {"source": str(path), "pending": str(pending)},
                    },
                )
                return
            except FileNotFoundError:
                return
            except (PermissionError, OSError):
                if attempt < 2:
                    time.sleep(0.05)
        logger.warning(
            "path_delete_failed",
            extra={
                "action": "path_delete_failed",
                "status": "warning",
                "detail": {"source": str(path), "pending": str(pending)},
            },
        )
        return

    def _checkpoint_path(self, session_id: str) -> Path:
        root = self.workspace_root / ".aicoding" / "git-checkpoints"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{session_id}.json"

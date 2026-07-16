from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.agent.runtime.mailbox import AgentAddress, MailEnvelope
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
from bridle.agent.runtime.project_registry import ProjectRuntimeRegistry
from bridle.features.projects.service import ProjectService


def _project(test_workspace: Path, name: str) -> Path:
    root = test_workspace / name
    root.mkdir()
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


@pytest.mark.asyncio
async def test_open_project_initializes_storage_without_starting_map_runtime(
    db: AsyncSession,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProjectRuntimeRegistry()
    monkeypatch.setattr(
        "bridle.features.projects.service.get_project_runtime_registry",
        lambda: registry,
        raising=False,
    )
    root = _project(test_workspace, "storage-only")

    first = await ProjectService.open_project(db, str(root))
    target = AgentAddress(first.id, "map-runtime", 1)
    mailbox = PersistentMailbox(
        root / ".bridle" / "mail.db",
        project_id=first.id,
        consumer_id="pending-before-reopen",
        default_target=target,
    )
    mailbox.enqueue(
        MailEnvelope(
            "pending-before-reopen",
            "Other",
            AgentAddress(first.id, "producer", 1),
            target,
            {"value": 1},
        )
    )
    await mailbox.close()

    try:
        second = await ProjectService.open_project(db, str(root))
        assert first.id == second.id
        assert registry.active_count == 0
        assert {path.name for path in (root / ".bridle").glob("*.db")} == {
            "change_outbox.db",
            "mail.db",
            "plan.db",
        }
    finally:
        await registry.stop_all()


@pytest.mark.asyncio
async def test_commit_failure_does_not_create_runtime(
    db: AsyncSession,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProjectRuntimeRegistry()
    monkeypatch.setattr(
        "bridle.features.projects.service.get_project_runtime_registry",
        lambda: registry,
        raising=False,
    )

    async def fail_commit() -> None:
        raise RuntimeError("commit_failed")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit_failed"):
        await ProjectService.open_project(db, str(_project(test_workspace, "commit")))

    assert registry.active_count == 0

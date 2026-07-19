from __future__ import annotations

from bridle.agent.runtime import gateway as gateway_module
from bridle.features.sessions.schemas import ProjectMessageCreateSchema
from bridle.features.sessions.service import ProjectSessionService
from bridle.models.project import ProjectRecord


async def _create_session(db, test_workspace):
    project = ProjectRecord(path=str(test_workspace), name="checkpoint-project")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return await ProjectSessionService.create(
        db,
        project_id=project.id,
        title="checkpoint-session",
    )


async def _message(db, session_id: str, role: str, content: str):
    return await ProjectSessionService.create_message(
        db,
        session_id,
        ProjectMessageCreateSchema(role=role, content=content),
    )


async def test_session_memory_checkpoint_restores_only_post_anchor_delta(
    db,
    test_workspace,
    monkeypatch,
) -> None:
    manager_type = gateway_module.SessionMemoryWindowManager
    await ProjectSessionService.ensure_memory_table(db)
    await ProjectSessionService.ensure_memory_table(db)
    session = await _create_session(db, test_workspace)
    old = await _message(db, session.id, "user", "old-" + "x" * 40)
    retained = await _message(db, session.id, "assistant", "retained-after-anchor")

    optimizer_calls: list[tuple[str, list[str]]] = []

    async def optimizer(summary: str, evicted: list[dict]) -> str:
        contents = [str(item.get("content", "")) for item in evicted]
        optimizer_calls.append((summary, contents))
        return "|".join(part for part in (summary, *contents) if part)

    first_manager = manager_type(budget=20, recent_window=1, optimizer=optimizer)
    await first_manager.context_for_turn(
        db,
        session.id,
        current_message=retained,
    )
    checkpoint = await ProjectSessionService.get_memory_checkpoint(db, session.id)
    assert checkpoint is not None
    assert checkpoint.anchor_message_id == old.id
    checkpoint_anchor = checkpoint.anchor_message_id
    checkpoint_summary = checkpoint.summary

    current = await _message(db, session.id, "user", "current-after-restart")
    original_list_after = ProjectSessionService.list_messages_after
    list_after_calls: list[tuple[str, str | None]] = []

    async def tracked_list_after(db_arg, session_id: str, *, after_message_id: str | None):
        list_after_calls.append((session_id, after_message_id))
        return await original_list_after(
            db_arg,
            session_id,
            after_message_id=after_message_id,
        )

    async def forbidden_full_history(*_args, **_kwargs):
        raise AssertionError("cold restore must not read the full message history")

    monkeypatch.setattr(ProjectSessionService, "list_messages_after", tracked_list_after)
    monkeypatch.setattr(ProjectSessionService, "list_messages", forbidden_full_history)

    restarted_manager = manager_type(
        budget=20,
        recent_window=1,
        optimizer=optimizer,
    )
    restored = await restarted_manager.context_for_turn(
        db,
        session.id,
        current_message=current,
    )

    assert list_after_calls == [(session.id, checkpoint_anchor)]
    assert sum(item.get("id") == current.id for item in restored) == 0
    assert all(item.get("id") != old.id for item in restored)
    assert optimizer_calls[-1] == (checkpoint_summary, [retained.content])
    updated = await ProjectSessionService.get_memory_checkpoint(db, session.id)
    assert updated is not None
    assert updated.anchor_message_id == retained.id

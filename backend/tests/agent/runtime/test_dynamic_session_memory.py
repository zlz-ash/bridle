from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from bridle.agent.runtime import gateway as gateway_module
from bridle.features.sessions.schemas import ProjectMessageCreateSchema
from bridle.features.sessions.service import ProjectSessionService
from bridle.models.project import ProjectRecord


async def _create_session(db, test_workspace):
    project = ProjectRecord(path=str(test_workspace), name="memory-project")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return await ProjectSessionService.create(
        db,
        project_id=project.id,
        title="memory-session",
    )


async def _message(db, session_id: str, role: str, content: str):
    return await ProjectSessionService.create_message(
        db,
        session_id,
        ProjectMessageCreateSchema(role=role, content=content),
    )


async def test_gateway_reuses_session_window_and_reads_only_delta(
    db,
    test_workspace,
    monkeypatch,
) -> None:
    manager_type = gateway_module.SessionMemoryWindowManager
    session = await _create_session(db, test_workspace)
    old = await _message(db, session.id, "user", "old-" + "x" * 40)
    first_current = await _message(db, session.id, "assistant", "first-current")

    calls = 0
    original = ProjectSessionService.list_messages_after
    original_get_checkpoint = ProjectSessionService.get_memory_checkpoint

    async def counted_list_messages_after(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    async def forbidden_full_history(*_args, **_kwargs):
        raise AssertionError("hot path must not call list_messages")

    monkeypatch.setattr(
        ProjectSessionService,
        "list_messages_after",
        counted_list_messages_after,
    )
    monkeypatch.setattr(ProjectSessionService, "list_messages", forbidden_full_history)

    async def optimizer(summary: str, evicted: list[dict]) -> str:
        text = ",".join(str(item.get("content", "")) for item in evicted)
        return "|".join(part for part in (summary, text) if part)

    manager = manager_type(budget=20, recent_window=1, optimizer=optimizer)
    first_window = await manager.context_for_turn(
        db,
        session.id,
        current_message=first_current,
    )
    assert calls == 1
    assert sum(item.get("id") == first_current.id for item in first_window) == 0
    assert all(item.get("id") != old.id for item in first_window)

    async def forbidden_checkpoint_read(*_args, **_kwargs):
        raise AssertionError("hot path must not read the checkpoint again")

    monkeypatch.setattr(
        ProjectSessionService,
        "get_memory_checkpoint",
        forbidden_checkpoint_read,
    )
    second_current = await _message(db, session.id, "user", "second-current")
    second_window = await manager.context_for_turn(
        db,
        session.id,
        current_message=second_current,
    )

    assert calls == 1
    assert sum(item.get("id") == second_current.id for item in second_window) == 0
    assert all(item.get("id") != first_current.id for item in second_window)
    monkeypatch.setattr(
        ProjectSessionService,
        "get_memory_checkpoint",
        original_get_checkpoint,
    )
    checkpoint = await original_get_checkpoint(db, session.id)
    assert checkpoint is not None
    assert checkpoint.anchor_message_id == first_current.id
    assert checkpoint.summary


async def test_gateway_context_has_one_short_term_memory_entry(
    db,
    test_workspace,
) -> None:
    manager_type = gateway_module.SessionMemoryWindowManager
    session = await _create_session(db, test_workspace)
    current = await _message(db, session.id, "user", "current request")
    manager = manager_type(budget=100, recent_window=2)

    window = await manager.context_for_turn(
        db,
        session.id,
        current_message=current,
    )
    context = gateway_module.AgentContext(
        instruction="current request",
        node={"id": "n1"},
        short_term_memory=window,
        accessible_context={"project_map": {}, "skill_ids": [], "session_role": "planning"},
    )

    assert context.short_term_memory == window
    assert "memory" not in context.accessible_context


async def test_cancelled_optimizer_preserves_hot_state_and_checkpoint(
    db,
    test_workspace,
) -> None:
    manager_type = gateway_module.SessionMemoryWindowManager
    session = await _create_session(db, test_workspace)
    first = await _message(db, session.id, "user", "alpha-payload")
    optimizer_entered = asyncio.Event()
    optimizer_calls = 0

    async def optimizer(_summary: str, evicted: list[dict]) -> str:
        nonlocal optimizer_calls
        optimizer_calls += 1
        if optimizer_calls == 1:
            optimizer_entered.set()
            await asyncio.Event().wait()
        return "|".join(
            str(item.get("content", "")).split("-", maxsplit=1)[0]
            for item in evicted
        )

    manager = manager_type(budget=15, recent_window=1, optimizer=optimizer)
    await manager.context_for_turn(
        db,
        session.id,
        current_message=first,
    )
    hot_state = manager._states[session.id]
    hot_messages_before = [dict(message) for message in hot_state.memory._messages]
    summary_before = hot_state.memory.summary
    anchor_before = hot_state.memory.anchor_message_id
    checkpoint_before = await ProjectSessionService.get_memory_checkpoint(db, session.id)

    cancelled_message = await _message(db, session.id, "assistant", "beta-payload")
    cancelled_turn = asyncio.create_task(
        manager.context_for_turn(
            db,
            session.id,
            current_message=cancelled_message,
        )
    )
    await asyncio.wait_for(optimizer_entered.wait(), timeout=5)
    cancelled_turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_turn

    checkpoint_after_cancel = await ProjectSessionService.get_memory_checkpoint(
        db,
        session.id,
    )
    hot_state_after_cancel = manager._states[session.id]
    hot_unchanged = (
        hot_state_after_cancel is hot_state
        and hot_state_after_cancel.memory._messages == hot_messages_before
        and hot_state_after_cancel.memory.summary == summary_before
        and hot_state_after_cancel.memory.anchor_message_id == anchor_before
    )
    checkpoint_unchanged = checkpoint_after_cancel == checkpoint_before

    retry_message = await _message(db, session.id, "user", "gamma")
    retry_window = await manager.context_for_turn(
        db,
        session.id,
        current_message=retry_message,
    )
    rendered_retry = str(retry_window)
    history = await ProjectSessionService.list_messages(db, session.id)

    assert hot_unchanged
    assert checkpoint_unchanged
    assert optimizer_calls == 2
    assert rendered_retry.count("alpha") == 1
    assert rendered_retry.count("beta") == 1
    assert [(message.role, message.content) for message in history] == [
        ("user", "alpha-payload"),
        ("assistant", "beta-payload"),
        ("user", "gamma"),
    ]


def test_pipeline_runtime_artifacts_are_git_ignored() -> None:
    project_root = Path(__file__).resolve().parents[4]
    representative_paths = [
        ".ai-dev-runtime/batches/BATCH-TEST/plan.json",
        ".ai-dev-runtime/state/pipeline-state.json",
        ".ai-dev-runtime/receipts/claims/test.json",
        ".ai-dev-runtime/evidence/test.json",
        ".ai-dev-runtime/logs/pipeline.log",
    ]

    for relative_path in representative_paths:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", relative_path],
            cwd=project_root,
            check=False,
        )
        assert result.returncode == 0, f"runtime artifact is not ignored: {relative_path}"

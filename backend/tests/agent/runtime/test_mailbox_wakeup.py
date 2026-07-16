from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Thread, get_ident
from typing import Any

import pytest


def _api() -> tuple[Any, Any, Any]:
    from bridle.agent.runtime.mailbox import AgentAddress, MailEnvelope
    from bridle.agent.runtime.persistent_mailbox import PersistentMailbox

    return AgentAddress, MailEnvelope, PersistentMailbox


def _mailbox(path: Path, *, hook: Any = None) -> Any:
    AgentAddress, _, PersistentMailbox = _api()
    return PersistentMailbox(
        path,
        project_id="project-wakeup",
        consumer_id="consumer-wakeup",
        busy_timeout_ms=20,
        empty_wait_hook=hook,
        default_target=AgentAddress("project-wakeup", "child", 1),
    )


def _envelope(message_id: str) -> Any:
    AgentAddress, MailEnvelope, _ = _api()
    return MailEnvelope(
        message_id=message_id,
        message_type="TaskAssigned",
        source=AgentAddress("project-wakeup", "parent", 1),
        target=AgentAddress("project-wakeup", "child", 1),
        payload={"value": message_id},
    )


@pytest.mark.asyncio
async def test_cross_thread_enqueue_cannot_miss_registration_window(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = test_workspace / "registration-race" / ".bridle" / "mail.db"
    empty_checked = asyncio.Event()
    release_registration = asyncio.Event()

    async def hook() -> None:
        empty_checked.set()
        await release_registration.wait()

    mailbox = _mailbox(path, hook=hook)
    loop = asyncio.get_running_loop()
    owner_thread = get_ident()
    scheduled_from: list[int] = []
    callbacks_on: list[int] = []
    original_call_soon_threadsafe = loop.call_soon_threadsafe

    def tracked_call_soon_threadsafe(callback: Any, *args: Any) -> Any:
        scheduled_from.append(get_ident())

        def tracked_callback() -> None:
            callbacks_on.append(get_ident())
            callback(*args)

        return original_call_soon_threadsafe(tracked_callback)

    monkeypatch.setattr(loop, "call_soon_threadsafe", tracked_call_soon_threadsafe)
    receive_task = asyncio.create_task(mailbox.receive(timeout=1))
    await asyncio.wait_for(empty_checked.wait(), timeout=1)
    outcome: list[str] = []

    def enqueue_from_thread() -> None:
        outcome.append(mailbox.enqueue(_envelope("cross-thread")).status)

    thread = Thread(target=enqueue_from_thread)
    thread.start()
    thread.join(timeout=1)
    assert not thread.is_alive()
    release_registration.set()
    result = await asyncio.wait_for(receive_task, timeout=1)

    assert outcome == ["inserted"]
    assert result.status == "claimed"
    assert result.message_id == "cross-thread"
    assert mailbox.wake_version >= 1
    assert scheduled_from
    assert all(thread_id != owner_thread for thread_id in scheduled_from)
    assert callbacks_on
    assert set(callbacks_on) == {owner_thread}
    await mailbox.close()


@pytest.mark.asyncio
async def test_close_completes_all_waiters_and_ignores_late_thread_notifications(
    test_workspace: Path,
) -> None:
    path = test_workspace / "close-waiters" / ".bridle" / "mail.db"
    mailbox = _mailbox(path)
    waiters = [asyncio.create_task(mailbox.receive(timeout=1)) for _ in range(3)]
    await asyncio.sleep(0)
    assert mailbox.waiter_count == 3
    await mailbox.close()
    closed_version = mailbox.wake_version
    results = await asyncio.wait_for(asyncio.gather(*waiters), timeout=1)

    thread = Thread(target=mailbox.notify)
    thread.start()
    thread.join(timeout=1)
    assert not thread.is_alive()
    await mailbox.close()

    assert [result.status for result in results] == ["closed", "closed", "closed"]
    assert mailbox.waiter_count == 0
    assert mailbox.wake_version == closed_version


@pytest.mark.asyncio
async def test_enqueue_ignores_loop_close_race_and_notifies_other_signals(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bridle.agent.runtime.mailbox import (
        register_wake_signal,
        unregister_wake_signal,
    )

    class ClosingLoop:
        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, callback: Any, *args: Any) -> None:
            del callback, args
            raise RuntimeError("event loop closed during scheduling")

    path = test_workspace / "loop-close-race" / ".bridle" / "mail.db"
    mailbox = _mailbox(path)
    closing_signal = register_wake_signal(path)
    monkeypatch.setattr(closing_signal, "_loop", ClosingLoop())
    try:
        enqueue_result = mailbox.enqueue(_envelope("loop-close-race"))
        claimed = await mailbox.receive(timeout=1)

        assert enqueue_result.status == "inserted"
        assert claimed.status == "claimed"
        assert claimed.message_id == "loop-close-race"
    finally:
        unregister_wake_signal(closing_signal)
        await mailbox.close()


"""EventBus unit tests."""
from __future__ import annotations

import asyncio

import pytest

from bridle.events.bus import EventBus, RING_BUFFER_SIZE


@pytest.fixture(autouse=True)
def reset_bus() -> None:
    EventBus.reset_for_tests()


@pytest.mark.asyncio
async def test_publish_subscribe_order() -> None:
    bus = EventBus.instance()
    task = asyncio.create_task(_collect(bus, count=2))
    await asyncio.sleep(0.01)
    bus.publish("chat_message_appended", {"message_id": "m1"})
    bus.publish("node_status_changed", {"node_id": "n1"})
    events = await asyncio.wait_for(task, timeout=2)
    assert [e.type for e in events] == ["chat_message_appended", "node_status_changed"]
    assert events[0].seq == 1
    assert events[1].seq == 2


@pytest.mark.asyncio
async def test_replay_after_last_seq() -> None:
    bus = EventBus.instance()
    bus.publish("a", {"x": 1})
    bus.publish("b", {"x": 2})
    collected = []
    async for event in bus.subscribe(last_seq=1):
        collected.append(event)
        if len(collected) >= 1:
            break
    assert collected[0].seq == 2


@pytest.mark.asyncio
async def test_type_filter() -> None:
    bus = EventBus.instance()
    task = asyncio.create_task(_collect(bus, count=1, types={"chat_message_appended"}))
    await asyncio.sleep(0.01)
    bus.publish("node_status_changed", {"node_id": "n1"})
    bus.publish("chat_message_appended", {"message_id": "m1"})
    events = await asyncio.wait_for(task, timeout=2)
    assert events[0].type == "chat_message_appended"


@pytest.mark.asyncio
async def test_ring_buffer_evicts_old_events() -> None:
    bus = EventBus.instance()
    for index in range(RING_BUFFER_SIZE + 5):
        bus.publish("t", {"i": index})
    assert bus._ring[0].seq == 6


async def _collect(
    bus: EventBus,
    *,
    count: int,
    types: set[str] | None = None,
):
    collected = []
    async for event in bus.subscribe(types=types):
        collected.append(event)
        if len(collected) >= count:
            break
    return collected

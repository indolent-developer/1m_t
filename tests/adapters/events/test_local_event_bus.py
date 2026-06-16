"""Tests for LocalEventBus — subscribe, emit, unsubscribe, async handlers, isolation."""
import asyncio
import sys
import os
from dataclasses import dataclass
from enum import Enum

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../src"))

import pytest
from adapters.events.local_event_bus import LocalEventBus


# ── Test fixtures ─────────────────────────────────────────────────────────────

class Event(str, Enum):
    FOO = "foo"
    BAR = "bar"


@dataclass
class Payload:
    event:  Event
    broker_id: str = "test"
    data:   object = None


def _bus() -> LocalEventBus:
    return LocalEventBus()


# ── subscribe / emit ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_handler_called():
    bus, calls = _bus(), []
    bus.subscribe(Event.FOO, lambda p: calls.append(p))
    await bus.emit(Payload(event=Event.FOO))
    assert len(calls) == 1
    assert calls[0].event == Event.FOO


@pytest.mark.asyncio
async def test_emit_wrong_event_not_called():
    bus, calls = _bus(), []
    bus.subscribe(Event.BAR, lambda p: calls.append(p))
    await bus.emit(Payload(event=Event.FOO))
    assert calls == []


@pytest.mark.asyncio
async def test_multiple_handlers_all_called():
    bus = _bus()
    calls = []
    bus.subscribe(Event.FOO, lambda p: calls.append("h1"))
    bus.subscribe(Event.FOO, lambda p: calls.append("h2"))
    bus.subscribe(Event.FOO, lambda p: calls.append("h3"))
    await bus.emit(Payload(event=Event.FOO))
    assert calls == ["h1", "h2", "h3"]


@pytest.mark.asyncio
async def test_emit_no_handlers_is_noop():
    bus = _bus()
    await bus.emit(Payload(event=Event.FOO))   # must not raise


# ── subscribe_all / wildcard ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_all_fires_on_any_event():
    bus, calls = _bus(), []
    bus.subscribe_all(lambda p: calls.append(p.event))
    await bus.emit(Payload(event=Event.FOO))
    await bus.emit(Payload(event=Event.BAR))
    assert calls == [Event.FOO, Event.BAR]


@pytest.mark.asyncio
async def test_specific_and_wildcard_both_fire():
    bus, calls = _bus(), []
    bus.subscribe(Event.FOO, lambda p: calls.append("specific"))
    bus.subscribe_all(lambda p: calls.append("wildcard"))
    await bus.emit(Payload(event=Event.FOO))
    assert "specific" in calls
    assert "wildcard" in calls
    assert len(calls) == 2


# ── unsubscribe ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unsubscribe_stops_handler():
    bus, calls = _bus(), []
    handler = lambda p: calls.append(p)
    bus.subscribe(Event.FOO, handler)
    bus.unsubscribe(Event.FOO, handler)
    await bus.emit(Payload(event=Event.FOO))
    assert calls == []


@pytest.mark.asyncio
async def test_unsubscribe_only_removes_target_handler():
    bus, calls = _bus(), []
    h1 = lambda p: calls.append("h1")
    h2 = lambda p: calls.append("h2")
    bus.subscribe(Event.FOO, h1)
    bus.subscribe(Event.FOO, h2)
    bus.unsubscribe(Event.FOO, h1)
    await bus.emit(Payload(event=Event.FOO))
    assert calls == ["h2"]


@pytest.mark.asyncio
async def test_unsubscribe_all_removes_wildcard():
    bus, calls = _bus(), []
    handler = lambda p: calls.append(p)
    bus.subscribe_all(handler)
    bus.unsubscribe_all(handler)
    await bus.emit(Payload(event=Event.FOO))
    assert calls == []


# ── async handlers ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_async_handler_is_awaited():
    bus, calls = _bus(), []

    async def async_handler(p):
        await asyncio.sleep(0)
        calls.append("async")

    bus.subscribe(Event.FOO, async_handler)
    await bus.emit(Payload(event=Event.FOO))
    assert calls == ["async"]


@pytest.mark.asyncio
async def test_mixed_sync_async_handlers():
    bus, calls = _bus(), []

    async def async_h(p): calls.append("async")
    def sync_h(p):        calls.append("sync")

    bus.subscribe(Event.FOO, sync_h)
    bus.subscribe(Event.FOO, async_h)
    await bus.emit(Payload(event=Event.FOO))
    assert set(calls) == {"sync", "async"}


# ── error isolation ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failing_handler_does_not_block_others():
    bus, calls = _bus(), []

    def bad_handler(p): raise RuntimeError("boom")
    def good_handler(p): calls.append("ok")

    bus.subscribe(Event.FOO, bad_handler)
    bus.subscribe(Event.FOO, good_handler)
    await bus.emit(Payload(event=Event.FOO))   # must not raise
    assert calls == ["ok"]


@pytest.mark.asyncio
async def test_failing_async_handler_does_not_block_others():
    bus, calls = _bus(), []

    async def bad_handler(p): raise ValueError("async boom")
    def good_handler(p): calls.append("ok")

    bus.subscribe(Event.FOO, bad_handler)
    bus.subscribe(Event.FOO, good_handler)
    await bus.emit(Payload(event=Event.FOO))
    assert calls == ["ok"]


# ── listener_count ────────────────────────────────────────────────────────────

def test_listener_count_specific():
    bus = _bus()
    assert bus.listener_count(Event.FOO) == 0
    bus.subscribe(Event.FOO, lambda p: None)
    assert bus.listener_count(Event.FOO) == 1
    bus.subscribe(Event.FOO, lambda p: None)
    assert bus.listener_count(Event.FOO) == 2


def test_listener_count_includes_wildcard():
    bus = _bus()
    bus.subscribe(Event.FOO, lambda p: None)
    bus.subscribe_all(lambda p: None)
    # specific query includes wildcard in the count
    assert bus.listener_count(Event.FOO) == 2


def test_listener_count_total():
    bus = _bus()
    bus.subscribe(Event.FOO, lambda p: None)
    bus.subscribe(Event.BAR, lambda p: None)
    bus.subscribe_all(lambda p: None)
    assert bus.listener_count() == 3


def test_listener_count_after_unsubscribe():
    bus = _bus()
    h = lambda p: None
    bus.subscribe(Event.FOO, h)
    bus.unsubscribe(Event.FOO, h)
    assert bus.listener_count(Event.FOO) == 0

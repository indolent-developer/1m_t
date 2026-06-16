"""
Tests for BaseBroker — init, _emit, reconnect, check_risk_limits, background tasks.

Uses a minimal concrete stub (StubBroker) to satisfy the abstract interface
without touching any real broker API.
"""
from __future__ import annotations

import asyncio
import sys
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../src"))

import pytest
from adapters.brokers.base_broker import BaseBroker
from adapters.brokers.entities.broker_event import BrokerEvent
from adapters.brokers.entities.broker_event_payload import BrokerEventPayload
from adapters.events.local_event_bus import LocalEventBus
from core.adapters.event_bus import IEventBus
from core.entities.broker_capabilities import BrokerCapabilities
from core.entities.broker_entities import AccountInfo, Order, OrderSide, OrderStatus, OrderType
from core.entities.market_quotes import Quote
from core.entities.position_types import Position


# ── Minimal concrete stub ─────────────────────────────────────────────────────

@dataclass
class StubConfig:
    broker_id:       str   = "stub_demo"
    is_demo:         bool  = True
    loan_amount:     float = 50_000.0
    equity_floor:    float = 55_000.0
    hard_max_loss:   float = 20_000.0
    starting_equity: float = 122_562.0


class StubBroker(BaseBroker):
    """Minimal concrete broker — all abstract methods are no-ops."""

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities()

    @property
    def supports_fractional_shares(self) -> bool:
        return False

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> bool:
        return True

    async def get_account_info(self) -> AccountInfo: ...
    async def get_position(self, symbol): ...
    async def get_positions(self, symbol=None): return []
    async def close_position(self, position_id, quantity=None): return True
    async def place_order(self, symbol, quantity, side, order_type, price=None, **kw): ...
    async def cancel_order(self, order_id): return True
    async def get_order(self, order_id): ...
    async def get_orders(self, symbol=None, status=None): return []
    async def get_quote(self, symbol): ...
    async def get_quotes(self, symbols): return {}


def _broker(event_bus=None) -> StubBroker:
    return StubBroker(StubConfig(), event_bus=event_bus)


def _account(current_value: float) -> AccountInfo:
    return AccountInfo(
        account_id="A1", account_name="Test", status="active",
        account_type="margin", currency="USD",
        cash_in_hand=current_value, current_value=current_value,
        margin_used=0.0, margin_available=current_value, leverage=1.0,
    )


# ── __init__ ──────────────────────────────────────────────────────────────────

def test_broker_id_from_config():
    b = _broker()
    assert b.broker_id == "stub_demo"

def test_broker_id_fallback_to_classname():
    cfg = StubConfig()
    cfg.broker_id = None
    b = StubBroker(cfg)
    assert b.broker_id == "stubbroker"   # __class__.__name__.lower()

def test_default_event_bus_is_local():
    b = _broker()
    assert isinstance(b.events, LocalEventBus)

def test_custom_event_bus_injected():
    bus = LocalEventBus()
    b = _broker(event_bus=bus)
    assert b.events is bus

def test_custom_event_bus_satisfies_protocol():
    bus = LocalEventBus()
    assert isinstance(bus, IEventBus)


# ── _emit ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_emit_fires_subscribed_handler():
    b = _broker()
    received: list[BrokerEventPayload] = []
    b.events.subscribe(BrokerEvent.CONNECTED, lambda p: received.append(p))

    await b._emit(BrokerEvent.CONNECTED)

    assert len(received) == 1
    assert received[0].event == BrokerEvent.CONNECTED
    assert received[0].broker_id == "stub_demo"


@pytest.mark.asyncio
async def test_emit_carries_data():
    b = _broker()
    received = []
    b.events.subscribe(BrokerEvent.ORDER_FILLED, lambda p: received.append(p))

    await b._emit(BrokerEvent.ORDER_FILLED, data={"order_id": "X1"})

    assert received[0].data == {"order_id": "X1"}


@pytest.mark.asyncio
async def test_emit_carries_error():
    b = _broker()
    received = []
    b.events.subscribe(BrokerEvent.CONNECTION_LOST, lambda p: received.append(p))

    await b._emit(BrokerEvent.CONNECTION_LOST, error="timeout")

    assert received[0].error == "timeout"


@pytest.mark.asyncio
async def test_emit_timestamp_is_set():
    b = _broker()
    received = []
    b.events.subscribe(BrokerEvent.CONNECTED, lambda p: received.append(p))
    await b._emit(BrokerEvent.CONNECTED)
    assert received[0].timestamp is not None


# ── reconnect ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconnect_succeeds_first_attempt():
    b = _broker()
    reconnecting_events = []
    b.events.subscribe(BrokerEvent.RECONNECTING, lambda p: reconnecting_events.append(p))

    result = await b.reconnect(max_attempts=3, delay=0)

    assert result is True
    assert len(reconnecting_events) == 1


@pytest.mark.asyncio
async def test_reconnect_retries_on_failure():
    b = _broker()
    attempts = []

    async def failing_connect():
        attempts.append(1)
        return False

    reconnecting_events = []
    b.events.subscribe(BrokerEvent.RECONNECTING, lambda p: reconnecting_events.append(p))

    with patch.object(b, "connect", side_effect=failing_connect):
        result = await b.reconnect(max_attempts=3, delay=0)

    assert result is False
    assert len(attempts) == 3
    assert len(reconnecting_events) == 3


@pytest.mark.asyncio
async def test_reconnect_succeeds_on_second_attempt():
    b = _broker()
    call_count = 0

    async def connect_on_second():
        nonlocal call_count
        call_count += 1
        return call_count >= 2

    with patch.object(b, "connect", side_effect=connect_on_second):
        result = await b.reconnect(max_attempts=3, delay=0)

    assert result is True
    assert call_count == 2


@pytest.mark.asyncio
async def test_reconnect_emits_reconnecting_each_attempt():
    b = _broker()
    events = []
    b.events.subscribe(BrokerEvent.RECONNECTING, lambda p: events.append(p.data))

    async def always_fail(): return False
    with patch.object(b, "connect", side_effect=always_fail):
        await b.reconnect(max_attempts=3, delay=0)

    assert [e["attempt"] for e in events] == [1, 2, 3]


# ── check_risk_limits ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_events_when_within_limits():
    b = _broker()
    fired = []
    b.events.subscribe(BrokerEvent.EQUITY_FLOOR_HIT, lambda p: fired.append("floor"))
    b.events.subscribe(BrokerEvent.DAILY_LOSS_LIMIT,  lambda p: fired.append("loss"))

    # current_value = 100_000: own_equity = 50_000 > floor(55k)... wait
    # own_equity = 100_000 - 50_000(loan) = 50_000 which is < 55_000 floor
    # Let's use 120_000: own = 70_000 > 55_000, drawdown = 122_562 - 120_000 = 2562 < 20_000
    await b.check_risk_limits(_account(current_value=120_000))

    assert fired == []


@pytest.mark.asyncio
async def test_equity_floor_hit():
    b = _broker()
    fired = []
    b.events.subscribe(BrokerEvent.EQUITY_FLOOR_HIT, lambda p: fired.append(p.data))

    # own_equity = 90_000 - 50_000 = 40_000 < 55_000 floor
    await b.check_risk_limits(_account(current_value=90_000))

    assert len(fired) == 1
    assert fired[0]["own_equity"] == pytest.approx(40_000)


@pytest.mark.asyncio
async def test_daily_loss_limit_hit():
    b = _broker()
    fired = []
    b.events.subscribe(BrokerEvent.DAILY_LOSS_LIMIT, lambda p: fired.append(p.data))

    # drawdown = 122_562 - 100_000 = 22_562 >= 20_000 hard max
    # own_equity = 100_000 - 50_000 = 50_000 < 55_000 — also trips floor
    # Use a value that only trips loss limit: current=60_000
    # drawdown = 122_562 - 60_000 = 62_562 >= 20_000 ✓
    # own_equity = 60_000 - 50_000 = 10_000 < 55_000 — also trips floor
    # Just verify loss limit fires regardless
    await b.check_risk_limits(_account(current_value=100_000))

    assert len(fired) == 1
    assert fired[0]["drawdown"] == pytest.approx(22_562)


@pytest.mark.asyncio
async def test_both_limits_can_fire_together():
    b = _broker()
    fired = []
    b.events.subscribe(BrokerEvent.EQUITY_FLOOR_HIT, lambda p: fired.append("floor"))
    b.events.subscribe(BrokerEvent.DAILY_LOSS_LIMIT,  lambda p: fired.append("loss"))

    # own_equity = 60_000 - 50_000 = 10_000 < 55_000 (floor)
    # drawdown   = 122_562 - 60_000 = 62_562 >= 20_000 (loss)
    await b.check_risk_limits(_account(current_value=60_000))

    assert "floor" in fired
    assert "loss"  in fired


@pytest.mark.asyncio
async def test_risk_limits_respect_custom_config():
    cfg = StubConfig(
        loan_amount=0,
        equity_floor=0,
        hard_max_loss=999_999,
        starting_equity=100_000,
    )
    b = StubBroker(cfg)
    fired = []
    b.events.subscribe(BrokerEvent.EQUITY_FLOOR_HIT, lambda p: fired.append("floor"))
    b.events.subscribe(BrokerEvent.DAILY_LOSS_LIMIT,  lambda p: fired.append("loss"))

    await b.check_risk_limits(_account(current_value=50_000))

    assert fired == []   # drawdown=50k < 999k limit, equity=50k > 0 floor


# ── background tasks ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_background_task_tracked():
    b = _broker()
    event = asyncio.Event()

    async def long_running():
        await event.wait()

    task = b._start_background_task(long_running())
    assert task in b._background_tasks

    event.set()
    await asyncio.sleep(0)   # let task finish and remove itself


@pytest.mark.asyncio
async def test_cancel_background_tasks():
    b = _broker()
    cancelled = []

    async def long_running():
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    b._start_background_task(long_running())
    assert len(b._background_tasks) == 1

    await asyncio.sleep(0)   # yield so task starts and reaches its first await
    await b._cancel_background_tasks()

    assert b._background_tasks == []
    assert cancelled == [True]


@pytest.mark.asyncio
async def test_cancel_background_tasks_clears_all():
    b = _broker()

    async def noop():
        await asyncio.sleep(999)

    b._start_background_task(noop())
    b._start_background_task(noop())
    b._start_background_task(noop())

    await b._cancel_background_tasks()

    assert b._background_tasks == []


# ── __repr__ ──────────────────────────────────────────────────────────────────

def test_repr_contains_broker_id():
    b = _broker()
    assert "stub_demo" in repr(b)


def test_repr_contains_listener_count():
    b = _broker()
    b.events.subscribe(BrokerEvent.CONNECTED, lambda p: None)
    assert "listeners=" in repr(b)

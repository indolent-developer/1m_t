"""
Tests for KeyLevelMonitorService's two-stage bounce/reversal confirmation,
specifically the "reversed LOD" (BREAK_ABOVE at LOD) path.

All external I/O (event bus, indicator provider) is mocked.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

import pytest
from adapters.brokers.entities.broker_event import BrokerEvent
from adapters.brokers.entities.broker_event_payload import BrokerEventPayload
from core.entities.level_event import LevelEvent, PriceLevelEvent
from core.entities.market_data import PriceTick
from services.key_level_monitor_service import KeyLevelMonitorService, _PendingBounce


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _monitor(symbols=("APLD",)) -> KeyLevelMonitorService:
    bus = AsyncMock()
    bus.emit = AsyncMock()

    svc = KeyLevelMonitorService.__new__(KeyLevelMonitorService)
    svc._bus              = bus
    svc._symbols          = list(symbols)
    svc._provider         = MagicMock()
    svc._provider.compute.return_value = 0.25   # ATR
    svc._provider_1m      = MagicMock()
    svc._provider_1m.load_one     = AsyncMock()
    svc._provider_1m.supertrend_direction = MagicMock(return_value=-1)
    svc._tracker_cfg      = dict(atr_period=14, min_atr_pct=0.005, band_mult=0.3)
    svc._static_trackers  = {}
    svc._hod_lod_trackers = {}
    svc._key_levels       = {}
    svc._label_map        = {s: {} for s in symbols}
    svc._level_touch_times = {}
    svc._pending_bounces  = {}
    return svc


def _break_above_lod_event(symbol="APLD", level=27.87, price=28.00) -> PriceLevelEvent:
    return PriceLevelEvent(
        event=LevelEvent.BREAK_ABOVE,
        symbol=symbol,
        level=level,
        price=price,
        zone_lo=level - 0.075,
        zone_hi=level + 0.075,
        atr=0.25,
        convincing=True,
        tick_source="scalable",
        label="LOD",
    )


# ── Reversed LOD is parked, not emitted one-shot ───────────────────────────────

@pytest.mark.asyncio
async def test_break_above_lod_is_parked_not_emitted_immediately():
    """A BREAK_ABOVE at LOD must go through the same park/confirm gate as
    BOUNCE-at-LOD — it must not be emitted (or dropped) based on a single,
    one-shot supertrend read."""
    svc = _monitor()
    svc._provider_1m.supertrend_direction.return_value = -1   # bearish at the moment of the break
    evt = _break_above_lod_event()
    await svc._park_bounce("APLD", evt)
    assert "APLD" in svc._pending_bounces
    svc._bus.emit.assert_not_called()


@pytest.mark.asyncio
async def test_break_above_lod_confirms_once_supertrend_flips_bullish():
    svc = _monitor()
    svc._provider_1m.supertrend_direction.return_value = -1
    evt = _break_above_lod_event(price=28.00)
    await svc._park_bounce("APLD", evt)   # st_at_bounce = -1

    svc._provider_1m.supertrend_direction.return_value = 1   # flips bullish later
    confirmed = await svc._check_pending_bounce("APLD", 28.10, _now())

    assert confirmed is evt
    assert "APLD" not in svc._pending_bounces


@pytest.mark.asyncio
async def test_break_above_lod_stays_pending_while_supertrend_still_bearish():
    svc = _monitor()
    svc._provider_1m.supertrend_direction.return_value = -1
    evt = _break_above_lod_event(price=28.00)
    await svc._park_bounce("APLD", evt)

    # still bearish — must not confirm yet
    confirmed = await svc._check_pending_bounce("APLD", 28.05, _now())
    assert confirmed is None
    assert "APLD" in svc._pending_bounces


@pytest.mark.asyncio
async def test_break_above_lod_cancelled_on_timeout():
    svc = _monitor()
    svc._provider_1m.supertrend_direction.return_value = -1
    evt = _break_above_lod_event(price=28.00)
    await svc._park_bounce("APLD", evt)

    stale_now = _now() + dt.timedelta(seconds=301)   # past _BOUNCE_TIMEOUT_SECS
    confirmed = await svc._check_pending_bounce("APLD", 28.05, stale_now)
    assert confirmed is None
    assert "APLD" not in svc._pending_bounces


# ── _on_tick routes BREAK_ABOVE/LOD through the park path ─────────────────────

@pytest.mark.asyncio
async def test_on_tick_parks_break_above_lod_instead_of_emitting():
    svc = _monitor()
    evt = _break_above_lod_event(price=28.00)
    tracker = MagicMock()
    tracker.level = 27.87
    tracker.update.return_value = [evt]
    svc._hod_lod_trackers["APLD"] = [tracker]
    svc._label_map["APLD"] = {round(27.87, 4): "LOD"}

    tick = PriceTick(symbol="APLD", price=28.00, source="scalable")
    payload = BrokerEventPayload(event=BrokerEvent.QUOTE_UPDATE, broker_id="test", data=tick)
    await svc._on_tick(payload)

    assert "APLD" in svc._pending_bounces
    svc._bus.emit.assert_not_called()

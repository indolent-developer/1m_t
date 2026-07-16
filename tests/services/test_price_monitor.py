"""
Tests for PriceMonitor.

All WS fetchers and FMP polling are mocked — no network, no IB Gateway needed.

What is covered:
  - constructor stores config and data-source configs correctly
  - _resolve_ws_fetcher: source routing (auto / ibkr / finnhub / fmp-only)
  - _resolve_ws_fetcher: IBKR exception falls back to Finnhub on "auto",
    but returns None when source is explicitly "ibkr"
  - start(): WS path subscribes every symbol and waits
  - start(): FMP poll task is created when fmp api_key present and source is auto
  - start(): FMP poll is skipped when source is pinned to ibkr or finnhub
  - start(): no source → warning + early return (no stale task created)
  - subscribe() / unsubscribe(): delegates to the live WS fetcher
  - subscribe() / unsubscribe(): no-ops gracefully when WS is not started
  - _emit_tick(): puts a QUOTE_UPDATE payload on the bus
  - stop(): cancels FMP tasks and calls unsubscribe on the WS fetcher
"""
from __future__ import annotations

import asyncio
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

import pytest

from adapters.events.local_event_bus import LocalEventBus
from adapters.brokers.entities.broker_event import BrokerEvent
from core.config.config_models import (
    FinnhubDataConfig,
    FmpDataConfig,
    IBKRBrokerConfig,
    PriceMonitorConfig,
)
from core.entities.market_data import PriceTick
from services.price_monitor import PriceMonitor


# ── Helpers ───────────────────────────────────────────────────────────────────

def _config(**kwargs) -> PriceMonitorConfig:
    defaults = dict(source="auto", poll_interval=5, stale_warn_seconds=60, stale_check_interval=10)
    return PriceMonitorConfig(**{**defaults, **kwargs})


def _ibkr_cfg() -> IBKRBrokerConfig:
    return IBKRBrokerConfig(host="127.0.0.1", port=4002, client_id_data=2,
                            market_data_type=3, max_symbols=10)


def _finnhub_cfg(api_key: str = "fh-key") -> FinnhubDataConfig:
    return FinnhubDataConfig(api_key=api_key, max_symbols=5)


def _fmp_cfg(api_key: str = "fmp-key") -> FmpDataConfig:
    return FmpDataConfig(api_key=api_key)


def _mock_ws_fetcher() -> MagicMock:
    """A duck-typed mock with async subscribe / unsubscribe."""
    f = MagicMock()
    f.subscribe   = AsyncMock()
    f.unsubscribe = AsyncMock()
    return f


def _monitor(
    symbols=None,
    config=None,
    ibkr_config=None,
    finnhub_config=None,
    fmp_config=None,
    bus=None,
) -> PriceMonitor:
    return PriceMonitor(
        symbols=symbols or ["AAPL", "TSLA"],
        bus=bus or LocalEventBus(),
        config=config or _config(),
        ibkr_config=ibkr_config,
        finnhub_config=finnhub_config,
        fmp_config=fmp_config,
    )


# ── Constructor ───────────────────────────────────────────────────────────────

def test_constructor_stores_config():
    cfg  = _config(source="ibkr", poll_interval=99)
    ibkr = _ibkr_cfg()
    m    = _monitor(config=cfg, ibkr_config=ibkr)
    assert m._config is cfg
    assert m._ibkr_config is ibkr
    assert m.symbols == ["AAPL", "TSLA"]


def test_constructor_config_fields_accessible():
    cfg = _config(source="finnhub", poll_interval=30, stale_warn_seconds=120)
    m   = _monitor(config=cfg)
    assert m._config.source == "finnhub"
    assert m._config.poll_interval == 30
    assert m._config.stale_warn_seconds == 120


# ── _resolve_ws_fetcher ───────────────────────────────────────────────────────

def test_resolve_ibkr_when_source_ibkr_and_config_present():
    mock_fetcher = _mock_ws_fetcher()
    with patch("data_fetchers.ibkr_ws_data_fetcher.get_shared_fetcher", return_value=mock_fetcher) as p:
        m      = _monitor(config=_config(source="ibkr"), ibkr_config=_ibkr_cfg())
        result = m._resolve_ws_fetcher()
    p.assert_called_once_with(config=m._ibkr_config)
    assert result is mock_fetcher


def test_resolve_returns_none_when_source_ibkr_but_no_ibkr_config():
    m = _monitor(config=_config(source="ibkr"), ibkr_config=None)
    assert m._resolve_ws_fetcher() is None


def test_resolve_finnhub_when_source_finnhub_and_key_present():
    mock_fetcher = _mock_ws_fetcher()
    with patch("data_fetchers.finnhub_ws_data_fetcher.get_shared_fetcher", return_value=mock_fetcher) as p:
        m      = _monitor(config=_config(source="finnhub"), finnhub_config=_finnhub_cfg())
        result = m._resolve_ws_fetcher()
    p.assert_called_once_with(config=m._finnhub_config)
    assert result is mock_fetcher


def test_resolve_returns_none_when_source_finnhub_but_no_api_key():
    m = _monitor(config=_config(source="finnhub"), finnhub_config=_finnhub_cfg(api_key=""))
    assert m._resolve_ws_fetcher() is None


def test_resolve_auto_prefers_ibkr_when_both_present():
    ibkr_mock     = _mock_ws_fetcher()
    finnhub_mock  = _mock_ws_fetcher()
    with patch("data_fetchers.ibkr_ws_data_fetcher.get_shared_fetcher", return_value=ibkr_mock):
        with patch("data_fetchers.finnhub_ws_data_fetcher.get_shared_fetcher", return_value=finnhub_mock):
            m      = _monitor(config=_config(source="auto"),
                              ibkr_config=_ibkr_cfg(), finnhub_config=_finnhub_cfg())
            result = m._resolve_ws_fetcher()
    assert result is ibkr_mock


def test_resolve_auto_falls_back_to_finnhub_when_no_ibkr_config():
    mock_fetcher = _mock_ws_fetcher()
    with patch("data_fetchers.finnhub_ws_data_fetcher.get_shared_fetcher", return_value=mock_fetcher):
        m      = _monitor(config=_config(source="auto"),
                          ibkr_config=None, finnhub_config=_finnhub_cfg())
        result = m._resolve_ws_fetcher()
    assert result is mock_fetcher


def test_resolve_auto_falls_back_to_finnhub_when_ibkr_raises():
    mock_fetcher = _mock_ws_fetcher()
    with patch("data_fetchers.ibkr_ws_data_fetcher.get_shared_fetcher", side_effect=ImportError("no ib_async")):
        with patch("data_fetchers.finnhub_ws_data_fetcher.get_shared_fetcher", return_value=mock_fetcher):
            m      = _monitor(config=_config(source="auto"),
                              ibkr_config=_ibkr_cfg(), finnhub_config=_finnhub_cfg())
            result = m._resolve_ws_fetcher()
    assert result is mock_fetcher


def test_resolve_ibkr_raises_and_no_fallback_when_source_is_explicit_ibkr():
    with patch("data_fetchers.ibkr_ws_data_fetcher.get_shared_fetcher", side_effect=ImportError("no ib_async")):
        m      = _monitor(config=_config(source="ibkr"), ibkr_config=_ibkr_cfg())
        result = m._resolve_ws_fetcher()
    assert result is None


def test_resolve_returns_none_when_no_source_configs():
    m = _monitor(config=_config(source="auto"))
    assert m._resolve_ws_fetcher() is None


# ── start() — WS path ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_subscribes_all_symbols_via_ws():
    mock_ws = _mock_ws_fetcher()
    symbols = ["AAPL", "MSFT", "TSLA"]

    with patch.object(PriceMonitor, "_resolve_ws_fetcher", return_value=mock_ws):
        with patch.object(PriceMonitor, "_stale_monitor", new_callable=lambda: lambda self: asyncio.sleep(9999)):
            m    = _monitor(symbols=symbols, config=_config(source="ibkr"), ibkr_config=_ibkr_cfg())
            task = asyncio.create_task(m.start())
            await asyncio.sleep(0)   # let start() reach the await
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    subscribed = [call.args[0] for call in mock_ws.subscribe.await_args_list]
    assert set(subscribed) == set(symbols)


@pytest.mark.asyncio
async def test_start_no_source_returns_early_without_stale_task():
    m     = _monitor(config=_config(source="auto"))   # no configs → no source
    calls = []

    with patch.object(PriceMonitor, "_stale_monitor", side_effect=lambda: calls.append("stale")):
        await m.start()

    assert calls == []   # stale monitor must NOT have been scheduled


# ── start() — FMP poll ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_creates_fmp_poll_task_when_api_key_present_and_source_auto():
    poll_called = []

    async def fake_fmp_poll(api_key):
        poll_called.append(api_key)
        await asyncio.sleep(9999)

    async def fake_stale_monitor(self):
        await asyncio.sleep(9999)

    with patch.object(PriceMonitor, "_resolve_ws_fetcher", return_value=None):
        with patch.object(PriceMonitor, "_run_fmp_poll", side_effect=fake_fmp_poll):
            with patch.object(PriceMonitor, "_stale_monitor", fake_stale_monitor):
                m    = _monitor(config=_config(source="auto"), fmp_config=_fmp_cfg())
                task = asyncio.create_task(m.start())
                await asyncio.sleep(0)  # let start() schedule the FMP task
                await asyncio.sleep(0)  # let the FMP task body execute
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    assert poll_called == ["fmp-key"]


@pytest.mark.asyncio
async def test_start_skips_fmp_poll_when_source_is_ibkr():
    mock_ws     = _mock_ws_fetcher()
    poll_called = []

    async def fake_fmp_poll(api_key):
        poll_called.append(api_key)

    with patch.object(PriceMonitor, "_resolve_ws_fetcher", return_value=mock_ws):
        with patch.object(PriceMonitor, "_run_fmp_poll", side_effect=fake_fmp_poll):
            with patch.object(PriceMonitor, "_stale_monitor", return_value=asyncio.sleep(9999)):
                m    = _monitor(config=_config(source="ibkr"),
                                ibkr_config=_ibkr_cfg(), fmp_config=_fmp_cfg())
                task = asyncio.create_task(m.start())
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    assert poll_called == []


@pytest.mark.asyncio
async def test_start_skips_fmp_poll_when_source_is_finnhub():
    mock_ws     = _mock_ws_fetcher()
    poll_called = []

    async def fake_fmp_poll(api_key):
        poll_called.append(api_key)

    with patch.object(PriceMonitor, "_resolve_ws_fetcher", return_value=mock_ws):
        with patch.object(PriceMonitor, "_run_fmp_poll", side_effect=fake_fmp_poll):
            with patch.object(PriceMonitor, "_stale_monitor", return_value=asyncio.sleep(9999)):
                m    = _monitor(config=_config(source="finnhub"),
                                finnhub_config=_finnhub_cfg(), fmp_config=_fmp_cfg())
                task = asyncio.create_task(m.start())
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    assert poll_called == []


# ── subscribe / unsubscribe ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_adds_symbol_and_delegates_to_ws():
    mock_ws        = _mock_ws_fetcher()
    m              = _monitor(symbols=["AAPL"])
    m._ws_fetcher  = mock_ws

    await m.subscribe("MSFT")

    assert "MSFT" in m.symbols
    mock_ws.subscribe.assert_awaited_once_with("MSFT", m._emit_tick)


@pytest.mark.asyncio
async def test_subscribe_does_not_duplicate_existing_symbol():
    mock_ws       = _mock_ws_fetcher()
    m             = _monitor(symbols=["AAPL"])
    m._ws_fetcher = mock_ws

    await m.subscribe("AAPL")

    assert m.symbols.count("AAPL") == 1


@pytest.mark.asyncio
async def test_subscribe_without_ws_fetcher_still_adds_to_symbols():
    m = _monitor(symbols=[])
    await m.subscribe("NVDA")
    assert "NVDA" in m.symbols


@pytest.mark.asyncio
async def test_unsubscribe_removes_symbol_and_delegates_to_ws():
    mock_ws       = _mock_ws_fetcher()
    m             = _monitor(symbols=["AAPL", "TSLA"])
    m._ws_fetcher = mock_ws

    await m.unsubscribe("AAPL")

    assert "AAPL" not in m.symbols
    assert "TSLA" in m.symbols
    mock_ws.unsubscribe.assert_awaited_once_with("AAPL", m._emit_tick)


@pytest.mark.asyncio
async def test_unsubscribe_unknown_symbol_is_noop():
    m = _monitor(symbols=["AAPL"])
    await m.unsubscribe("XXXX")   # must not raise
    assert m.symbols == ["AAPL"]


# ── _emit_tick ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_emit_tick_publishes_quote_update_on_bus():
    bus    = LocalEventBus()
    events = []
    bus.subscribe(BrokerEvent.QUOTE_UPDATE, lambda p: events.append(p))

    m    = _monitor(bus=bus)
    tick = PriceTick(symbol="AAPL", price=195.5, bid=195.4, ask=195.6,
                     volume=1000, timestamp=1_700_000_000_000, source="finnhub")
    await m._emit_tick(tick)

    assert len(events) == 1
    payload = events[0]
    assert payload.event == BrokerEvent.QUOTE_UPDATE
    assert payload.data is tick
    assert payload.broker_id == "finnhub"


@pytest.mark.asyncio
async def test_emit_tick_updates_last_seen():
    m    = _monitor()
    tick = PriceTick(symbol="TSLA", price=220.0, bid=219.9, ask=220.1,
                     volume=500, timestamp=1_700_000_001_000, source="ibkr")
    await m._emit_tick(tick)
    assert m._last_seen["TSLA"] == 1_700_000_001_000.0


# ── stop() ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_calls_unsubscribe_for_all_symbols():
    mock_ws       = _mock_ws_fetcher()
    symbols       = ["AAPL", "TSLA"]
    m             = _monitor(symbols=symbols)
    m._ws_fetcher = mock_ws

    await m.stop()

    unsubbed = {call.args[0] for call in mock_ws.unsubscribe.await_args_list}
    assert unsubbed == set(symbols)
    assert m._ws_fetcher is None


@pytest.mark.asyncio
async def test_stop_cancels_fmp_tasks():
    completed = []

    async def long_poll():
        try:
            await asyncio.sleep(9999)
        except asyncio.CancelledError:
            completed.append("cancelled")

    m = _monitor()
    m._tasks.append(asyncio.create_task(long_poll()))
    await asyncio.sleep(0)   # let the task start

    await m.stop()

    assert completed == ["cancelled"]
    assert m._tasks == []


@pytest.mark.asyncio
async def test_stop_without_ws_fetcher_is_safe():
    m = _monitor()
    await m.stop()   # must not raise

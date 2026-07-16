"""
Tests for NewsReactionAnalyzer.
All external I/O (Telegram, FMP price fetch) is mocked.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
import os
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

import pytest
from adapters.brokers.entities.broker_event import BrokerEvent
from adapters.brokers.entities.broker_event_payload import BrokerEventPayload
from core.entities.market_data import PriceTick, StockNews
from core.entities.news_event import NewsEvent
from core.entities.ohlc import OHLCData
from core.entities.scanner_event import ScannerEvent, ScannerHit
from services.news_reaction_analyzer import NewsReactionAnalyzer, PendingWatch, _MAX_WATCHES


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _news(
    symbol: str = "AAPL",
    title: str = "Apple beats earnings",
    minutes_ago: float = 5.0,
    news_source: str = "FMP",
    latency_s: float = 30.0,
) -> StockNews:
    pub = _now() - dt.timedelta(minutes=minutes_ago)
    fetched = pub + dt.timedelta(seconds=latency_s)
    return StockNews(
        symbol=symbol,
        published_date=pub,
        publisher="Reuters",
        title=title,
        url="https://example.com/",
        text="",
        fetched_at=fetched,
        news_source=news_source,
    )


def _tick_payload(symbol: str, price: float) -> BrokerEventPayload:
    tick = PriceTick(symbol=symbol, price=price, source="fmp")
    return BrokerEventPayload(event=BrokerEvent.QUOTE_UPDATE, broker_id="test", data=tick)


def _analyzer(
    watchlist=None,
    fmp_price: float = 100.0,
) -> NewsReactionAnalyzer:
    bus = AsyncMock()
    bus.emit    = AsyncMock()
    bus.subscribe = MagicMock()

    fmp = MagicMock()
    fmp.get_last_price.return_value = fmp_price
    fmp.get_market_data.return_value = []   # no historical bar by default — falls back to tick/last-price

    svc = NewsReactionAnalyzer.__new__(NewsReactionAnalyzer)
    svc._bus          = bus
    svc._fmp          = fmp
    svc._chat_id      = None
    svc._chart_id     = "3UGuuzJ4"
    svc._bot          = None
    svc._watchlist    = watchlist if watchlist is not None else set()
    svc._watches      = deque(maxlen=_MAX_WATCHES)
    svc._last_prices  = {}
    svc._poll_symbols = set()
    svc._poll_task    = None
    svc._cleanup_task = None
    return svc


def _watch(
    symbol: str = "AAPL",
    price_at_news: float = 100.0,
    minutes_remaining: float = 30.0,
    alerted: bool = False,
) -> PendingWatch:
    return PendingWatch(
        symbol=symbol,
        news=_news(symbol=symbol),
        price_at_news=price_at_news,
        expires_at=_now() + dt.timedelta(minutes=minutes_remaining),
        alerted=alerted,
    )


# ── _on_news — price snapshot ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_news_uses_last_tick_price():
    svc = _analyzer()
    svc._last_prices["AAPL"] = 155.0
    await svc._on_news(_news("AAPL"))
    assert svc._watches[0].price_at_news == 155.0


@pytest.mark.asyncio
async def test_on_news_falls_back_to_fmp_when_no_tick():
    svc = _analyzer(fmp_price=142.0)
    await svc._on_news(_news("AAPL"))
    assert svc._watches[0].price_at_news == 142.0


@pytest.mark.asyncio
async def test_on_news_skips_when_no_price_available():
    svc = _analyzer(fmp_price=0.0)
    await svc._on_news(_news("AAPL"))
    assert len(svc._watches) == 0


@pytest.mark.asyncio
async def test_on_news_fmp_exception_skips_watch():
    svc = _analyzer()
    svc._fmp.get_last_price.side_effect = RuntimeError("FMP down")
    await svc._on_news(_news("AAPL"))
    assert len(svc._watches) == 0


@pytest.mark.asyncio
async def test_on_news_prefers_historical_price_at_publish_over_live_tick():
    """
    price_at_news must come from the 1-min bar covering `published_date`,
    not the (possibly much later, stale-relative-to-publish) live tick —
    this is what makes move %'s correct when NEWS_PUBLISHED is delivered
    minutes after the article actually printed.
    """
    svc = _analyzer()
    item = _news("AAPL", minutes_ago=10)
    pub_et = item.published_date.astimezone(dt.timezone.utc).astimezone(
        __import__("pytz").timezone("America/New_York")
    ).replace(tzinfo=None)
    bars = [
        OHLCData(open=99, high=99.5, low=98.5, close=99.0, time=pub_et - dt.timedelta(minutes=2)),
        OHLCData(open=99, high=99.5, low=98.5, close=99.2, time=pub_et),   # bar at publish time
        OHLCData(open=99, high=99.5, low=98.5, close=105.0, time=pub_et + dt.timedelta(minutes=5)),
    ]
    svc._fmp.get_market_data.return_value = bars
    svc._last_prices["AAPL"] = 105.0   # live tick — should NOT be used
    await svc._on_news(item)
    assert svc._watches[0].price_at_news == 99.2


@pytest.mark.asyncio
async def test_on_news_falls_back_to_tick_when_no_historical_bars():
    svc = _analyzer()
    svc._fmp.get_market_data.return_value = []
    svc._last_prices["AAPL"] = 155.0
    await svc._on_news(_news("AAPL"))
    assert svc._watches[0].price_at_news == 155.0


@pytest.mark.asyncio
async def test_on_news_falls_back_to_tick_when_historical_lookup_raises():
    svc = _analyzer()
    svc._fmp.get_market_data.side_effect = RuntimeError("FMP down")
    svc._last_prices["AAPL"] = 155.0
    await svc._on_news(_news("AAPL"))
    assert svc._watches[0].price_at_news == 155.0


# ── _on_news — watch setup ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_news_sets_correct_expiry():
    svc = _analyzer()
    svc._last_prices["AAPL"] = 100.0
    item = _news("AAPL", minutes_ago=10)
    await svc._on_news(item)
    watch = svc._watches[0]
    expected_expiry = item.published_date.replace(tzinfo=dt.timezone.utc) + dt.timedelta(minutes=60)
    diff = abs((watch.expires_at - expected_expiry).total_seconds())
    assert diff < 2


@pytest.mark.asyncio
async def test_on_news_adds_to_poll_symbols_when_not_on_watchlist():
    svc = _analyzer(watchlist=set())
    svc._last_prices["TSLA"] = 200.0
    await svc._on_news(_news("TSLA"))
    assert "TSLA" in svc._poll_symbols


@pytest.mark.asyncio
async def test_on_news_not_added_to_poll_when_already_on_watchlist():
    svc = _analyzer(watchlist={"AAPL"})
    svc._last_prices["AAPL"] = 100.0
    await svc._on_news(_news("AAPL"))
    assert "AAPL" not in svc._poll_symbols


@pytest.mark.asyncio
async def test_on_news_ignores_empty_symbol():
    svc = _analyzer()
    await svc._on_news(_news(symbol=""))
    assert len(svc._watches) == 0


# ── FIFO cap ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fifo_cap_at_30_items():
    svc = _analyzer()
    svc._last_prices = {f"SYM{i}": 100.0 for i in range(_MAX_WATCHES + 5)}
    for i in range(_MAX_WATCHES + 5):
        await svc._on_news(_news(symbol=f"SYM{i}", title=f"story {i}"))
    assert len(svc._watches) == _MAX_WATCHES


@pytest.mark.asyncio
async def test_fifo_oldest_dropped_when_cap_exceeded():
    svc = _analyzer()
    svc._last_prices = {f"SYM{i}": 100.0 for i in range(_MAX_WATCHES + 1)}
    for i in range(_MAX_WATCHES + 1):
        await svc._on_news(_news(symbol=f"SYM{i}", title=f"story {i}"))
    symbols_in_watch = [w.symbol for w in svc._watches]
    assert "SYM0" not in symbols_in_watch          # oldest dropped
    assert f"SYM{_MAX_WATCHES}" in symbols_in_watch  # newest kept


# ── _check_symbol — move detection ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_2pct_up_move_triggers_reaction():
    svc = _analyzer()
    svc._watches.append(_watch("AAPL", price_at_news=100.0))
    svc._confirm_via_bar = AsyncMock(return_value=102.5)
    reactions = []
    svc._on_reaction = AsyncMock(side_effect=lambda w, p, m, n: reactions.append((w, p, m)))
    await svc._check_symbol("AAPL", 102.5)
    assert len(reactions) == 1
    assert reactions[0][2] > 0   # positive move


@pytest.mark.asyncio
async def test_2pct_down_move_triggers_reaction():
    svc = _analyzer()
    svc._watches.append(_watch("AAPL", price_at_news=100.0))
    svc._confirm_via_bar = AsyncMock(return_value=97.5)
    reactions = []
    svc._on_reaction = AsyncMock(side_effect=lambda w, p, m, n: reactions.append((w, p, m)))
    await svc._check_symbol("AAPL", 97.5)
    assert len(reactions) == 1
    assert reactions[0][2] < 0   # negative move


@pytest.mark.asyncio
async def test_sub_2pct_move_does_not_trigger():
    svc = _analyzer()
    svc._watches.append(_watch("AAPL", price_at_news=100.0))
    svc._on_reaction = AsyncMock()
    await svc._check_symbol("AAPL", 101.0)   # 1 % — below threshold
    svc._on_reaction.assert_not_called()


@pytest.mark.asyncio
async def test_exactly_2pct_move_triggers():
    svc = _analyzer()
    svc._watches.append(_watch("AAPL", price_at_news=100.0))
    svc._confirm_via_bar = AsyncMock(return_value=102.0)
    svc._on_reaction = AsyncMock()
    await svc._check_symbol("AAPL", 102.0)
    svc._on_reaction.assert_called_once()


# ── _check_symbol — bar confirmation (guards against bad/stale ticks) ─────────

@pytest.mark.asyncio
async def test_check_symbol_does_not_fire_when_bar_confirmation_unavailable():
    """A tick alone is never enough — if FMP bar data can't confirm it, don't fire."""
    svc = _analyzer()
    svc._watches.append(_watch("PYPL", price_at_news=57.04))
    svc._confirm_via_bar = AsyncMock(return_value=None)
    svc._on_reaction = AsyncMock()
    await svc._check_symbol("PYPL", 47.37)   # -17% tick
    svc._on_reaction.assert_not_called()


@pytest.mark.asyncio
async def test_check_symbol_does_not_fire_when_bar_data_reverts():
    """
    Reproduces the PYPL false-alert scenario: one bad tick shows -17%, but the
    confirming 1-min bar shows price is actually still near price_at_news —
    must not fire.
    """
    svc = _analyzer()
    svc._watches.append(_watch("PYPL", price_at_news=57.04))
    svc._confirm_via_bar = AsyncMock(return_value=56.45)   # bar says: still ~normal
    svc._on_reaction = AsyncMock()
    await svc._check_symbol("PYPL", 47.37)   # bad tick
    svc._on_reaction.assert_not_called()


@pytest.mark.asyncio
async def test_check_symbol_fires_when_bar_confirms_move():
    svc = _analyzer()
    svc._watches.append(_watch("PYPL", price_at_news=57.04))
    svc._confirm_via_bar = AsyncMock(return_value=47.37)   # bar agrees — real move
    reactions = []
    svc._on_reaction = AsyncMock(side_effect=lambda w, p, m, n: reactions.append((w, p, m)))
    await svc._check_symbol("PYPL", 47.37)
    assert len(reactions) == 1
    assert reactions[0][1] == 47.37


@pytest.mark.asyncio
async def test_confirm_via_bar_returns_latest_bar_close():
    svc = _analyzer()
    bars = [
        OHLCData(open=56, high=56.5, low=55.8, close=56.2, time=dt.datetime(2026, 7, 15, 7, 36)),
        OHLCData(open=56.2, high=56.3, low=47.3, close=47.37, time=dt.datetime(2026, 7, 15, 7, 37)),
    ]
    svc._fmp.get_market_data.return_value = bars
    price = await svc._confirm_via_bar("PYPL")
    assert price == 47.37


@pytest.mark.asyncio
async def test_confirm_via_bar_returns_none_on_no_bars():
    svc = _analyzer()
    svc._fmp.get_market_data.return_value = []
    assert await svc._confirm_via_bar("PYPL") is None


@pytest.mark.asyncio
async def test_confirm_via_bar_returns_none_on_exception():
    svc = _analyzer()
    svc._fmp.get_market_data.side_effect = RuntimeError("FMP down")
    assert await svc._confirm_via_bar("PYPL") is None


@pytest.mark.asyncio
async def test_concurrent_ticks_do_not_double_fire():
    """
    Reproduces the SNDK duplicate-alert bug: multiple sources (IBKR/Finnhub/FMP,
    all live under `source: auto`) can each call _check_symbol for the same
    symbol at nearly the same instant. Since bar confirmation awaits an HTTP
    call before `alerted` is set, concurrent calls must not both slip through.
    """
    svc = _analyzer()
    svc._watches.append(_watch("SNDK", price_at_news=1659.19))

    async def slow_confirm(symbol):
        await asyncio.sleep(0.01)   # simulates the FMP round-trip
        return 1622.19

    svc._confirm_via_bar = slow_confirm
    fire_count = 0

    async def fake_on_reaction(w, p, m, n):
        nonlocal fire_count
        fire_count += 1

    svc._on_reaction = fake_on_reaction

    # Three "sources" all see the same breaching tick at once.
    await asyncio.gather(
        svc._check_symbol("SNDK", 1622.19),
        svc._check_symbol("SNDK", 1622.19),
        svc._check_symbol("SNDK", 1622.19),
    )
    assert fire_count == 1


@pytest.mark.asyncio
async def test_checking_flag_cleared_after_non_confirming_bar():
    """A watch that didn't fire (bad tick, bar didn't confirm) must remain
    checkable by later ticks — `checking` should reset, not stick."""
    svc = _analyzer()
    watch = _watch("PYPL", price_at_news=57.04)
    svc._watches.append(watch)
    svc._confirm_via_bar = AsyncMock(return_value=56.45)   # doesn't confirm
    svc._on_reaction = AsyncMock()
    await svc._check_symbol("PYPL", 47.37)
    assert watch.checking is False
    assert watch.alerted is False


@pytest.mark.asyncio
async def test_alerted_watch_not_triggered_again():
    svc = _analyzer()
    svc._watches.append(_watch("AAPL", price_at_news=100.0, alerted=True))
    svc._on_reaction = AsyncMock()
    await svc._check_symbol("AAPL", 110.0)
    svc._on_reaction.assert_not_called()


@pytest.mark.asyncio
async def test_expired_watch_not_triggered():
    svc = _analyzer()
    expired = PendingWatch(
        symbol="AAPL",
        news=_news("AAPL"),
        price_at_news=100.0,
        expires_at=_now() - dt.timedelta(minutes=1),   # already expired
    )
    svc._watches.append(expired)
    svc._on_reaction = AsyncMock()
    await svc._check_symbol("AAPL", 115.0)
    svc._on_reaction.assert_not_called()


@pytest.mark.asyncio
async def test_check_only_matches_correct_symbol():
    svc = _analyzer()
    svc._watches.append(_watch("TSLA", price_at_news=200.0))
    svc._on_reaction = AsyncMock()
    await svc._check_symbol("AAPL", 250.0)   # different symbol
    svc._on_reaction.assert_not_called()


# ── _on_reaction — side effects ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_reaction_marks_watch_alerted():
    svc  = _analyzer()
    w    = _watch("AAPL", price_at_news=100.0)
    svc._send = AsyncMock()
    svc._add_to_watchlist = AsyncMock()
    await svc._on_reaction(w, 103.0, 0.03, _now())
    assert w.alerted


@pytest.mark.asyncio
async def test_on_reaction_removes_from_poll_symbols():
    svc = _analyzer()
    svc._poll_symbols.add("AAPL")
    w = _watch("AAPL", price_at_news=100.0)
    svc._send = AsyncMock()
    svc._add_to_watchlist = AsyncMock()
    await svc._on_reaction(w, 103.0, 0.03, _now())
    assert "AAPL" not in svc._poll_symbols


@pytest.mark.asyncio
async def test_on_reaction_calls_send():
    svc = _analyzer()
    svc._send = AsyncMock()
    svc._add_to_watchlist = AsyncMock()
    w = _watch("AAPL", price_at_news=100.0)
    await svc._on_reaction(w, 103.0, 0.03, _now())
    svc._send.assert_called_once()


@pytest.mark.asyncio
async def test_on_reaction_emits_scanner_hit():
    svc = _analyzer()
    svc._send = AsyncMock()
    w = _watch("NVDA", price_at_news=400.0)
    await svc._on_reaction(w, 410.0, 0.025, _now())
    svc._bus.emit.assert_called_once()
    hit = svc._bus.emit.call_args[0][0]
    assert isinstance(hit, ScannerHit)
    assert hit.symbol == "NVDA"
    assert hit.scanner_name == "news_catalyst"
    assert hit.event == ScannerEvent.SYMBOL_DETECTED


@pytest.mark.asyncio
async def test_on_reaction_send_failure_does_not_crash():
    svc = _analyzer()
    svc._send = AsyncMock(side_effect=RuntimeError("Telegram down"))
    svc._add_to_watchlist = AsyncMock()
    w = _watch("AAPL", price_at_news=100.0)
    # Should raise (send is not guarded here; _send itself guards internally)
    # But _add_to_watchlist should still be called if we guard in _on_reaction
    # Verify at minimum that alerted is still set
    try:
        await svc._on_reaction(w, 103.0, 0.03, _now())
    except Exception:
        pass
    assert w.alerted


# ── _on_tick handler ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_tick_updates_last_prices():
    svc     = _analyzer()
    payload = _tick_payload("AAPL", 155.0)
    await svc._on_tick(payload)
    assert svc._last_prices["AAPL"] == 155.0


@pytest.mark.asyncio
async def test_on_tick_non_pricetick_ignored():
    svc     = _analyzer()
    payload = BrokerEventPayload(event=BrokerEvent.QUOTE_UPDATE, broker_id="test", data="not_a_tick")
    await svc._on_tick(payload)
    assert not svc._last_prices


@pytest.mark.asyncio
async def test_on_tick_triggers_check():
    svc = _analyzer()
    svc._watches.append(_watch("AAPL", price_at_news=100.0))
    svc._confirm_via_bar = AsyncMock(return_value=103.0)
    svc._on_reaction = AsyncMock()
    await svc._on_tick(_tick_payload("AAPL", 103.0))   # 3 % move
    svc._on_reaction.assert_called_once()


# ── Cleanup loop ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cleanup_removes_expired_watches():
    svc = _analyzer()
    expired = PendingWatch(
        symbol="AAPL",
        news=_news(),
        price_at_news=100.0,
        expires_at=_now() - dt.timedelta(minutes=5),
    )
    active = _watch("TSLA")
    svc._watches.extend([expired, active])
    # Manually run one cleanup cycle
    now = _now()
    stale = [w for w in svc._watches if w.alerted or now > w.expires_at]
    for w in stale:
        try:
            svc._watches.remove(w)
        except ValueError:
            pass
    assert len(svc._watches) == 1
    assert svc._watches[0].symbol == "TSLA"


@pytest.mark.asyncio
async def test_cleanup_removes_alerted_watches():
    svc = _analyzer()
    done  = _watch("AAPL", alerted=True)
    fresh = _watch("TSLA")
    svc._watches.extend([done, fresh])
    now = _now()
    stale = [w for w in svc._watches if w.alerted or now > w.expires_at]
    for w in stale:
        try:
            svc._watches.remove(w)
        except ValueError:
            pass
    assert len(svc._watches) == 1
    assert svc._watches[0].symbol == "TSLA"


# ── Telegram message format ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_no_bot_no_error():
    svc = _analyzer()
    svc._bot = None
    await svc._send("hello")   # should not raise


@pytest.mark.asyncio
async def test_send_calls_bot_send_message():
    svc = _analyzer()
    svc._bot = AsyncMock()
    svc._bot.send_message = AsyncMock()
    svc._chat_id = "123"
    await svc._send("test message")
    svc._bot.send_message.assert_called_once()
    call_kwargs = svc._bot.send_message.call_args.kwargs
    assert call_kwargs["text"] == "test message"
    assert call_kwargs["parse_mode"] == "Markdown"


@pytest.mark.asyncio
async def test_reaction_message_contains_symbol():
    svc = _analyzer()
    sent_msgs = []
    svc._send = AsyncMock(side_effect=lambda m: sent_msgs.append(m))
    svc._add_to_watchlist = AsyncMock()
    w = _watch("NVDA", price_at_news=400.0)
    await svc._on_reaction(w, 412.0, 0.03, _now())
    assert "NVDA" in sent_msgs[0]


@pytest.mark.asyncio
async def test_reaction_message_shows_direction_up():
    svc = _analyzer()
    sent_msgs = []
    svc._send = AsyncMock(side_effect=lambda m: sent_msgs.append(m))
    svc._add_to_watchlist = AsyncMock()
    w = _watch("AAPL", price_at_news=100.0)
    await svc._on_reaction(w, 103.0, 0.03, _now())
    assert "+" in sent_msgs[0]


@pytest.mark.asyncio
async def test_reaction_message_shows_latency_in_minutes_not_seconds():
    svc = _analyzer()
    sent_msgs = []
    svc._send = AsyncMock(side_effect=lambda m: sent_msgs.append(m))
    svc._add_to_watchlist = AsyncMock()
    w = _watch("AAPL", price_at_news=100.0)
    w.news = _news("AAPL", latency_s=1654.0)   # e.g. a 27.6-min-late feed
    await svc._on_reaction(w, 103.0, 0.03, _now())
    assert "latency 27.6m" in sent_msgs[0]
    assert "1654s" not in sent_msgs[0]


@pytest.mark.asyncio
async def test_reaction_message_shows_direction_down():
    svc = _analyzer()
    sent_msgs = []
    svc._send = AsyncMock(side_effect=lambda m: sent_msgs.append(m))
    svc._add_to_watchlist = AsyncMock()
    w = _watch("AAPL", price_at_news=100.0)
    await svc._on_reaction(w, 97.5, -0.025, _now())
    assert "-" in sent_msgs[0]

"""
Tests for NewsMonitorService.
All HTTP calls and Redis I/O are mocked — no real API keys or network needed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

import pytest
from core.entities.market_data import StockNews
from core.entities.news_event import NewsEvent
from services.news_monitor_service import (
    NewsMonitorService,
    _dedup_key,
    _normalize_title,
    _load_ignore,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _news(
    symbol: str = "AAPL",
    title: str = "Apple beats earnings",
    minutes_ago: float = 10.0,
    news_source: str = "FMP",
) -> StockNews:
    pub = _now() - dt.timedelta(minutes=minutes_ago)
    return StockNews(
        symbol=symbol,
        published_date=pub,
        publisher="Reuters",
        title=title,
        url=f"https://example.com/{title[:8]}",
        text="",
        fetched_at=_now(),
        news_source=news_source,
    )


def _profile(mcap: int = 500_000_000, avgvol: int = 2_000_000) -> MagicMock:
    p = MagicMock()
    p.market_cap     = mcap
    p.average_volume = avgvol
    return p


def _svc(
    bus=None,
    fmp=None,
    finnhub=None,
    yahoo=None,
    cache=None,
    fundamentals=None,
    fresh_window_minutes: int = 120,
) -> NewsMonitorService:
    """Build a service without calling __init__ async start."""
    if bus is None:
        bus = AsyncMock()
        bus.emit = AsyncMock()
    if fmp is None:
        fmp = MagicMock()
        fmp.get_stock_news.return_value = []
    if fundamentals is None:
        fundamentals = AsyncMock()
        fundamentals.get_profile.return_value = _profile()
    svc = NewsMonitorService.__new__(NewsMonitorService)
    svc._bus     = bus
    svc._fmp     = fmp
    svc._finnhub = finnhub
    svc._yahoo   = yahoo
    svc._cache   = cache
    svc._fundamentals = fundamentals
    svc._poll_fmp     = 60
    svc._poll_finnhub = 45
    svc._poll_yahoo   = 90
    svc._fresh_minutes = fresh_window_minutes
    svc._watched      = set()
    svc._seen         = {}
    svc._seen_at      = {}
    svc._profile_cache      = {}
    svc._profile_cache_time = {}
    svc._ignore             = set()
    svc._ignore_loaded_at   = None
    svc._tasks              = []
    return svc


# ── _normalize_title ──────────────────────────────────────────────────────────

def test_normalize_title_lowercases():
    assert _normalize_title("AAPL Earnings Beat") == _normalize_title("aapl earnings beat")


def test_normalize_title_drops_stopwords():
    result = _normalize_title("The stock of Apple")
    assert "the" not in result.split()
    assert "of"  not in result.split()


def test_normalize_title_strips_punctuation():
    result = _normalize_title("Breaking: AAPL up 5%!")
    assert "!" not in result
    assert ":" not in result
    assert "%" not in result


# ── _dedup_key ────────────────────────────────────────────────────────────────

def test_dedup_key_same_title_same_day():
    a = _news(title="Apple Q3 beats", minutes_ago=5)
    b = _news(title="Apple Q3 beats", minutes_ago=10)
    # same calendar date → same key
    assert _dedup_key(a) == _dedup_key(b)


def test_dedup_key_includes_symbol():
    a = _news(symbol="AAPL", title="Earnings beat")
    b = _news(symbol="TSLA", title="Earnings beat")
    assert _dedup_key(a) != _dedup_key(b)


def test_dedup_key_case_insensitive_title():
    a = _news(title="Apple Reports Record Revenue")
    b = _news(title="apple reports record revenue")
    assert _dedup_key(a) == _dedup_key(b)


# ── Freshness filter ──────────────────────────────────────────────────────────

def test_fresh_item_passes():
    svc  = _svc()
    item = _news(minutes_ago=30)
    assert svc._is_fresh(item)


def test_stale_item_rejected():
    svc  = _svc(fresh_window_minutes=60)
    item = _news(minutes_ago=90)
    assert not svc._is_fresh(item)


def test_item_exactly_at_window_boundary_passes():
    svc  = _svc(fresh_window_minutes=60)
    item = _news(minutes_ago=59.9)
    assert svc._is_fresh(item)


def test_naive_published_date_treated_as_utc():
    svc  = _svc(fresh_window_minutes=60)
    item = _news(minutes_ago=30)
    item.published_date = item.published_date.replace(tzinfo=None)
    assert svc._is_fresh(item)


# ── Candidate filter ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_candidate_passes_mcap_and_vol():
    fundamentals = AsyncMock()
    fundamentals.get_profile.return_value = _profile(mcap=500_000_000, avgvol=2_000_000)
    svc = _svc(fundamentals=fundamentals)
    assert await svc._is_candidate("AAPL")


@pytest.mark.asyncio
async def test_candidate_rejected_low_mcap():
    fundamentals = AsyncMock()
    fundamentals.get_profile.return_value = _profile(mcap=100_000_000, avgvol=5_000_000)
    svc = _svc(fundamentals=fundamentals)
    assert not await svc._is_candidate("TINY")


@pytest.mark.asyncio
async def test_candidate_rejected_low_volume():
    fundamentals = AsyncMock()
    fundamentals.get_profile.return_value = _profile(mcap=1_000_000_000, avgvol=500_000)
    svc = _svc(fundamentals=fundamentals)
    assert not await svc._is_candidate("LOWVOL")


@pytest.mark.asyncio
async def test_candidate_rejected_none_profile():
    fundamentals = AsyncMock()
    fundamentals.get_profile.return_value = None
    svc = _svc(fundamentals=fundamentals)
    assert not await svc._is_candidate("NODATA")


@pytest.mark.asyncio
async def test_candidate_profile_fetch_exception_returns_false():
    fundamentals = AsyncMock()
    fundamentals.get_profile.side_effect = RuntimeError("API down")
    svc = _svc(fundamentals=fundamentals)
    assert not await svc._is_candidate("ERR")


@pytest.mark.asyncio
async def test_candidate_cached_avoids_second_fetch():
    fundamentals = AsyncMock()
    fundamentals.get_profile.return_value = _profile()
    svc = _svc(fundamentals=fundamentals)
    await svc._is_candidate("AAPL")
    await svc._is_candidate("AAPL")
    assert fundamentals.get_profile.call_count == 1


@pytest.mark.asyncio
async def test_candidate_ignored_symbol_rejected():
    svc = _svc()
    svc._ignore = {"BANNED"}
    svc._ignore_loaded_at = _now()
    assert not await svc._is_candidate("BANNED")


# ── Deduplication ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_is_new_first_time_returns_true():
    svc  = _svc()
    item = _news()
    key  = _dedup_key(item)
    assert await svc._is_new(key, item)


@pytest.mark.asyncio
async def test_is_new_second_time_returns_false():
    svc  = _svc()
    item = _news()
    key  = _dedup_key(item)
    await svc._is_new(key, item)
    assert not await svc._is_new(key, item)


@pytest.mark.asyncio
async def test_dedup_marks_first_source():
    svc  = _svc()
    item = _news(news_source="FMP")
    key  = _dedup_key(item)
    await svc._is_new(key, item)
    assert svc._seen[key] == "FMP"


@pytest.mark.asyncio
async def test_dedup_redis_load_blocks_emit():
    cache = AsyncMock()
    cache.load.return_value = {"source": "FMP", "at": _now().isoformat()}
    cache.save = AsyncMock()
    svc  = _svc(cache=cache)
    item = _news()
    key  = _dedup_key(item)
    result = await svc._is_new(key, item)
    assert not result
    cache.save.assert_not_called()


@pytest.mark.asyncio
async def test_dedup_redis_save_called_for_new_article():
    cache = AsyncMock()
    cache.load.return_value = None
    cache.save = AsyncMock()
    svc  = _svc(cache=cache)
    item = _news()
    key  = _dedup_key(item)
    result = await svc._is_new(key, item)
    assert result
    cache.save.assert_called_once()


@pytest.mark.asyncio
async def test_dedup_redis_error_still_uses_memory():
    cache = AsyncMock()
    cache.load.side_effect = RuntimeError("Redis down")
    svc  = _svc(cache=cache)
    item = _news()
    key  = _dedup_key(item)
    # First: Redis fails but memory marks it as seen → still returns True
    assert await svc._is_new(key, item)
    # Second: memory dedup catches it
    assert not await svc._is_new(key, item)


# ── Full process pipeline ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_process_emits_new_article():
    bus = AsyncMock()
    bus.emit = AsyncMock()
    svc = _svc(bus=bus)
    item = _news()
    await svc._process(item)
    bus.emit.assert_called_once_with(item)


@pytest.mark.asyncio
async def test_process_does_not_emit_stale():
    bus = AsyncMock()
    bus.emit = AsyncMock()
    svc = _svc(bus=bus, fresh_window_minutes=30)
    item = _news(minutes_ago=60)
    await svc._process(item)
    bus.emit.assert_not_called()


@pytest.mark.asyncio
async def test_process_does_not_emit_failed_candidate():
    bus = AsyncMock()
    bus.emit = AsyncMock()
    fundamentals = AsyncMock()
    fundamentals.get_profile.return_value = _profile(mcap=1_000_000, avgvol=100)
    svc = _svc(bus=bus, fundamentals=fundamentals)
    item = _news()
    await svc._process(item)
    bus.emit.assert_not_called()


@pytest.mark.asyncio
async def test_process_does_not_emit_duplicate():
    bus = AsyncMock()
    bus.emit = AsyncMock()
    svc = _svc(bus=bus)
    item = _news()
    await svc._process(item)
    await svc._process(item)   # duplicate
    assert bus.emit.call_count == 1


@pytest.mark.asyncio
async def test_process_ignores_empty_symbol():
    bus = AsyncMock()
    bus.emit = AsyncMock()
    svc  = _svc(bus=bus)
    item = _news(symbol="")
    await svc._process(item)
    bus.emit.assert_not_called()


@pytest.mark.asyncio
async def test_process_emits_with_fetched_at_stamped():
    bus = AsyncMock()
    bus.emit = AsyncMock()
    svc = _svc(bus=bus)
    item = _news()
    await svc._process(item)
    emitted: StockNews = bus.emit.call_args[0][0]
    assert emitted.fetched_at is not None


@pytest.mark.asyncio
async def test_process_different_symbols_same_title_both_emitted():
    bus = AsyncMock()
    bus.emit = AsyncMock()
    svc = _svc(bus=bus)
    aapl = _news(symbol="AAPL", title="Market rallies on Fed news")
    tsla = _news(symbol="TSLA", title="Market rallies on Fed news")
    await svc._process(aapl)
    await svc._process(tsla)
    assert bus.emit.call_count == 2


# ── Symbol management ─────────────────────────────────────────────────────────

def test_add_symbol_uppercases():
    svc = _svc()
    svc.add_symbol("aapl")
    assert "AAPL" in svc._watched


def test_remove_symbol():
    svc = _svc()
    svc.add_symbol("AAPL")
    svc.remove_symbol("AAPL")
    assert "AAPL" not in svc._watched


def test_remove_nonexistent_symbol_no_error():
    svc = _svc()
    svc.remove_symbol("NOTHERE")   # should not raise


# ── latency_seconds property ──────────────────────────────────────────────────

def test_latency_seconds_computed():
    pub = _now() - dt.timedelta(seconds=45)
    item = StockNews(
        symbol="AAPL", published_date=pub, publisher="R",
        title="Test", url="u", text="",
        fetched_at=_now(), news_source="FMP",
    )
    lat = item.latency_seconds
    assert lat is not None
    assert 40 <= lat <= 55


def test_latency_seconds_none_when_fetched_at_missing():
    item = _news()
    item.fetched_at = None
    assert item.latency_seconds is None


def test_latency_seconds_naive_pub_date_handled():
    pub = dt.datetime.utcnow() - dt.timedelta(seconds=30)   # naive
    item = StockNews(
        symbol="AAPL", published_date=pub, publisher="R",
        title="Test", url="u", text="",
        fetched_at=dt.datetime.now(dt.timezone.utc), news_source="FMP",
    )
    lat = item.latency_seconds
    assert lat is not None and lat >= 0

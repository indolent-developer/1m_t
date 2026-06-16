"""
Tests for BenzingaDataFetcher.
All HTTP calls are mocked — no real API key needed.
"""
from __future__ import annotations

import datetime as dt
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

import pytest
import requests
from data_fetchers.benzinga_data_fetcher import BenzingaDataFetcher, _parse_date


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _fetcher() -> BenzingaDataFetcher:
    return BenzingaDataFetcher({"api_key": "test-key"})


def _raw_item(
    title="Apple Beats Q3",
    created="2026-06-09T05:12:33.000000",
    author="John Smith",
    url="https://benzinga.com/article/123",
    teaser="Apple beats earnings.",
) -> dict:
    return {
        "id":      "bz-123",
        "title":   title,
        "created": created,
        "author":  author,
        "url":     url,
        "teaser":  teaser,
        "body":    "Full body text...",
        "stocks":  [{"name": "AAPL"}],
        "channels": [],
        "tags":    [],
    }


def _mock_response(data, status=200):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = data
    return resp


# ── _parse_date ───────────────────────────────────────────────────────────────

def test_parse_date_iso_with_microseconds():
    d = _parse_date("2026-06-09T05:12:33.000000")
    assert d == dt.datetime(2026, 6, 9, 5, 12, 33)


def test_parse_date_iso_without_microseconds():
    d = _parse_date("2026-06-09T05:12:33")
    assert d == dt.datetime(2026, 6, 9, 5, 12, 33)


def test_parse_date_space_separated():
    d = _parse_date("2026-06-09 05:12:33")
    assert d == dt.datetime(2026, 6, 9, 5, 12, 33)


def test_parse_date_date_only():
    d = _parse_date("2026-06-09")
    assert d == dt.datetime(2026, 6, 9, 0, 0, 0)


def test_parse_date_rfc2822():
    d = _parse_date("Mon, 09 Jun 2026 05:12:33 -0400")
    assert d.year == 2026 and d.month == 6 and d.day == 9


def test_parse_date_empty_returns_now():
    d = _parse_date("")
    assert abs((d - dt.datetime.now()).total_seconds()) < 5


def test_parse_date_garbage_returns_now():
    d = _parse_date("not-a-date")
    assert abs((d - dt.datetime.now()).total_seconds()) < 5


def test_parse_date_with_tz_offset_stripped():
    d = _parse_date("2026-06-09T05:12:33+05:30")
    assert d.year == 2026 and d.month == 6 and d.day == 9


# ── get_stock_news — success ──────────────────────────────────────────────────

def test_get_stock_news_returns_items():
    fetcher = _fetcher()
    with patch.object(fetcher._session, "get", return_value=_mock_response([_raw_item()])):
        result = fetcher.get_stock_news(symbol="AAPL")
    assert len(result) == 1
    assert result[0].title == "Apple Beats Q3"
    assert result[0].publisher == "John Smith"
    assert result[0].site == "Benzinga"
    assert result[0].symbol == "AAPL"


def test_get_stock_news_parses_date():
    fetcher = _fetcher()
    with patch.object(fetcher._session, "get", return_value=_mock_response([_raw_item()])):
        result = fetcher.get_stock_news(symbol="AAPL")
    assert result[0].published_date == dt.datetime(2026, 6, 9, 5, 12, 33)


def test_get_stock_news_no_symbol_fetches_general_news():
    """Without a symbol, should still call the API (general news endpoint)."""
    fetcher = _fetcher()
    with patch.object(fetcher._session, "get", return_value=_mock_response([_raw_item()])) as m:
        result = fetcher.get_stock_news()
    assert len(result) == 1
    call_params = m.call_args[1]["params"]
    assert "tickers" not in call_params


def test_get_stock_news_passes_ticker_param():
    fetcher = _fetcher()
    with patch.object(fetcher._session, "get", return_value=_mock_response([])) as m:
        fetcher.get_stock_news(symbol="TSLA")
    params = m.call_args[1]["params"]
    assert params["tickers"] == "TSLA"


def test_get_stock_news_respects_limit():
    fetcher = _fetcher()
    items = [_raw_item(title=f"Article {i}") for i in range(10)]
    with patch.object(fetcher._session, "get", return_value=_mock_response(items)):
        result = fetcher.get_stock_news(symbol="AAPL", limit=3)
    assert len(result) == 3


def test_get_stock_news_skips_items_without_title():
    fetcher = _fetcher()
    raw = [{"id": "1", "title": "", "created": "2026-06-09T00:00:00"}]
    with patch.object(fetcher._session, "get", return_value=_mock_response(raw)):
        result = fetcher.get_stock_news(symbol="AAPL")
    assert result == []


def test_get_news_delegates_to_get_stock_news():
    fetcher = _fetcher()
    with patch.object(fetcher._session, "get", return_value=_mock_response([_raw_item()])):
        result = fetcher.get_news("AAPL")
    assert len(result) == 1


# ── get_stock_news — error handling ──────────────────────────────────────────

def test_get_stock_news_401_returns_empty():
    fetcher = _fetcher()
    with patch.object(fetcher._session, "get", return_value=_mock_response({}, status=401)):
        result = fetcher.get_stock_news(symbol="AAPL")
    assert result == []


def test_get_stock_news_500_returns_empty():
    fetcher = _fetcher()
    with patch.object(fetcher._session, "get", return_value=_mock_response({}, status=500)):
        result = fetcher.get_stock_news(symbol="AAPL")
    assert result == []


def test_get_stock_news_request_exception_returns_empty():
    fetcher = _fetcher()
    with patch.object(
        fetcher._session, "get", side_effect=requests.RequestException("timeout")
    ):
        result = fetcher.get_stock_news(symbol="AAPL")
    assert result == []


def test_get_stock_news_non_list_response_returns_empty():
    """If Benzinga returns a dict (e.g. error payload) instead of a list."""
    fetcher = _fetcher()
    with patch.object(fetcher._session, "get", return_value=_mock_response({"error": "bad"})):
        result = fetcher.get_stock_news(symbol="AAPL")
    assert result == []


def test_get_stock_news_json_decode_error_returns_empty():
    fetcher = _fetcher()
    bad_resp = MagicMock(spec=requests.Response)
    bad_resp.status_code = 200
    bad_resp.json.side_effect = ValueError("bad json")
    with patch.object(fetcher._session, "get", return_value=bad_resp):
        result = fetcher.get_stock_news(symbol="AAPL")
    assert result == []


# ── Caching ───────────────────────────────────────────────────────────────────

def test_get_stock_news_caches_on_second_call():
    fetcher = _fetcher()
    with patch.object(
        fetcher._session, "get", return_value=_mock_response([_raw_item()])
    ) as mock_get:
        fetcher.get_stock_news(symbol="AAPL")
        fetcher.get_stock_news(symbol="AAPL")
    assert mock_get.call_count == 1


def test_get_stock_news_different_symbols_not_cached_together():
    fetcher = _fetcher()
    with patch.object(
        fetcher._session, "get", return_value=_mock_response([_raw_item()])
    ) as mock_get:
        fetcher.get_stock_news(symbol="AAPL")
        fetcher.get_stock_news(symbol="TSLA")
    assert mock_get.call_count == 2


# ── Stubs ─────────────────────────────────────────────────────────────────────

def test_get_market_data_returns_empty():
    from core.entities.time_frame import TimeFrame
    assert _fetcher().get_market_data(
        "AAPL", dt.datetime.now() - dt.timedelta(days=1), dt.datetime.now(), TimeFrame.DAY
    ) == []


def test_get_price_batch_returns_empty():
    assert _fetcher().get_price_batch(["AAPL"]) == {}


def test_get_company_profile_returns_none():
    assert _fetcher().get_company_profile("AAPL") is None


def test_get_company_overview_returns_none():
    assert _fetcher().get_company_overview("AAPL") is None


def test_get_price_target_consensus_returns_none():
    assert _fetcher().get_price_target_consensus("AAPL") is None


def test_get_upgrades_downgrades_returns_empty():
    assert _fetcher().get_upgrades_downgrades("AAPL") == []


def test_get_calendar_returns_empty():
    import datetime
    assert _fetcher().get_calendar(
        "AAPL", datetime.date.today(), datetime.date.today()
    ) == []


def test_get_biggest_gainers_returns_empty():
    assert _fetcher().get_biggest_gainers() == []


def test_get_biggest_losers_returns_empty():
    assert _fetcher().get_biggest_losers() == []


def test_senate_trade_returns_empty():
    assert _fetcher().senate_trade("AAPL") == []


def test_get_earnings_call_transcript_returns_none():
    assert _fetcher().get_earnings_call_transcript("AAPL") is None

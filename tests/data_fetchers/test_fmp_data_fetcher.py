"""
Tests for FmpDataFetcher and DataFetcherBase.
All HTTP calls are mocked — no real API key or network needed.
"""
from __future__ import annotations

import datetime as dt
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

import pytest
from data_fetchers.data_fetcher_base import DataFetcherBase
from data_fetchers.financial_modelling_prep_data_fetcher import FmpDataFetcher
from core.entities.market_data import PriceQuote, StockNews
from core.entities.ohlc import OHLCData
from core.entities.time_frame import TimeFrame
from core.entities.analyst_data import Grade, PriceTargetConsensus


# ── Helpers ───────────────────────────────────────────────────────────────────

CONFIG = {"api_key": "test-key", "base_url": "https://fmp.test/api/v3/"}


def _fetcher() -> FmpDataFetcher:
    return FmpDataFetcher(CONFIG)


def _mock_response(data, status=200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = data
    resp.text = str(data)
    return resp


# ── DataFetcherBase — ABC enforcement ─────────────────────────────────────────

def test_cannot_instantiate_base_directly():
    with pytest.raises(TypeError):
        DataFetcherBase()


def test_concrete_without_all_abstract_raises():
    class Incomplete(DataFetcherBase):
        def get_market_data(self, *a, **kw): ...
        # missing other abstract methods
    with pytest.raises(TypeError):
        Incomplete()


def test_fmp_is_datafetcherbase_subclass():
    assert issubclass(FmpDataFetcher, DataFetcherBase)


# ── __init__ ──────────────────────────────────────────────────────────────────

def test_init_reads_api_key():
    f = _fetcher()
    assert f.api_key == "test-key"


def test_init_missing_api_key_raises():
    with pytest.raises(ValueError, match="api_key"):
        FmpDataFetcher({})


def test_init_default_base_url():
    f = FmpDataFetcher({"api_key": "k"})
    assert "financialmodelingprep" in f.base_url


# ── run_query / handle_response ───────────────────────────────────────────────

def test_run_query_returns_json():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response([{"symbol": "AAPL"}])):
        result = f.run_query("profile", params={"symbol": "AAPL"})
    assert result == [{"symbol": "AAPL"}]


def test_run_query_uses_cache_on_second_call():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response([{"id": 1}])) as mock_get:
        f.run_query("test-endpoint", cache_time_seconds=60)
        f.run_query("test-endpoint", cache_time_seconds=60)
    assert mock_get.call_count == 1


def test_run_query_skips_cache_when_zero():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"x": 1})) as mock_get:
        f.run_query("ep", cache_time_seconds=0)
        f.run_query("ep", cache_time_seconds=0)
    assert mock_get.call_count == 2


def test_handle_response_200_returns_json():
    f = _fetcher()
    assert f.handle_response(_mock_response({"ok": True})) == {"ok": True}


def test_handle_response_error_returns_none_no_throw():
    f = _fetcher()
    result = f.handle_response(_mock_response({}, status=401), throw_error=False)
    assert result is None


def test_handle_response_error_raises_when_throw():
    f = _fetcher()
    with pytest.raises(RuntimeError, match="401"):
        f.handle_response(_mock_response({}, status=401), throw_error=True)


# ── get_stock_news ────────────────────────────────────────────────────────────

def test_get_stock_news_with_symbol():
    f = _fetcher()
    raw = [{"symbol": "IREN", "publishedDate": "2026-06-03 07:00:00",
             "publisher": "Reuters", "title": "IREN spikes", "url": "http://x",
             "text": "body", "image": "", "site": "reuters.com"}]
    with patch("requests.get", return_value=_mock_response(raw)):
        news = f.get_stock_news("IREN")
    assert len(news) == 1
    assert news[0].symbol == "IREN"
    assert news[0].title  == "IREN spikes"


def test_get_news_delegates_to_get_stock_news():
    f = _fetcher()
    with patch.object(f, "get_stock_news", return_value=[]) as mock:
        f.get_news("AAPL")
    mock.assert_called_once_with(symbol="AAPL", page=0, limit=100)


def test_get_stock_news_returns_empty_on_bad_response():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(None)):
        news = f.get_stock_news("AAPL")
    assert news == []


# ── get_upgrades_downgrades ───────────────────────────────────────────────────

def test_get_upgrades_downgrades_maps_grades():
    f = _fetcher()
    raw = [{"symbol": "AAPL", "date": "2026-06-01", "gradingCompany": "MS",
             "action": "upgrade", "previousGrade": "Hold", "newGrade": "Buy"}]
    with patch("requests.get", return_value=_mock_response(raw)):
        grades = f.get_upgrades_downgrades("AAPL")
    assert len(grades) == 1
    assert isinstance(grades[0], Grade)


def test_get_upgrades_downgrades_empty_on_none():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(None)):
        result = f.get_upgrades_downgrades("AAPL")
    assert result == []


# ── get_price_target_consensus ────────────────────────────────────────────────

def test_get_price_target_consensus_maps_correctly():
    f = _fetcher()
    raw = [{"symbol": "AAPL", "targetHigh": 220.0, "targetLow": 180.0,
             "targetConsensus": 200.0, "targetMedian": 198.0}]
    with patch("requests.get", return_value=_mock_response(raw)):
        consensus = f.get_price_target_consensus("AAPL")
    assert isinstance(consensus, PriceTargetConsensus)
    assert consensus.target_high    == pytest.approx(220.0)
    assert consensus.target_low     == pytest.approx(180.0)
    assert consensus.target_consensus == pytest.approx(200.0)


def test_get_price_target_consensus_returns_none_on_empty():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response([])):
        result = f.get_price_target_consensus("AAPL")
    assert result is None


# ── get_market_data ───────────────────────────────────────────────────────────

def test_get_market_data_daily_maps_ohlc():
    f = _fetcher()
    raw = [{"open": 100.0, "high": 105.0, "low": 98.0, "close": 103.0,
             "volume": 1_000_000, "date": "2026-06-01"}]
    with patch("requests.get", return_value=_mock_response(raw)):
        data = f.get_market_data(
            "AAPL",
            dt.datetime(2026, 6, 1),
            dt.datetime(2026, 6, 3),
            TimeFrame.DAY,
        )
    assert len(data) == 1
    assert isinstance(data[0], OHLCData)
    assert data[0].open  == pytest.approx(100.0)
    assert data[0].close == pytest.approx(103.0)


def test_get_market_data_intraday_reverses_order():
    f = _fetcher()
    raw = [
        {"open": 102.0, "high": 103.0, "low": 101.0, "close": 102.5,
         "volume": 100, "date": "2026-06-01 10:00:00"},
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
         "volume": 100, "date": "2026-06-01 09:30:00"},
    ]
    with patch("requests.get", return_value=_mock_response(raw)):
        data = f.get_market_data(
            "AAPL",
            dt.datetime(2026, 6, 1),
            dt.datetime(2026, 6, 1),
            TimeFrame.MINUTE_1,
        )
    # After reverse: earliest bar should be first
    assert data[0].open == pytest.approx(100.0)
    assert data[1].open == pytest.approx(102.0)


def test_get_market_data_unsupported_timeframe_raises():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response([])):
        with pytest.raises(ValueError, match="Unsupported"):
            f.get_market_data("AAPL", dt.datetime.now(), dt.datetime.now(), TimeFrame.WEEK)


def test_get_market_data_returns_empty_on_none_response():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(None)):
        result = f.get_market_data("AAPL", dt.datetime.now(), dt.datetime.now(), TimeFrame.DAY)
    assert result == []


# ── get_price_batch ───────────────────────────────────────────────────────────

_FMP_MODULE = "data_fetchers.financial_modelling_prep_data_fetcher"


def test_get_price_batch_regular_hours():
    f = _fetcher()
    raw = [{"symbol": "AAPL", "price": 185.0, "volume": 1000,
             "timestamp": 0, "changePercentage": 1.5,
             "dayHigh": 186.0, "dayLow": 183.0}]
    with patch(f"{_FMP_MODULE}.is_extended_market_time", return_value=False), \
         patch("requests.get", return_value=_mock_response(raw)):
        result = f.get_price_batch(["AAPL"])

    assert "AAPL" in result
    assert result["AAPL"].bid_price == pytest.approx(185.0)


def test_get_price_batch_extended_hours():
    f = _fetcher()
    raw = [{"symbol": "AAPL", "bidPrice": 184.5, "askPrice": 185.0,
             "bidSize": 10, "askSize": 10, "volume": 500, "timestamp": 0}]
    with patch(f"{_FMP_MODULE}.is_extended_market_time", return_value=True), \
         patch("requests.get", return_value=_mock_response(raw)):
        result = f.get_price_batch(["AAPL"])

    assert "AAPL" in result
    assert result["AAPL"].bid_price == pytest.approx(184.5)


def test_get_price_delegates_to_batch():
    f = _fetcher()
    quote = PriceQuote(symbol="AAPL", bid_price=185.0, ask_price=185.5)
    with patch.object(f, "get_price_batch", return_value={"AAPL": quote}):
        price = f.get_price("AAPL")
    assert price == pytest.approx(185.0)


def test_get_price_returns_zero_if_missing():
    f = _fetcher()
    with patch.object(f, "get_price_batch", return_value={}):
        price = f.get_price("MISSING")
    assert price == 0.0


# ── PriceQuote entity ─────────────────────────────────────────────────────────

def test_price_quote_mid():
    q = PriceQuote(symbol="X", bid_price=100.0, ask_price=102.0)
    assert q.mid == pytest.approx(101.0)


def test_price_quote_price_returns_bid():
    q = PriceQuote(symbol="X", bid_price=99.5, ask_price=100.0)
    assert q.price == pytest.approx(99.5)


# ── _convert_timeframe_to_api_param ──────────────────────────────────────────

@pytest.mark.parametrize("tf,expected", [
    (TimeFrame.MINUTE_1,  "1min"),
    (TimeFrame.MINUTE_5,  "5min"),
    (TimeFrame.MINUTE_15, "15min"),
    (TimeFrame.MINUTE_30, "30min"),
    (TimeFrame.HOUR_1,    "1hour"),
    (TimeFrame.DAY,       "1day"),
    (TimeFrame.WEEK,      "1week"),
    (TimeFrame.MONTH,     "1month"),
])
def test_convert_timeframe(tf, expected):
    assert _fetcher()._convert_timeframe_to_api_param(tf) == expected

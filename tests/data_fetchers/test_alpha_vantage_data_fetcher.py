"""
Tests for AlphaVantageDataFetcher.
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
from data_fetchers.alpha_vantage_data_fetcher import AlphaVantageDataFetcher
from core.entities.market_data import PriceQuote, StockNews
from core.entities.ohlc import OHLCData
from core.entities.time_frame import TimeFrame
from core.entities.analyst_data import PriceTargetConsensus
from core.entities.company_profile import CompanyProfile


# ── Helpers ───────────────────────────────────────────────────────────────────

CONFIG = {"api_key": "test-key"}
_MOD   = "data_fetchers.alpha_vantage_data_fetcher"


def _fetcher() -> AlphaVantageDataFetcher:
    return AlphaVantageDataFetcher(CONFIG)


def _mock_response(data, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


def _mock_error(status: int = 429) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status.side_effect = Exception(f"HTTP {status}")
    return resp


# ── ABC / subclass ────────────────────────────────────────────────────────────

def test_is_datafetcherbase_subclass():
    assert issubclass(AlphaVantageDataFetcher, DataFetcherBase)


def test_init_reads_api_key():
    assert _fetcher().api_key == "test-key"


def test_init_missing_key_raises():
    with pytest.raises(ValueError, match="api_key"):
        AlphaVantageDataFetcher({})


# ── _get — rate limit / error handling ───────────────────────────────────────

def test_get_returns_none_on_rate_limit_info():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"Information": "demo only"})):
        result = f._get({"function": "GLOBAL_QUOTE", "symbol": "AAPL"}, cache_ttl=0)
    assert result is None


def test_get_returns_none_on_error_message():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"Error Message": "invalid symbol"})):
        result = f._get({"function": "GLOBAL_QUOTE", "symbol": "FAKE"}, cache_ttl=0)
    assert result is None


def test_get_returns_none_on_http_error():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_error(429)):
        result = f._get({"function": "GLOBAL_QUOTE"}, cache_ttl=0)
    assert result is None


def test_get_caches_on_second_call():
    f    = _fetcher()
    data = {"feed": []}
    with patch("requests.get", return_value=_mock_response(data)) as mock_get:
        f._get({"function": "NEWS_SENTIMENT"}, cache_ttl=60)
        f._get({"function": "NEWS_SENTIMENT"}, cache_ttl=60)
    assert mock_get.call_count == 1


def test_get_skips_cache_when_zero_ttl():
    f    = _fetcher()
    data = {"feed": []}
    with patch("requests.get", return_value=_mock_response(data)) as mock_get:
        f._get({"function": "NEWS_SENTIMENT"}, cache_ttl=0)
        f._get({"function": "NEWS_SENTIMENT"}, cache_ttl=0)
    assert mock_get.call_count == 2


# ── get_stock_news ────────────────────────────────────────────────────────────

_AV_FEED = {
    "items": "1",
    "feed": [{
        "title":                 "Apple beats Q3",
        "url":                   "https://example.com/apple",
        "time_published":        "20260608T120000",
        "authors":               ["John Doe"],
        "summary":               "Apple reported strong Q3 earnings.",
        "source":                "Reuters",
        "source_domain":         "reuters.com",
        "banner_image":          "https://example.com/img.jpg",
        "overall_sentiment_score": 0.35,
        "overall_sentiment_label": "Somewhat-Bullish",
        "ticker_sentiment": [{
            "ticker":                 "AAPL",
            "relevance_score":        "1.0",
            "ticker_sentiment_score": "0.45",
            "ticker_sentiment_label": "Bullish",
        }],
    }],
}


def test_get_stock_news_returns_list():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_AV_FEED)):
        news = f.get_stock_news(symbol="AAPL")
    assert len(news) == 1
    assert isinstance(news[0], StockNews)


def test_get_stock_news_maps_fields_correctly():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_AV_FEED)):
        news = f.get_stock_news(symbol="AAPL")
    n = news[0]
    assert n.title     == "Apple beats Q3"
    assert n.url       == "https://example.com/apple"
    assert n.publisher == "Reuters"
    assert n.site      == "reuters.com"
    assert n.text      == "Apple reported strong Q3 earnings."
    assert n.symbol    == "AAPL"


def test_get_stock_news_parses_timestamp():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_AV_FEED)):
        news = f.get_stock_news(symbol="AAPL")
    assert news[0].published_date == dt.datetime(2026, 6, 8, 12, 0, 0)


def test_get_stock_news_returns_empty_on_rate_limit():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"Information": "rate limit"})):
        news = f.get_stock_news(symbol="AAPL")
    assert news == []


def test_get_stock_news_no_symbol_omits_tickers_param():
    """NEWS_SENTIMENT without a symbol returns general market news."""
    f    = _fetcher()
    data = {"feed": []}
    with patch("requests.get", return_value=_mock_response(data)) as mock_get:
        f.get_stock_news(symbol=None)
    call_params = mock_get.call_args[1]["params"]
    assert "tickers" not in call_params


def test_get_news_delegates_to_get_stock_news():
    f = _fetcher()
    with patch.object(f, "get_stock_news", return_value=[]) as mock:
        f.get_news("AAPL")
    mock.assert_called_once_with(symbol="AAPL", page=0, limit=50)


# ── get_market_data — daily ───────────────────────────────────────────────────

_DAILY_TS = {
    "Meta Data": {"2. Symbol": "IBM"},
    "Time Series (Daily)": {
        "2026-06-08": {"1. open": "286.43", "2. high": "290.50",
                       "3. low": "279.43", "4. close": "280.89", "5. volume": "6653230"},
        "2026-06-05": {"1. open": "300.00", "2. high": "302.30",
                       "3. low": "281.07", "4. close": "284.84", "5. volume": "12509480"},
    },
}


def test_get_market_data_daily_returns_ohlc():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_DAILY_TS)):
        data = f.get_market_data(
            "IBM",
            dt.datetime(2026, 6, 5),
            dt.datetime(2026, 6, 8),
            TimeFrame.DAY,
        )
    assert len(data) == 2
    assert all(isinstance(b, OHLCData) for b in data)


def test_get_market_data_daily_sorted_oldest_first():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_DAILY_TS)):
        data = f.get_market_data("IBM", dt.datetime(2026, 6, 1), dt.datetime(2026, 6, 9), TimeFrame.DAY)
    assert data[0].time < data[1].time


def test_get_market_data_daily_filters_by_date():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_DAILY_TS)):
        data = f.get_market_data("IBM", dt.datetime(2026, 6, 8), dt.datetime(2026, 6, 8), TimeFrame.DAY)
    assert len(data) == 1
    assert data[0].close == pytest.approx(280.89)


def test_get_market_data_daily_returns_empty_on_rate_limit():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"Information": "limit"})):
        data = f.get_market_data("IBM", dt.datetime(2026, 6, 1), dt.datetime(2026, 6, 9), TimeFrame.DAY)
    assert data == []


# ── get_market_data — intraday ────────────────────────────────────────────────

_INTRADAY_5M = {
    "Meta Data": {"2. Symbol": "IBM", "4. Interval": "5min"},
    "Time Series (5min)": {
        "2026-06-05 19:55:00": {"1. open": "280.10", "2. high": "281.19",
                                "3. low": "280.08", "4. close": "280.75", "5. volume": "1556"},
        "2026-06-05 19:50:00": {"1. open": "280.60", "2. high": "280.60",
                                "3. low": "280.08", "4. close": "280.37", "5. volume": "2630"},
    },
}


def test_get_market_data_intraday_5m_maps_correctly():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_INTRADAY_5M)):
        data = f.get_market_data(
            "IBM",
            dt.datetime(2026, 6, 5, 0, 0),
            dt.datetime(2026, 6, 5, 23, 59),
            TimeFrame.MINUTE_5,
        )
    assert len(data) == 2
    assert data[0].time < data[1].time


def test_get_market_data_unsupported_timeframe_raises():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({})):
        with pytest.raises(ValueError, match="unsupported timeframe"):
            f.get_market_data("IBM", dt.datetime.now(), dt.datetime.now(), TimeFrame.HOUR_4)


# ── get_price_batch ───────────────────────────────────────────────────────────

_GLOBAL_QUOTE = {
    "Global Quote": {
        "01. symbol":           "AAPL",
        "02. open":             "185.00",
        "03. high":             "187.00",
        "04. low":              "183.00",
        "05. price":            "186.00",
        "06. volume":           "50000000",
        "07. latest trading day": "2026-06-08",
        "08. previous close":   "184.50",
        "09. change":           "1.50",
        "10. change percent":   "0.8130%",
    }
}


def test_get_price_batch_returns_quote():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_GLOBAL_QUOTE)):
        result = f.get_price_batch(["AAPL"])
    assert "AAPL" in result
    assert isinstance(result["AAPL"], PriceQuote)


def test_get_price_batch_maps_fields():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_GLOBAL_QUOTE)):
        result = f.get_price_batch(["AAPL"])
    q = result["AAPL"]
    assert q.bid_price        == pytest.approx(186.0)
    assert q.day_high         == pytest.approx(187.0)
    assert q.day_low          == pytest.approx(183.0)
    assert q.change_percentage == pytest.approx(0.813)
    assert q.volume           == 50_000_000


def test_get_price_batch_skips_symbol_on_rate_limit():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"Information": "rate limit"})):
        result = f.get_price_batch(["AAPL"])
    assert result == {}


def test_get_price_delegates_to_batch():
    f = _fetcher()
    q = PriceQuote(symbol="AAPL", bid_price=186.0, ask_price=186.0)
    with patch.object(f, "get_price_batch", return_value={"AAPL": q}):
        price = f.get_price("AAPL")
    assert price == pytest.approx(186.0)


# ── get_company_overview / get_company_profile ────────────────────────────────

_OVERVIEW = {
    "Symbol": "IBM",
    "Name": "International Business Machines",
    "CIK": "51143",
    "Exchange": "NYSE",
    "Currency": "USD",
    "Country": "USA",
    "Sector": "TECHNOLOGY",
    "Industry": "INFORMATION TECHNOLOGY SERVICES",
    "Description": "IBM is a technology company.",
    "OfficialSite": "https://www.ibm.com",
    "MarketCapitalization": "267716936000",
    "Beta": "0.82",
    "AnalystTargetPrice": "290.17",
}


def test_get_company_overview_returns_dict():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_OVERVIEW)):
        result = f.get_company_overview("IBM")
    assert result["Symbol"] == "IBM"


def test_get_company_profile_maps_fields():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_OVERVIEW)):
        profile = f.get_company_profile("IBM")
    assert isinstance(profile, CompanyProfile)
    assert profile.symbol       == "IBM"
    assert profile.company_name == "International Business Machines"
    assert profile.sector       == "TECHNOLOGY"
    assert profile.exchange     == "NYSE"
    assert profile.market_cap   == 267_716_936_000


def test_get_company_profile_returns_none_on_error():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"Information": "rate limit"})):
        profile = f.get_company_profile("IBM")
    assert profile is None


# ── get_price_target_consensus ────────────────────────────────────────────────

def test_get_price_target_consensus_from_overview():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_OVERVIEW)):
        consensus = f.get_price_target_consensus("IBM")
    assert isinstance(consensus, PriceTargetConsensus)
    assert consensus.target_consensus == pytest.approx(290.17)
    assert consensus.target_median    == pytest.approx(290.17)


def test_get_price_target_consensus_returns_none_when_no_target():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"Symbol": "X"})):
        result = f.get_price_target_consensus("X")
    assert result is None


# ── get_biggest_gainers / losers ──────────────────────────────────────────────

_TOP = {
    "metadata": "...",
    "last_updated": "...",
    "top_gainers": [{"ticker": "AAPL", "change_percentage": "5%"}],
    "top_losers":  [{"ticker": "IBM",  "change_percentage": "-3%"}],
    "most_actively_traded": [],
}


def test_get_biggest_gainers():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_TOP)):
        result = f.get_biggest_gainers()
    assert result[0]["ticker"] == "AAPL"


def test_get_biggest_losers():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_TOP)):
        result = f.get_biggest_losers()
    assert result[0]["ticker"] == "IBM"


# ── stubs ─────────────────────────────────────────────────────────────────────

def test_senate_trade_returns_empty():
    assert _fetcher().senate_trade("AAPL") == []


def test_upgrades_downgrades_returns_empty():
    assert _fetcher().get_upgrades_downgrades("AAPL") == []


def test_earnings_transcript_returns_none():
    assert _fetcher().get_earnings_call_transcript("AAPL") is None

"""
Tests for FinnhubDataFetcher.
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
from data_fetchers.finnhub_data_fetcher import FinnhubDataFetcher
from core.entities.market_data import PriceQuote, StockNews
from core.entities.ohlc import OHLCData
from core.entities.time_frame import TimeFrame
from core.entities.analyst_data import Grade, PriceTargetConsensus
from core.entities.company_profile import CompanyProfile


# ── Helpers ───────────────────────────────────────────────────────────────────

CONFIG = {"api_key": "test-key"}


def _fetcher() -> FinnhubDataFetcher:
    return FinnhubDataFetcher(CONFIG)


def _mock_response(data, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


def _mock_http_error(status: int = 403) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status.side_effect = Exception(f"HTTP {status}")
    return resp


# ── ABC / subclass ────────────────────────────────────────────────────────────

def test_is_datafetcherbase_subclass():
    assert issubclass(FinnhubDataFetcher, DataFetcherBase)


def test_init_reads_api_key():
    assert _fetcher().api_key == "test-key"


def test_init_missing_key_raises():
    with pytest.raises(ValueError, match="api_key"):
        FinnhubDataFetcher({})


# ── _get — caching and error handling ────────────────────────────────────────

def test_get_caches_on_second_call():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response([])) as mock_get:
        f._get("company-news", {"symbol": "AAPL", "from": "2026-06-01", "to": "2026-06-08"}, cache_ttl=60)
        f._get("company-news", {"symbol": "AAPL", "from": "2026-06-01", "to": "2026-06-08"}, cache_ttl=60)
    assert mock_get.call_count == 1


def test_get_skips_cache_on_zero_ttl():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response([])) as mock_get:
        f._get("quote", {"symbol": "AAPL"}, cache_ttl=0)
        f._get("quote", {"symbol": "AAPL"}, cache_ttl=0)
    assert mock_get.call_count == 2


def test_get_returns_none_on_error_field():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"error": "Invalid API key."})):
        result = f._get("quote", {"symbol": "AAPL"}, cache_ttl=0)
    assert result is None


def test_get_returns_none_on_http_error():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_http_error(403)):
        result = f._get("quote", {"symbol": "AAPL"}, cache_ttl=0)
    assert result is None


# ── get_stock_news ────────────────────────────────────────────────────────────

_COMPANY_NEWS = [
    {
        "category": "company",
        "datetime": 1749398400,   # approx 2025-06-08
        "headline": "Apple Q3 revenue beats estimates",
        "id": 123,
        "image": "https://example.com/img.jpg",
        "related": "AAPL",
        "source": "Reuters",
        "summary": "Apple reported strong Q3.",
        "url": "https://reuters.com/apple-q3",
    },
    {
        "category": "company",
        "datetime": 1749312000,
        "headline": "Apple announces new iPhone",
        "id": 124,
        "image": "",
        "related": "AAPL",
        "source": "Bloomberg",
        "summary": "New iPhone details.",
        "url": "https://bloomberg.com/iphone",
    },
]


def test_get_stock_news_returns_list():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_COMPANY_NEWS)):
        news = f.get_stock_news(symbol="AAPL")
    assert len(news) == 2
    assert all(isinstance(n, StockNews) for n in news)


def test_get_stock_news_maps_fields():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_COMPANY_NEWS)):
        news = f.get_stock_news(symbol="AAPL")
    n = news[0]
    assert n.title     == "Apple Q3 revenue beats estimates"
    assert n.url       == "https://reuters.com/apple-q3"
    assert n.publisher == "Reuters"
    assert n.text      == "Apple reported strong Q3."
    assert n.symbol    == "AAPL"


def test_get_stock_news_converts_unix_timestamp():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_COMPANY_NEWS)):
        news = f.get_stock_news(symbol="AAPL")
    assert isinstance(news[0].published_date, dt.datetime)


def test_get_stock_news_returns_empty_without_symbol():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_COMPANY_NEWS)):
        news = f.get_stock_news(symbol=None)
    assert news == []


def test_get_stock_news_returns_empty_on_api_error():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"error": "Invalid API key."})):
        news = f.get_stock_news(symbol="AAPL")
    assert news == []


def test_get_stock_news_handles_non_list_response():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"message": "no data"})):
        news = f.get_stock_news(symbol="AAPL")
    assert news == []


def test_get_stock_news_respects_limit():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_COMPANY_NEWS)):
        news = f.get_stock_news(symbol="AAPL", limit=1)
    assert len(news) == 1


def test_get_news_delegates_to_get_stock_news():
    f = _fetcher()
    with patch.object(f, "get_stock_news", return_value=[]) as mock:
        f.get_news("AAPL")
    mock.assert_called_once_with(symbol="AAPL", page=0, limit=50)


# ── get_market_data ───────────────────────────────────────────────────────────

_CANDLES_OK = {
    "c": [280.75, 281.10],
    "h": [281.19, 281.50],
    "l": [280.08, 280.90],
    "o": [280.10, 280.80],
    "s": "ok",
    "t": [1749225600, 1749225900],  # unix timestamps
    "v": [1556, 2100],
}

_CANDLES_NO_DATA = {"s": "no_data"}


def test_get_market_data_returns_ohlc_list():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_CANDLES_OK)):
        data = f.get_market_data(
            "AAPL",
            dt.datetime(2026, 6, 6),
            dt.datetime(2026, 6, 8),
            TimeFrame.MINUTE_5,
        )
    assert len(data) == 2
    assert all(isinstance(b, OHLCData) for b in data)


def test_get_market_data_maps_ohlcv():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_CANDLES_OK)):
        data = f.get_market_data("AAPL", dt.datetime(2026, 6, 6), dt.datetime(2026, 6, 8), TimeFrame.MINUTE_5)
    b = data[0]
    assert b.open   == pytest.approx(280.10)
    assert b.high   == pytest.approx(281.19)
    assert b.low    == pytest.approx(280.08)
    assert b.close  == pytest.approx(280.75)
    assert b.volume == pytest.approx(1556.0)


def test_get_market_data_daily_resolution():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_CANDLES_OK)) as mock_get:
        f.get_market_data("AAPL", dt.datetime(2026, 6, 1), dt.datetime(2026, 6, 8), TimeFrame.DAY)
    call_params = mock_get.call_args[1]["params"]
    assert call_params["resolution"] == "D"


def test_get_market_data_returns_empty_on_no_data():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_CANDLES_NO_DATA)):
        data = f.get_market_data("AAPL", dt.datetime(2026, 6, 1), dt.datetime(2026, 6, 8), TimeFrame.DAY)
    assert data == []


def test_get_market_data_raises_on_unsupported_timeframe():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_CANDLES_OK)):
        with pytest.raises(ValueError, match="unsupported timeframe"):
            f.get_market_data("AAPL", dt.datetime.now(), dt.datetime.now(), TimeFrame.HOUR_4)


# ── get_price_batch ───────────────────────────────────────────────────────────

_QUOTE = {
    "c":  186.50,    # current price
    "d":  1.50,      # change
    "dp": 0.81,      # change %
    "h":  187.20,    # day high
    "l":  183.80,    # day low
    "o":  185.00,    # open
    "pc": 185.00,    # prev close
    "t":  1749398400,
    "v":  45_000_000,
}


def test_get_price_batch_returns_quote():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_QUOTE)):
        result = f.get_price_batch(["AAPL"])
    assert "AAPL" in result
    assert isinstance(result["AAPL"], PriceQuote)


def test_get_price_batch_maps_fields():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_QUOTE)):
        result = f.get_price_batch(["AAPL"])
    q = result["AAPL"]
    assert q.bid_price        == pytest.approx(186.50)
    assert q.day_high         == pytest.approx(187.20)
    assert q.day_low          == pytest.approx(183.80)
    assert q.change_percentage == pytest.approx(0.81)
    assert q.volume           == 45_000_000


def test_get_price_batch_skips_on_api_error():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"error": "Invalid key."})):
        result = f.get_price_batch(["AAPL"])
    assert result == {}


def test_get_price_delegates_to_batch():
    f = _fetcher()
    q = PriceQuote(symbol="AAPL", bid_price=186.5, ask_price=186.5)
    with patch.object(f, "get_price_batch", return_value={"AAPL": q}):
        price = f.get_price("AAPL")
    assert price == pytest.approx(186.5)


# ── get_company_profile ───────────────────────────────────────────────────────

_PROFILE2 = {
    "country":               "US",
    "currency":              "USD",
    "exchange":              "NASDAQ",
    "finnhubIndustry":       "Technology",
    "ipo":                   "1980-12-12",
    "logo":                  "https://example.com/aapl.png",
    "marketCapitalization":  2950000.0,  # millions
    "name":                  "Apple Inc",
    "phone":                 "14089961010",
    "shareOutstanding":      15204.14,
    "ticker":                "AAPL",
    "weburl":                "https://www.apple.com/",
}


def test_get_company_profile_returns_profile():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_PROFILE2)):
        profile = f.get_company_profile("AAPL")
    assert isinstance(profile, CompanyProfile)


def test_get_company_profile_maps_fields():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_PROFILE2)):
        profile = f.get_company_profile("AAPL")
    assert profile.symbol       == "AAPL"
    assert profile.company_name == "Apple Inc"
    assert profile.exchange     == "NASDAQ"
    assert profile.currency     == "USD"
    assert profile.country      == "US"
    assert profile.industry     == "Technology"
    assert profile.market_cap   == 2_950_000_000_000


def test_get_company_profile_returns_none_on_error():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"error": "Invalid key."})):
        profile = f.get_company_profile("AAPL")
    assert profile is None


# ── get_upgrades_downgrades ───────────────────────────────────────────────────

_RECOMMENDATIONS = [
    {"buy": 20, "hold": 8, "period": "2026-06-01", "sell": 2, "strongBuy": 10, "strongSell": 0, "symbol": "AAPL"},
    {"buy": 18, "hold": 9, "period": "2026-05-01", "sell": 3, "strongBuy": 8,  "strongSell": 1, "symbol": "AAPL"},
]


def test_get_upgrades_downgrades_returns_grades():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_RECOMMENDATIONS)):
        grades = f.get_upgrades_downgrades("AAPL")
    assert len(grades) == 2
    assert all(isinstance(g, Grade) for g in grades)


def test_get_upgrades_downgrades_dominant_action():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_RECOMMENDATIONS)):
        grades = f.get_upgrades_downgrades("AAPL")
    # strongBuy(10) + buy(20) = 30, hold=8, sell=2 → dominant is "buy"
    assert grades[0].action in ("Buy", "Strong Buy")


def test_get_upgrades_downgrades_returns_empty_on_error():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"error": "Invalid key."})):
        grades = f.get_upgrades_downgrades("AAPL")
    assert grades == []


# ── get_price_target_consensus ────────────────────────────────────────────────

_PRICE_TARGET = {
    "lastUpdated": "2026-06-01",
    "symbol":      "AAPL",
    "targetHigh":  220.0,
    "targetLow":   180.0,
    "targetMean":  200.0,
    "targetMedian": 198.0,
}


def test_get_price_target_consensus_maps_correctly():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_PRICE_TARGET)):
        consensus = f.get_price_target_consensus("AAPL")
    assert isinstance(consensus, PriceTargetConsensus)
    assert consensus.target_high     == pytest.approx(220.0)
    assert consensus.target_low      == pytest.approx(180.0)
    assert consensus.target_consensus == pytest.approx(200.0)
    assert consensus.target_median   == pytest.approx(198.0)


def test_get_price_target_returns_none_on_error():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"error": "Invalid key."})):
        result = f.get_price_target_consensus("AAPL")
    assert result is None


# ── get_calendar ──────────────────────────────────────────────────────────────

_EARNINGS_CAL = {
    "earningsCalendar": [
        {
            "date": "2026-07-30",
            "eps": None,
            "epsEstimate": 1.55,
            "hour": "amc",
            "quarter": 3,
            "revenueEstimate": 94_500_000_000,
            "symbol": "AAPL",
            "year": 2026,
        }
    ]
}


def test_get_calendar_returns_list():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response(_EARNINGS_CAL)):
        result = f.get_calendar(
            "AAPL",
            dt.date(2026, 7, 1),
            dt.date(2026, 8, 1),
        )
    assert len(result) == 1
    assert result[0]["symbol"] == "AAPL"


def test_get_calendar_returns_empty_on_error():
    f = _fetcher()
    with patch("requests.get", return_value=_mock_response({"error": "Invalid key."})):
        result = f.get_calendar("AAPL", dt.date.today(), dt.date.today())
    assert result == []


# ── stubs ─────────────────────────────────────────────────────────────────────

def test_senate_trade_returns_empty():
    assert _fetcher().senate_trade("AAPL") == []


def test_biggest_gainers_returns_empty():
    assert _fetcher().get_biggest_gainers() == []


def test_biggest_losers_returns_empty():
    assert _fetcher().get_biggest_losers() == []


def test_earnings_transcript_returns_none():
    assert _fetcher().get_earnings_call_transcript("AAPL") is None

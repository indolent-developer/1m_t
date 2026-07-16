"""
Tests for YahooFinanceDataFetcher.
All yfinance calls are mocked — no network access required.
"""
from __future__ import annotations

import datetime as dt
import sys
import os
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

import pytest
from data_fetchers.yahoo_finance_data_fetcher import YahooFinanceDataFetcher
from core.entities.market_data import StockNews


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _fetcher() -> YahooFinanceDataFetcher:
    return YahooFinanceDataFetcher()


def _raw_news_item(
    title="Test Article",
    pub_date="2026-06-09T05:12:33Z",
    publisher="Reuters",
    url="https://finance.yahoo.com/test",
    has_content=True,
) -> dict:
    """Simulate a yfinance news item in the new 'content' format."""
    if has_content:
        return {
            "content": {
                "title": title,
                "pubDate": pub_date,
                "provider": {"displayName": publisher},
                "canonicalUrl": {"url": url},
                "summary": "A summary of the article.",
                "thumbnail": {
                    "resolutions": [{"url": "https://img.example.com/thumb.jpg"}]
                },
            }
        }
    return {
        "title": title,
        "providerPublishTime": 1749440400,  # 2026-06-09T05:00:00 UTC
        "publisher": publisher,
        "link": url,
    }


def _mock_ticker(news=None) -> MagicMock:
    ticker = MagicMock()
    ticker.news = news if news is not None else []
    return ticker


# ── _parse_news_item ──────────────────────────────────────────────────────────

def test_parse_news_item_new_format():
    fetcher = _fetcher()
    item = _raw_news_item()
    result = fetcher._parse_news_item(item, "AAPL")
    assert result is not None
    assert result.title == "Test Article"
    assert result.publisher == "Reuters"
    assert result.url == "https://finance.yahoo.com/test"
    assert result.published_date == dt.datetime(2026, 6, 9, 5, 12, 33, tzinfo=dt.timezone.utc)
    assert result.image == "https://img.example.com/thumb.jpg"
    assert result.symbol == "AAPL"


def test_parse_news_item_old_format():
    fetcher = _fetcher()
    item = _raw_news_item(has_content=False)
    result = fetcher._parse_news_item(item, "ABAT")
    assert result is not None
    assert result.title == "Test Article"
    assert result.publisher == "Reuters"
    assert result.symbol == "ABAT"


def test_parse_news_item_missing_title_returns_none():
    fetcher = _fetcher()
    result = fetcher._parse_news_item({"content": {}}, "AAPL")
    assert result is None


def test_parse_news_item_site_is_yahoo_finance():
    fetcher = _fetcher()
    result = fetcher._parse_news_item(_raw_news_item(), "AAPL")
    assert result.site == "Yahoo Finance"


def test_parse_news_item_bad_pub_date_falls_back_gracefully():
    fetcher = _fetcher()
    item = {"content": {"title": "Story", "pubDate": "not-a-date"}}
    result = fetcher._parse_news_item(item, "AAPL")
    assert result is not None


def test_parse_news_item_empty_provider_dict():
    fetcher = _fetcher()
    item = {"content": {"title": "Story", "pubDate": "2026-06-09T00:00:00Z", "provider": {}}}
    result = fetcher._parse_news_item(item, "AAPL")
    assert result is not None
    assert result.publisher == ""


# ── get_stock_news ────────────────────────────────────────────────────────────

def test_get_stock_news_returns_parsed_items():
    fetcher = _fetcher()
    raw = [_raw_news_item("Article 1"), _raw_news_item("Article 2")]
    with patch.object(fetcher, "_ticker", return_value=_mock_ticker(raw)):
        result = fetcher.get_stock_news(symbol="AAPL")
    assert len(result) == 2
    assert result[0].title == "Article 1"


def test_get_stock_news_no_symbol_returns_empty():
    fetcher = _fetcher()
    assert fetcher.get_stock_news(symbol=None) == []
    assert fetcher.get_stock_news() == []


def test_get_stock_news_respects_limit():
    fetcher = _fetcher()
    raw = [_raw_news_item(f"Article {i}") for i in range(20)]
    with patch.object(fetcher, "_ticker", return_value=_mock_ticker(raw)):
        result = fetcher.get_stock_news(symbol="AAPL", limit=5)
    assert len(result) == 5


def test_get_stock_news_yfinance_exception_returns_empty():
    fetcher = _fetcher()
    bad_ticker = MagicMock()
    bad_ticker.news = PropertyMock(side_effect=RuntimeError("yfinance down"))
    type(bad_ticker).news = PropertyMock(side_effect=RuntimeError("yfinance down"))
    with patch.object(fetcher, "_ticker", return_value=bad_ticker):
        result = fetcher.get_stock_news(symbol="AAPL")
    assert result == []


def test_get_stock_news_uses_cache_on_second_call():
    fetcher = _fetcher()
    raw = [_raw_news_item("Cached article")]
    mock_ticker = _mock_ticker(raw)
    with patch.object(fetcher, "_ticker", return_value=mock_ticker) as mock_tick:
        fetcher.get_stock_news(symbol="AAPL")
        fetcher.get_stock_news(symbol="AAPL")
    assert mock_tick.call_count == 1


def test_get_stock_news_filters_items_with_no_title():
    fetcher = _fetcher()
    raw = [
        _raw_news_item("Good Article"),
        {"content": {}},   # no title → should be skipped
        _raw_news_item("Another Good"),
    ]
    with patch.object(fetcher, "_ticker", return_value=_mock_ticker(raw)):
        result = fetcher.get_stock_news(symbol="AAPL")
    assert len(result) == 2


def test_get_news_delegates_to_get_stock_news():
    fetcher = _fetcher()
    raw = [_raw_news_item("Story")]
    with patch.object(fetcher, "_ticker", return_value=_mock_ticker(raw)):
        result = fetcher.get_news("AAPL", limit=10)
    assert len(result) == 1


# ── get_price_batch ───────────────────────────────────────────────────────────

def test_get_price_batch_returns_price_quotes():
    fetcher = _fetcher()
    fi = MagicMock()
    fi.last_price = 1.23
    fi.three_month_average_volume = 5_000_000
    fi.day_high = 1.30
    fi.day_low  = 1.10
    fi.previous_close = 1.20
    mock_ticker = MagicMock()
    mock_ticker.fast_info = fi
    with patch.object(fetcher, "_ticker", return_value=mock_ticker):
        result = fetcher.get_price_batch(["ABAT"])
    assert "ABAT" in result
    assert result["ABAT"].bid_price == pytest.approx(1.23)


def test_get_price_batch_ticker_exception_skips_symbol():
    fetcher = _fetcher()
    bad_ticker = MagicMock()
    type(bad_ticker).fast_info = PropertyMock(side_effect=RuntimeError("error"))
    with patch.object(fetcher, "_ticker", return_value=bad_ticker):
        result = fetcher.get_price_batch(["AAPL"])
    assert result == {}


def test_get_price_batch_empty_symbols():
    fetcher = _fetcher()
    assert fetcher.get_price_batch([]) == {}


# ── get_company_profile ───────────────────────────────────────────────────────

def test_get_company_profile_returns_profile():
    fetcher = _fetcher()
    info = {
        "symbol": "AAPL",
        "longName": "Apple Inc.",
        "exchange": "NMS",
        "currency": "USD",
        "country": "United States",
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "longBusinessSummary": "Apple Inc. designs...",
        "website": "https://apple.com",
        "marketCap": 3_000_000_000_000,
        "beta": 1.2,
        "fullTimeEmployees": 164000,
    }
    mock_ticker = MagicMock()
    mock_ticker.info = info
    with patch.object(fetcher, "_ticker", return_value=mock_ticker):
        profile = fetcher.get_company_profile("AAPL")
    assert profile is not None
    assert profile.symbol == "AAPL"
    assert profile.company_name == "Apple Inc."
    assert profile.market_cap == 3_000_000_000_000
    assert profile.full_time_employees == "164000"


def test_get_company_profile_exception_returns_none():
    fetcher = _fetcher()
    bad_ticker = MagicMock()
    type(bad_ticker).info = PropertyMock(side_effect=RuntimeError("error"))
    with patch.object(fetcher, "_ticker", return_value=bad_ticker):
        result = fetcher.get_company_profile("AAPL")
    assert result is None


# ── get_price_target_consensus ────────────────────────────────────────────────

def test_get_price_target_consensus_returns_targets():
    fetcher = _fetcher()
    targets = MagicMock()
    targets.high   = 250.0
    targets.low    = 150.0
    targets.mean   = 200.0
    targets.median = 195.0
    mock_ticker = MagicMock()
    mock_ticker.analyst_price_targets = targets
    with patch.object(fetcher, "_ticker", return_value=mock_ticker):
        result = fetcher.get_price_target_consensus("AAPL")
    assert result is not None
    assert result.target_high   == pytest.approx(250.0)
    assert result.target_consensus == pytest.approx(200.0)


def test_get_price_target_consensus_none_targets_returns_none():
    fetcher = _fetcher()
    mock_ticker = MagicMock()
    mock_ticker.analyst_price_targets = None
    with patch.object(fetcher, "_ticker", return_value=mock_ticker):
        result = fetcher.get_price_target_consensus("AAPL")
    assert result is None


# ── stubs return empty ────────────────────────────────────────────────────────

def test_get_biggest_gainers_returns_empty():
    assert _fetcher().get_biggest_gainers() == []


def test_get_biggest_losers_returns_empty():
    assert _fetcher().get_biggest_losers() == []


def test_senate_trade_returns_empty():
    assert _fetcher().senate_trade("AAPL") == []


def test_get_earnings_call_transcript_returns_none():
    assert _fetcher().get_earnings_call_transcript("AAPL") is None


# ── get_market_data ───────────────────────────────────────────────────────────

def test_get_market_data_unsupported_timeframe_raises():
    from core.entities.time_frame import TimeFrame
    fetcher = _fetcher()
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = MagicMock(empty=True)
    with patch.object(fetcher, "_ticker", return_value=mock_ticker):
        # All mapped timeframes should not raise
        pass

    # Pass an unknown TimeFrame value that isn't in the map
    class FakeTimeFrame:
        pass
    with pytest.raises((ValueError, AttributeError, KeyError)):
        fetcher.get_market_data(
            "AAPL",
            dt.datetime.now() - dt.timedelta(days=5),
            dt.datetime.now(),
            FakeTimeFrame(),
        )


def test_get_market_data_empty_df_returns_empty():
    import pandas as pd
    from core.entities.time_frame import TimeFrame
    fetcher = _fetcher()
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = pd.DataFrame()
    with patch.object(fetcher, "_ticker", return_value=mock_ticker):
        result = fetcher.get_market_data(
            "AAPL",
            dt.datetime(2026, 6, 1),
            dt.datetime(2026, 6, 5),
            TimeFrame.DAY,
        )
    assert result == []

"""
Tests for NewsService.
All HTTP calls are mocked — no real API keys needed.
"""
from __future__ import annotations

import datetime as dt
import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

import pytest
from core.entities.market_data import StockNews
from services.news_service import NewsService, _title_key, _stamp


# ── Helpers ───────────────────────────────────────────────────────────────────

def _news(title: str, hours_ago: float = 1.0, symbol: str = "AAPL", site: str = "") -> StockNews:
    return StockNews(
        symbol=symbol,
        published_date=dt.datetime.utcnow() - dt.timedelta(hours=hours_ago),
        publisher="Reuters",
        title=title,
        url=f"https://example.com/{title[:10]}",
        text="",
        site=site,
    )


def _svc(fmp=None, finnhub=None, av=None, yf=None, benzinga=None) -> NewsService:
    """Build a NewsService without calling __init__ (no env-var lookups)."""
    svc = NewsService.__new__(NewsService)
    svc.lookback_days     = 2
    svc.last_fetch_stats  = {}

    svc._fmp      = _mock_source(fmp)      if fmp      is not None else None
    svc._finnhub  = _mock_source(finnhub)  if finnhub  is not None else None
    svc._av       = _mock_source(av)       if av       is not None else None
    svc._yf       = _mock_source(yf)       if yf       is not None else None
    svc._benzinga = _mock_source(benzinga) if benzinga is not None else None
    return svc


def _mock_source(items) -> MagicMock:
    m = MagicMock()
    if isinstance(items, Exception):
        m.get_stock_news.side_effect = items
    else:
        m.get_stock_news.return_value = items
    return m


def _svc_none() -> NewsService:
    return _svc()


# ── _title_key ────────────────────────────────────────────────────────────────

def test_title_key_lowercases():
    assert _title_key("AAPL Earnings Beat") == _title_key("aapl earnings beat")


def test_title_key_strips_stopwords():
    assert "the" not in _title_key("The quick brown fox").split()


def test_title_key_strips_punctuation():
    key = _title_key("Breaking: AAPL beats!")
    assert "!" not in key and ":" not in key


def test_title_key_dedup_same_headline():
    assert _title_key("Apple Inc. reports Q3 earnings beat") == \
           _title_key("Apple Inc reports Q3 earnings beat!")


# ── _stamp ────────────────────────────────────────────────────────────────────

def test_stamp_sets_site_when_blank():
    items = [_news("Story A"), _news("Story B")]
    _stamp(items, "FMP")
    assert all(n.site == "FMP" for n in items)


def test_stamp_does_not_overwrite_existing_site():
    item = _news("Story", site="Reuters")
    _stamp([item], "FMP")
    assert item.site == "Reuters"


def test_stamp_no_op_on_empty_list():
    _stamp([], "FMP")  # should not raise


# ── sources property ──────────────────────────────────────────────────────────

def test_sources_empty_when_no_sources():
    assert _svc_none().sources == []


def test_sources_fmp_only():
    assert _svc(fmp=[]).sources == ["FMP"]


def test_sources_fmp_and_finnhub():
    s = _svc(fmp=[], finnhub=[]).sources
    assert "FMP" in s and "Finnhub" in s and "AlphaVantage" not in s


def test_sources_all_three():
    assert set(_svc(fmp=[], finnhub=[], av=[]).sources) == {"FMP", "Finnhub", "AlphaVantage"}


def test_sources_all_four():
    assert set(_svc(fmp=[], finnhub=[], av=[], yf=[]).sources) == {
        "FMP", "Finnhub", "AlphaVantage", "Yahoo"
    }


def test_sources_all_five():
    assert set(_svc(fmp=[], finnhub=[], av=[], yf=[], benzinga=[]).sources) == {
        "FMP", "Finnhub", "AlphaVantage", "Yahoo", "Benzinga"
    }


def test_sources_yahoo_only():
    assert _svc(yf=[]).sources == ["Yahoo"]


def test_sources_benzinga_only():
    assert _svc(benzinga=[]).sources == ["Benzinga"]


# ── get_news — all four sources fire ──────────────────────────────────────────

def test_all_three_sources_are_called():
    svc = _svc(
        fmp=[_news("FMP story")],
        finnhub=[_news("Finnhub story")],
        av=[_news("AV story")],
    )
    result = svc.get_news("AAPL")
    titles = {n.title for n in result}
    assert "FMP story"     in titles
    assert "Finnhub story" in titles
    assert "AV story"      in titles
    assert len(result) == 3


def test_all_four_sources_are_called():
    svc = _svc(
        fmp=[_news("FMP story")],
        finnhub=[_news("Finnhub story")],
        av=[_news("AV story")],
        yf=[_news("Yahoo story")],
    )
    result = svc.get_news("AAPL")
    titles = {n.title for n in result}
    assert "FMP story"     in titles
    assert "Finnhub story" in titles
    assert "AV story"      in titles
    assert "Yahoo story"   in titles
    assert len(result) == 4


def test_all_five_sources_are_called():
    svc = _svc(
        fmp=[_news("FMP story")],
        finnhub=[_news("Finnhub story")],
        av=[_news("AV story")],
        yf=[_news("Yahoo story")],
        benzinga=[_news("Benzinga story")],
    )
    result = svc.get_news("AAPL")
    titles = {n.title for n in result}
    assert "FMP story"      in titles
    assert "Finnhub story"  in titles
    assert "AV story"       in titles
    assert "Yahoo story"    in titles
    assert "Benzinga story" in titles
    assert len(result) == 5


def test_all_three_mocks_get_called_with_symbol():
    svc = _svc(fmp=[], finnhub=[], av=[])
    svc.get_news("TSLA")
    svc._fmp.get_stock_news.assert_called_once_with(symbol="TSLA", limit=100)
    svc._finnhub.get_stock_news.assert_called_once_with(symbol="TSLA", limit=100)
    svc._av.get_stock_news.assert_called_once_with(symbol="TSLA", limit=50)


def test_all_four_mocks_get_called_with_symbol():
    svc = _svc(fmp=[], finnhub=[], av=[], yf=[])
    svc.get_news("ABAT")
    svc._fmp.get_stock_news.assert_called_once_with(symbol="ABAT", limit=100)
    svc._finnhub.get_stock_news.assert_called_once_with(symbol="ABAT", limit=100)
    svc._av.get_stock_news.assert_called_once_with(symbol="ABAT", limit=50)
    svc._yf.get_stock_news.assert_called_once_with(symbol="ABAT", limit=50)


def test_all_five_mocks_get_called_with_symbol():
    svc = _svc(fmp=[], finnhub=[], av=[], yf=[], benzinga=[])
    svc.get_news("NVDA")
    svc._fmp.get_stock_news.assert_called_once_with(symbol="NVDA", limit=100)
    svc._finnhub.get_stock_news.assert_called_once_with(symbol="NVDA", limit=100)
    svc._av.get_stock_news.assert_called_once_with(symbol="NVDA", limit=50)
    svc._yf.get_stock_news.assert_called_once_with(symbol="NVDA", limit=50)
    svc._benzinga.get_stock_news.assert_called_once_with(symbol="NVDA", limit=50)


# ── last_fetch_stats ──────────────────────────────────────────────────────────

def test_last_fetch_stats_populated_after_get_news():
    svc = _svc(
        fmp=[_news("FMP 1"), _news("FMP 2")],
        finnhub=[_news("FH 1")],
        av=[_news("AV 1")],
        yf=[_news("Yahoo 1")],
        benzinga=[_news("Benzinga 1")],
    )
    svc.get_news("AAPL")
    assert svc.last_fetch_stats["FMP"]          == 2
    assert svc.last_fetch_stats["Finnhub"]       == 1
    assert svc.last_fetch_stats["AlphaVantage"]  == 1
    assert svc.last_fetch_stats["Yahoo"]         == 1
    assert svc.last_fetch_stats["Benzinga"]      == 1
    assert svc.last_fetch_stats["merged"]        == 6


def test_last_fetch_stats_counts_dropped_dups():
    same_title = "Apple beats earnings"
    svc = _svc(
        fmp=[_news(same_title)],
        finnhub=[_news(same_title)],   # dup
        av=[_news(same_title)],        # dup
        yf=[_news(same_title)],        # dup
        benzinga=[_news(same_title)],  # dup
    )
    svc.get_news("AAPL")
    assert svc.last_fetch_stats["merged"]        == 1
    assert svc.last_fetch_stats["dropped_dups"]  == 4


def test_last_fetch_stats_zero_for_failed_source():
    svc = _svc(
        fmp=RuntimeError("FMP down"),
        finnhub=[_news("FH story")],
        av=None,
    )
    svc.get_news("AAPL")
    assert svc.last_fetch_stats["FMP"]    == 0
    assert svc.last_fetch_stats["Finnhub"] == 1


# ── get_news — single source ──────────────────────────────────────────────────

def test_get_news_from_fmp_only():
    result = _svc(fmp=[_news("Apple beats", hours_ago=2)]).get_news("AAPL")
    assert len(result) == 1 and result[0].title == "Apple beats"


def test_get_news_from_finnhub_only():
    result = _svc(finnhub=[_news("Fed hikes", hours_ago=3)]).get_news("AAPL")
    assert len(result) == 1 and result[0].title == "Fed hikes"


def test_get_news_from_av_only():
    result = _svc(av=[_news("AV story", hours_ago=1)]).get_news("AAPL")
    assert len(result) == 1 and result[0].title == "AV story"


def test_get_news_returns_empty_when_no_sources():
    assert _svc_none().get_news("AAPL") == []


# ── deduplication ─────────────────────────────────────────────────────────────

def test_dedup_exact_titles_same_source():
    result = _svc(fmp=[_news("Apple beats"), _news("Apple beats")]).get_news("AAPL")
    assert len(result) == 1


def test_dedup_across_fmp_and_finnhub():
    result = _svc(
        fmp=[_news("Apple Q3 beats")],
        finnhub=[_news("Apple Q3 beats")],
    ).get_news("AAPL")
    assert len(result) == 1


def test_dedup_across_all_three():
    same = "Apple Q3 beats estimates"
    result = _svc(
        fmp=[_news(same)],
        finnhub=[_news(same)],
        av=[_news(same)],
    ).get_news("AAPL")
    assert len(result) == 1


def test_dedup_case_insensitive():
    result = _svc(
        fmp=[_news("Apple beats Q3 estimates")],
        finnhub=[_news("APPLE BEATS Q3 ESTIMATES")],
    ).get_news("AAPL")
    assert len(result) == 1


def test_dedup_fmp_wins_over_finnhub_on_dup():
    """FMP is fetched first so its version of a duplicate is kept."""
    fmp_item = _news("Shared story", site="fmp-publisher")
    fh_item  = _news("Shared story", site="finnhub-publisher")
    result   = _svc(fmp=[fmp_item], finnhub=[fh_item]).get_news("AAPL")
    assert len(result) == 1
    assert result[0].site == "fmp-publisher"


# ── date filtering ────────────────────────────────────────────────────────────

def test_filters_old_items():
    result = _svc(
        fmp=[_news("Old", hours_ago=72), _news("Fresh", hours_ago=1)]
    ).get_news("AAPL", lookback_days=2)
    assert len(result) == 1 and result[0].title == "Fresh"


def test_lookback_days_1_excludes_yesterday():
    result = _svc(
        fmp=[_news("Today", hours_ago=2), _news("Yesterday", hours_ago=26)]
    ).get_news("AAPL", lookback_days=1)
    assert len(result) == 1 and result[0].title == "Today"


# ── sort order ────────────────────────────────────────────────────────────────

def test_sorted_newest_first():
    result = _svc(fmp=[
        _news("Old",    hours_ago=5),
        _news("Newest", hours_ago=1),
        _news("Middle", hours_ago=3),
    ]).get_news("AAPL")
    assert result[0].title == "Newest"
    assert result[-1].title == "Old"


# ── error resilience ──────────────────────────────────────────────────────────

def test_fmp_failure_does_not_block_finnhub():
    result = _svc(
        fmp=RuntimeError("FMP down"),
        finnhub=[_news("FH story")],
    ).get_news("AAPL")
    assert len(result) == 1 and result[0].title == "FH story"


def test_finnhub_failure_does_not_block_fmp():
    result = _svc(
        fmp=[_news("FMP story")],
        finnhub=RuntimeError("Finnhub down"),
    ).get_news("AAPL")
    assert len(result) == 1 and result[0].title == "FMP story"


def test_av_failure_does_not_block_others():
    result = _svc(
        fmp=[_news("FMP story")],
        finnhub=[_news("FH story")],
        av=RuntimeError("AV down"),
    ).get_news("AAPL")
    assert {n.title for n in result} == {"FMP story", "FH story"}


def test_yahoo_failure_does_not_block_others():
    result = _svc(
        fmp=[_news("FMP story")],
        finnhub=[_news("FH story")],
        yf=RuntimeError("Yahoo down"),
    ).get_news("AAPL")
    assert {n.title for n in result} == {"FMP story", "FH story"}


def test_benzinga_failure_does_not_block_others():
    result = _svc(
        fmp=[_news("FMP story")],
        finnhub=[_news("FH story")],
        benzinga=RuntimeError("Benzinga down"),
    ).get_news("AAPL")
    assert {n.title for n in result} == {"FMP story", "FH story"}


def test_all_sources_fail_returns_empty():
    result = _svc(
        fmp=RuntimeError("down"),
        finnhub=RuntimeError("down"),
        av=RuntimeError("down"),
        yf=RuntimeError("down"),
        benzinga=RuntimeError("down"),
    ).get_news("AAPL")
    assert result == []


def test_get_news_from_yahoo_only():
    result = _svc(yf=[_news("Yahoo exclusive", hours_ago=1)]).get_news("AAPL")
    assert len(result) == 1 and result[0].title == "Yahoo exclusive"


def test_yahoo_dedup_with_fmp():
    """Yahoo article with same title as FMP article is deduped; FMP wins."""
    fmp_item = _news("Shared article", site="fmp-site")
    yf_item  = _news("Shared article", site="yahoo-site")
    result   = _svc(fmp=[fmp_item], yf=[yf_item]).get_news("AAPL")
    assert len(result) == 1
    assert result[0].site == "fmp-site"


def test_get_news_from_benzinga_only():
    result = _svc(benzinga=[_news("Benzinga exclusive", hours_ago=1)]).get_news("AAPL")
    assert len(result) == 1 and result[0].title == "Benzinga exclusive"


def test_benzinga_dedup_with_fmp():
    """Benzinga article with same title as FMP is deduped; FMP wins."""
    fmp_item = _news("Shared article", site="fmp-site")
    bz_item  = _news("Shared article", site="benzinga-site")
    result   = _svc(fmp=[fmp_item], benzinga=[bz_item]).get_news("AAPL")
    assert len(result) == 1
    assert result[0].site == "fmp-site"


# ── get_news_multi ────────────────────────────────────────────────────────────

def test_get_news_multi_returns_per_symbol_dict():
    fmp_mock = MagicMock()
    fmp_mock.get_stock_news.side_effect = lambda symbol, limit: [_news(f"{symbol} story", symbol=symbol)]
    svc = _svc()
    svc._fmp = fmp_mock
    result = svc.get_news_multi(["AAPL", "TSLA"])
    assert set(result.keys()) == {"AAPL", "TSLA"}
    assert result["AAPL"][0].symbol == "AAPL"
    assert result["TSLA"][0].symbol == "TSLA"


def test_get_news_multi_empty_symbols():
    assert _svc_none().get_news_multi([]) == {}


# ── init — API key detection ──────────────────────────────────────────────────

def test_init_no_fmp_key_disables_fmp(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.delenv("FINANCIAL_MODELING_PREP_API_KEY", raising=False)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("AV_API_KEY", raising=False)
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    svc = NewsService()
    assert svc._fmp is None


def test_init_fmp_key_builds_fmp(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("AV_API_KEY", raising=False)
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    svc = NewsService()
    assert svc._fmp is not None


def test_init_no_finnhub_key_disables_finnhub(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    svc = NewsService()
    assert svc._finnhub is None


def test_init_no_av_key_disables_av(monkeypatch):
    monkeypatch.delenv("AV_API_KEY", raising=False)
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    svc = NewsService()
    assert svc._av is None


def test_init_no_benzinga_key_disables_benzinga(monkeypatch):
    monkeypatch.delenv("BENZINGA_API_KEY", raising=False)
    monkeypatch.delenv("BENZIGA_API_KEY", raising=False)
    svc = NewsService()
    assert svc._benzinga is None


def test_init_benzinga_key_builds_benzinga(monkeypatch):
    monkeypatch.setenv("BENZINGA_API_KEY", "test-key")
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.delenv("FINANCIAL_MODELING_PREP_API_KEY", raising=False)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("AV_API_KEY", raising=False)
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    svc = NewsService()
    assert svc._benzinga is not None


def test_init_benziga_typo_key_builds_benzinga(monkeypatch):
    """The typo variant BENZIGA_API_KEY (missing N) should also work."""
    monkeypatch.delenv("BENZINGA_API_KEY", raising=False)
    monkeypatch.setenv("BENZIGA_API_KEY", "test-key")
    svc = NewsService()
    assert svc._benzinga is not None


def test_init_yahoo_always_on(monkeypatch):
    """Yahoo Finance requires no API key — it should always be active."""
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.delenv("FINANCIAL_MODELING_PREP_API_KEY", raising=False)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("AV_API_KEY", raising=False)
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    svc = NewsService()
    assert svc._yf is not None
    assert "Yahoo" in svc.sources

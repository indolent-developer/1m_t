"""
services.news_service

Aggregates stock news from FMP, Finnhub, Alpha Vantage, Yahoo Finance, and
Benzinga for a given ticker within a rolling lookback window.

Sources:
    FMP            — FmpDataFetcher.get_stock_news()
    Finnhub        — FinnhubDataFetcher.get_stock_news()
    Alpha Vantage  — AlphaVantageDataFetcher.get_stock_news()
    Yahoo Finance  — YahooFinanceDataFetcher.get_stock_news()  [always on]
    Benzinga       — BenzingaDataFetcher.get_stock_news()

Each source is tried independently; a failure in one does not affect the
others. Results are merged, deduplicated by normalised title, filtered to
the lookback window, and returned sorted newest-first.

Usage:
    svc  = NewsService()
    news = svc.get_news("AAPL")                  # last 2 days, all sources
    news = svc.get_news("NVDA", lookback_days=1)  # today only
    bulk = svc.get_news_multi(["AAPL", "TSLA"])   # {symbol: [StockNews]}
    print(svc.last_fetch_stats)
    # {'FMP': 12, 'Finnhub': 8, 'AlphaVantage': 6, 'Yahoo': 10, 'Benzinga': 5,
    #  'merged': 30, 'dropped_dups': 11}

Environment:
    FMP_API_KEY              — enables FMP source
    FINNHUB_API_KEY          — enables Finnhub source
    AV_API_KEY  or
    ALPHA_VANTAGE_API_KEY    — enables Alpha Vantage source
    BENZINGA_API_KEY  or
    BENZIGA_API_KEY (typo, accepted too) — enables Benzinga source
    Yahoo Finance is always on (no API key required).
"""
from __future__ import annotations

import datetime as dt
from core.utils.log_helper import getLogger
import os
import re
from typing import Dict, List, Optional

from core.entities.market_data import StockNews

logger = getLogger(__name__)

_STOPWORDS = frozenset({"a", "an", "the", "in", "of", "for", "to", "and", "on", "at"})


def _title_key(title: str) -> str:
    """Normalise headline for deduplication: lowercase, strip punctuation, drop stopwords."""
    words = re.sub(r"[^a-z0-9 ]", "", title.lower()).split()
    return " ".join(w for w in words if w not in _STOPWORDS)


class NewsService:
    """
    Fetch and aggregate recent news for one or more tickers.

    Sources are built from environment variables at init time.
    Any source missing its API key or failing to import is silently
    disabled — the rest continue working.

    After each get_news() call, last_fetch_stats holds a breakdown:
        {'FMP': N, 'Finnhub': N, 'AlphaVantage': N, 'Yahoo': N, 'Benzinga': N,
         'merged': N, 'dropped_dups': N}
    """

    def __init__(self, lookback_days: int = 2) -> None:
        self.lookback_days    = lookback_days
        self.last_fetch_stats: Dict[str, int] = {}

        self._fmp      = self._build_fmp()
        self._finnhub  = self._build_finnhub()
        self._av       = self._build_av()
        self._yf       = self._build_yf()
        self._benzinga = self._build_benzinga()

        logger.info("NewsService: active sources — %s", ", ".join(self.sources))

    # ── Source factories ──────────────────────────────────────────────────────

    def _build_fmp(self):
        key = os.environ.get("FMP_API_KEY") or os.environ.get("FINANCIAL_MODELING_PREP_API_KEY")
        if not key:
            return None
        try:
            from data_fetchers.financial_modelling_prep_data_fetcher import FmpDataFetcher
            return FmpDataFetcher({"api_key": key})
        except Exception as e:
            logger.warning("FMP source init failed: %s", e)
            return None

    def _build_finnhub(self):
        key = os.environ.get("FINNHUB_API_KEY")
        if not key:
            return None
        try:
            from data_fetchers.finnhub_data_fetcher import FinnhubDataFetcher
            return FinnhubDataFetcher({"api_key": key})
        except Exception as e:
            logger.warning("Finnhub source init failed: %s", e)
            return None

    def _build_av(self):
        key = os.environ.get("AV_API_KEY") or os.environ.get("ALPHA_VANTAGE_API_KEY")
        if not key:
            return None
        try:
            from data_fetchers.alpha_vantage_data_fetcher import AlphaVantageDataFetcher
            return AlphaVantageDataFetcher({"api_key": key})
        except Exception as e:
            logger.warning("Alpha Vantage source init failed: %s", e)
            return None

    def _build_yf(self):
        try:
            from data_fetchers.yahoo_finance_data_fetcher import YahooFinanceDataFetcher
            return YahooFinanceDataFetcher()
        except Exception as e:
            logger.warning("Yahoo Finance source init failed: %s", e)
            return None

    def _build_benzinga(self):
        key = os.environ.get("BENZINGA_API_KEY") or os.environ.get("BENZIGA_API_KEY")
        if not key:
            return None
        try:
            from data_fetchers.benzinga_data_fetcher import BenzingaDataFetcher
            return BenzingaDataFetcher({"api_key": key})
        except Exception as e:
            logger.warning("Benzinga source init failed: %s", e)
            return None

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def sources(self) -> List[str]:
        """Names of currently active sources."""
        active = []
        if self._fmp:
            active.append("FMP")
        if self._finnhub:
            active.append("Finnhub")
        if self._av:
            active.append("AlphaVantage")
        if self._yf:
            active.append("Yahoo")
        if self._benzinga:
            active.append("Benzinga")
        return active

    def get_news(
        self,
        symbol: str,
        lookback_days: Optional[int] = None,
    ) -> List[StockNews]:
        """
        Return news for *symbol* merged from all active sources.
        Deduped by title, filtered to last `lookback_days` calendar days,
        sorted newest-first.
        """
        days  = lookback_days if lookback_days is not None else self.lookback_days
        since = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=days)

        raw_counts, items = self._fetch_all(symbol)
        result = self._process(items, since)

        dropped = sum(raw_counts.values()) - len(result)
        self.last_fetch_stats = {**raw_counts, "merged": len(result), "dropped_dups": dropped}

        logger.info(
            "[NewsService] %s: FMP=%d Finnhub=%d AV=%d Yahoo=%d Benzinga=%d → merged=%d (-%d dups)",
            symbol,
            raw_counts.get("FMP", 0),
            raw_counts.get("Finnhub", 0),
            raw_counts.get("AlphaVantage", 0),
            raw_counts.get("Yahoo", 0),
            raw_counts.get("Benzinga", 0),
            len(result),
            dropped,
        )

        return result

    def get_news_multi(
        self,
        symbols: List[str],
        lookback_days: Optional[int] = None,
    ) -> Dict[str, List[StockNews]]:
        """Fetch news for multiple tickers. Returns {symbol: [StockNews, ...]}."""
        return {sym: self.get_news(sym, lookback_days=lookback_days) for sym in symbols}

    # ── Internals ─────────────────────────────────────────────────────────────

    def _fetch_all(self, symbol: str) -> tuple[Dict[str, int], List[StockNews]]:
        counts: Dict[str, int] = {}
        items:  List[StockNews] = []

        if self._fmp:
            try:
                fetched = self._fmp.get_stock_news(symbol=symbol, limit=100)
                _stamp(fetched, "FMP")
                counts["FMP"] = len(fetched)
                items.extend(fetched)
                logger.info("[FMP]          %3d articles for %s", len(fetched), symbol)
            except Exception as e:
                logger.warning("[FMP] fetch failed for %s: %s", symbol, e)
                counts["FMP"] = 0

        if self._finnhub:
            try:
                fetched = self._finnhub.get_stock_news(symbol=symbol, limit=100)
                _stamp(fetched, "Finnhub")
                counts["Finnhub"] = len(fetched)
                items.extend(fetched)
                logger.info("[Finnhub]      %3d articles for %s", len(fetched), symbol)
            except Exception as e:
                logger.warning("[Finnhub] fetch failed for %s: %s", symbol, e)
                counts["Finnhub"] = 0

        if self._av:
            try:
                fetched = self._av.get_stock_news(symbol=symbol, limit=50)
                _stamp(fetched, "AlphaVantage")
                counts["AlphaVantage"] = len(fetched)
                items.extend(fetched)
                logger.info("[AlphaVantage] %3d articles for %s", len(fetched), symbol)
            except Exception as e:
                logger.warning("[AlphaVantage] fetch failed for %s: %s", symbol, e)
                counts["AlphaVantage"] = 0

        if self._yf:
            try:
                fetched = self._yf.get_stock_news(symbol=symbol, limit=50)
                _stamp(fetched, "Yahoo")
                counts["Yahoo"] = len(fetched)
                items.extend(fetched)
                logger.info("[Yahoo]        %3d articles for %s", len(fetched), symbol)
            except Exception as e:
                logger.warning("[Yahoo] fetch failed for %s: %s", symbol, e)
                counts["Yahoo"] = 0

        if self._benzinga:
            try:
                fetched = self._benzinga.get_stock_news(symbol=symbol, limit=50)
                _stamp(fetched, "Benzinga")
                counts["Benzinga"] = len(fetched)
                items.extend(fetched)
                logger.info("[Benzinga]     %3d articles for %s", len(fetched), symbol)
            except Exception as e:
                logger.warning("[Benzinga] fetch failed for %s: %s", symbol, e)
                counts["Benzinga"] = 0

        return counts, items

    def _process(self, items: List[StockNews], since: dt.datetime) -> List[StockNews]:
        # Filter to lookback window.
        # Fetchers produce mixed datetime types (FMP=pytz-aware ET, Finnhub=naive local,
        # Yahoo=naive UTC); strip tzinfo to naive UTC so comparison never raises TypeError.
        def _to_naive_utc(d: dt.datetime) -> dt.datetime:
            if d.tzinfo is not None:
                return d.astimezone(dt.timezone.utc).replace(tzinfo=None)
            return d

        recent = [n for n in items if _to_naive_utc(n.published_date) >= since]

        # Deduplicate by normalised title — first occurrence wins (FMP → Finnhub → AV priority)
        seen:   set[str]        = set()
        unique: List[StockNews] = []
        for n in recent:
            key = _title_key(n.title)
            if key and key not in seen:
                seen.add(key)
                unique.append(n)

        # Newest-first
        unique.sort(key=lambda n: _to_naive_utc(n.published_date), reverse=True)
        return unique


def _stamp(items: List[StockNews], source_name: str) -> None:
    """Tag each article's `site` field with the source name when site is blank."""
    for item in items:
        if not item.site:
            item.site = source_name

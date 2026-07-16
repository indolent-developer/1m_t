"""
services.news_monitor_service — NewsMonitorService

Live news aggregator. Polls multiple sources on independent cadences,
stamps each article with fetched_at + news_source, deduplicates across
sources, and emits NewsEvent.NEWS_PUBLISHED on the bus for every new article
that passes the candidate filter.

Sources
-------
  FMP      — /stable/news/stock-latest (global feed, no symbol param needed)
             polled every 60 s; one call covers all symbols.
  Finnhub  — /company-news?symbol=X (per-symbol)
             polled every 45 s across all watched symbols.
  Yahoo    — yfinance Ticker.news (per-symbol)
             polled every 90 s across all watched symbols.

Candidate filter (applied once per symbol, profile cached 4 h)
--------------------------------------------------------------
  • Symbol not in data/indp_ignore.json
  • market_cap  > 300 000 000
  • average_volume > 1 000 000

Deduplication
-------------
  L1: in-memory dict[dedup_key → source_name]
  L2: Redis (category="news_dedup", TTL 86400 s)
  Key: "{SYMBOL}:{normalised_title}:{publish_date}"

When the same article arrives from a second source, the latency of both
is logged so you can compare source speeds — no alert is re-emitted.

Freshness guard
---------------
  Articles older than `fresh_window_minutes` (default 120) are silently
  dropped regardless of source.  This prevents a service restart from
  re-alerting on articles already processed today.

Usage
-----
    monitor = NewsMonitorService(bus, fmp, finnhub=fh, yahoo=yf, cache=redis)
    await monitor.start()
    monitor.add_symbol("AAPL")
    ...
    await monitor.stop()
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set

_NEWS_DIR = Path(__file__).parents[2] / "data" / "news"

from core.adapters.event_bus import IEventBus
from core.entities.market_data import StockNews
from core.entities.news_event import NewsEvent
from core.utils.log_helper import getLogger

if TYPE_CHECKING:
    from data_fetchers.financial_modelling_prep_data_fetcher import FmpDataFetcher
    from infrastructure.cache.redis_cache import RedisCache

logger = getLogger(__name__)

_STOPWORDS = frozenset({"a", "an", "the", "in", "of", "for", "to", "and", "on", "at"})
_REDIS_DEDUP_CATEGORY = "news_dedup"
_MIN_MARKET_CAP   = 300_000_000
_MIN_AVG_VOLUME   = 1_000_000
_IGNORE_REFRESH_S = 300        # 5 minutes


def _normalize_title(title: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", "", title.lower()).split()
    return " ".join(w for w in words if w not in _STOPWORDS)


def _dedup_key(item: StockNews) -> str:
    pub = item.published_date
    return f"{item.symbol.upper()}:{_normalize_title(item.title)}:{pub.date()}"


def _load_ignore() -> Set[str]:
    """Load the symbol ignore list from data/indp_ignore.json (same file as SymbolAutoWatcher)."""
    try:
        import json
        from pathlib import Path
        path = Path(__file__).parents[2] / "data" / "indp_ignore.json"
        if path.exists():
            return {s.upper() for s in json.loads(path.read_text())}
    except Exception:
        pass
    return set()


class NewsMonitorService:
    """
    Poll FMP (global), Finnhub, and Yahoo for news; deduplicate; emit on bus.

    Parameters
    ----------
    bus              : event bus — emits StockNews as NewsEvent.NEWS_PUBLISHED
    fmp              : FmpDataFetcher — required (provides the global feed + profiles)
    finnhub          : FinnhubDataFetcher — optional
    yahoo            : YahooFinanceDataFetcher — optional
    cache            : RedisCache — optional (enables crash-safe dedup)
    poll_fmp_seconds : cadence for FMP global feed (default 60 s)
    poll_finnhub_seconds : cadence per symbol for Finnhub (default 45 s)
    poll_yahoo_seconds   : cadence per symbol for Yahoo (default 90 s)
    fresh_window_minutes : max age of articles to process (default 120 min)
    """

    def __init__(
        self,
        bus: IEventBus,
        fmp: "FmpDataFetcher",
        finnhub=None,
        yahoo=None,
        cache: Optional["RedisCache"] = None,
        poll_fmp_seconds: int = 60,
        poll_finnhub_seconds: int = 45,
        poll_yahoo_seconds: int = 90,
        fresh_window_minutes: int = 120,
        watchlist: Optional[Set[str]] = None,
    ) -> None:
        self._bus     = bus
        self._fmp     = fmp
        self._finnhub = finnhub
        self._yahoo   = yahoo
        self._cache   = cache

        self._poll_fmp     = poll_fmp_seconds
        self._poll_finnhub = poll_finnhub_seconds
        self._poll_yahoo   = poll_yahoo_seconds
        self._fresh_minutes = fresh_window_minutes

        # Use the caller's set by reference so add_symbol/remove_symbol on the
        # watcher are immediately visible to the Finnhub/Yahoo poll loops.
        self._watched: Set[str] = watchlist if watchlist is not None else set()

        # L1 dedup: key → name of first source that reported it
        self._seen: Dict[str, str] = {}
        # L1 dedup: key → fetched_at of first report (for latency comparison logging)
        self._seen_at: Dict[str, datetime] = {}

        # Candidate filter: FundamentalsService uses /stable/profile (7-day file cache)
        from services.fundamentals_service import FundamentalsService
        self._fundamentals = FundamentalsService()

        # In-memory pass/fail cache so we don't re-check on every article
        self._profile_cache: Dict[str, bool] = {}

        # Ignore list
        self._ignore: Set[str] = set()
        self._ignore_loaded_at: Optional[datetime] = None

        self._non_us_warned: Set[str] = set()   # symbols already warned about once
        self._tasks: List[asyncio.Task] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._fmp_loop(),     name="news_fmp"),
            asyncio.create_task(self._finnhub_loop(), name="news_finnhub"),
            asyncio.create_task(self._yahoo_loop(),   name="news_yahoo"),
        ]
        logger.info(
            "[NewsMonitorService] started — FMP=%ds Finnhub=%ds Yahoo=%ds fresh=%dmin",
            self._poll_fmp, self._poll_finnhub, self._poll_yahoo, self._fresh_minutes,
        )

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("[NewsMonitorService] stopped")

    # ── Symbol management ─────────────────────────────────────────────────────

    def add_symbol(self, symbol: str) -> None:
        self._watched.add(symbol.upper())

    def remove_symbol(self, symbol: str) -> None:
        self._watched.discard(symbol.upper())

    # ── Poll loops ────────────────────────────────────────────────────────────

    async def _fmp_loop(self) -> None:
        while True:
            try:
                await self._poll_fmp_global()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[NewsMonitorService] FMP global poll error")
            # Also poll FMP per-symbol for watched symbols on the same cadence
            for symbol in list(self._watched):
                if "." in symbol:   # skip non-US symbols
                    continue
                try:
                    await self._poll_symbol(symbol, self._fmp, "FMP")
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.warning("[NewsMonitorService] FMP per-symbol poll error for %s", symbol)
            await asyncio.sleep(self._poll_fmp)

    async def _finnhub_loop(self) -> None:
        while True:
            if self._finnhub is None:
                await asyncio.sleep(self._poll_finnhub)
                continue
            for symbol in list(self._watched):
                if "." in symbol:
                    if symbol not in self._non_us_warned:
                        logger.warning("[NewsMonitorService] skipping %s — non-US symbol (Finnhub/Yahoo/FMP only support US tickers)", symbol)
                        self._non_us_warned.add(symbol)
                    continue
                try:
                    await self._poll_symbol(symbol, self._finnhub, "Finnhub")
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.warning("[NewsMonitorService] Finnhub poll error for %s", symbol)
            await asyncio.sleep(self._poll_finnhub)

    async def _yahoo_loop(self) -> None:
        while True:
            if self._yahoo is None:
                await asyncio.sleep(self._poll_yahoo)
                continue
            for symbol in list(self._watched):
                if "." in symbol:   # skip non-US symbols
                    continue
                try:
                    await self._poll_symbol(symbol, self._yahoo, "Yahoo")
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.warning("[NewsMonitorService] Yahoo poll error for %s", symbol)
            await asyncio.sleep(self._poll_yahoo)

    # ── Fetch helpers ─────────────────────────────────────────────────────────

    async def _poll_fmp_global(self) -> None:
        fetched_at = datetime.now(timezone.utc)
        loop = asyncio.get_event_loop()
        items: List[StockNews] = await loop.run_in_executor(
            None, lambda: self._fmp.get_stock_news(symbol=None, limit=50)
        )
        if not items:
            logger.debug("[NewsMonitorService] FMP global: no items returned")
            return
        for item in items:
            item.fetched_at  = fetched_at
            item.news_source = "FMP"
        # Save ALL fetched articles for historical latency analysis (before any filtering)
        self._append_batch(items)
        logger.debug("[NewsMonitorService] FMP global: saved %d articles for latency history", len(items))
        # Pass to process pipeline — freshness/candidate/dedup gate controls bus emit
        for item in items:
            await self._process(item)

    async def _poll_symbol(self, symbol: str, fetcher, source: str) -> None:
        fetched_at = datetime.now(timezone.utc)
        loop = asyncio.get_event_loop()
        items: List[StockNews] = await loop.run_in_executor(
            None, lambda: fetcher.get_stock_news(symbol=symbol, limit=50)
        )
        for item in (items or []):
            item.fetched_at  = fetched_at
            item.news_source = source
            await self._process(item)

    # ── Processing pipeline ───────────────────────────────────────────────────

    async def _process(self, item: StockNews) -> None:
        symbol = (item.symbol or "").upper().strip()
        if not symbol:
            return

        if not self._is_fresh(item):
            return

        if not await self._is_candidate(symbol):
            return

        key = _dedup_key(item)
        if not await self._is_new(key, item):
            return

        logger.info(
            "[NewsMonitorService] NEW  %-6s  [%s]  latency=%.0fs  '%s'",
            symbol, item.news_source, item.latency_seconds or 0, item.title[:60],
        )
        await self._bus.emit(item)
        # Per-symbol sources (Finnhub/Yahoo/FMP-per-symbol) save here;
        # FMP global saves the whole batch at fetch time (before filtering).
        if item.news_source != "FMP":
            self._append_batch([item])

    def _append_batch(self, items: List[StockNews]) -> None:
        try:
            import pytz as _pytz
            _ET = _pytz.timezone("America/New_York")
            _NEWS_DIR.mkdir(parents=True, exist_ok=True)
            by_date: Dict[str, List[dict]] = {}
            for item in items:
                pub = item.published_date
                if pub.tzinfo is None:
                    pub = _ET.localize(pub)
                date_str = pub.astimezone(timezone.utc).strftime("%Y-%m-%d")
                record = {
                    "symbol":          item.symbol,
                    "title":           item.title,
                    "publisher":       item.publisher,
                    "url":             item.url,
                    "published_at":    pub.isoformat(),
                    "fetched_at":      item.fetched_at.isoformat() if item.fetched_at else None,
                    "source":          item.news_source,
                    "latency_seconds": item.latency_seconds,
                }
                by_date.setdefault(date_str, []).append(record)
            for date_str, records in by_date.items():
                path = _NEWS_DIR / f"{date_str}.jsonl"
                with path.open("a", encoding="utf-8") as f:
                    for rec in records:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug("[NewsMonitorService] news save failed: %s", exc)

    def _is_fresh(self, item: StockNews) -> bool:
        import pytz as _pytz
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=self._fresh_minutes)
        pub = item.published_date
        if pub.tzinfo is None:
            pub = _pytz.timezone("America/New_York").localize(pub)
        return pub >= cutoff

    async def _is_candidate(self, symbol: str) -> bool:
        self._maybe_refresh_ignore()
        if symbol in self._ignore:
            return False

        # In-memory short-circuit (FundamentalsService owns the 7-day TTL)
        if symbol in self._profile_cache:
            return self._profile_cache[symbol]

        # FundamentalsService hits /stable/profile (free tier) with 7-day file cache.
        # get_profile() already swallows its own fetch errors and returns None, but
        # don't rely on that internal guarantee — treat a raised exception the same
        # as a None profile rather than letting it propagate and drop the article.
        try:
            profile = await self._fundamentals.get_profile(symbol)
        except Exception as exc:
            logger.warning("[NewsMonitorService] %s — profile fetch raised: %s", symbol, exc)
            profile = None

        if profile is None:
            logger.warning("[NewsMonitorService] %s — profile unavailable (stable/profile returned None)", symbol)
            self._profile_cache[symbol] = False
        elif not profile.market_cap or profile.market_cap <= _MIN_MARKET_CAP:
            logger.debug("[NewsMonitorService] %s filtered out — mcap=%.0fM below %.0fM min",
                         symbol, (profile.market_cap or 0) / 1e6, _MIN_MARKET_CAP / 1e6)
            self._profile_cache[symbol] = False
        elif profile.average_volume is not None and profile.average_volume <= _MIN_AVG_VOLUME:
            logger.debug("[NewsMonitorService] %s filtered out — avgvol=%.0fK below %.0fK min",
                         symbol, profile.average_volume / 1e3, _MIN_AVG_VOLUME / 1e3)
            self._profile_cache[symbol] = False
        else:
            self._profile_cache[symbol] = True
            logger.debug(
                "[NewsMonitorService] %s passes filter — mcap=%.0fM avgvol=%s",
                symbol, profile.market_cap / 1e6,
                f"{profile.average_volume/1e3:.0f}K" if profile.average_volume else "n/a",
            )

        return self._profile_cache[symbol]

    async def _is_new(self, key: str, item: StockNews) -> bool:
        """Return True if this article hasn't been seen before; mark it as seen."""
        if key in self._seen:
            first_source = self._seen[key]
            first_at     = self._seen_at.get(key)
            their_latency = item.latency_seconds or 0
            our_latency   = (
                (item.fetched_at - first_at).total_seconds() if first_at and item.fetched_at else 0
            )
            logger.debug(
                "[NewsMonitorService] dup %-6s [%s] — first seen via %s %.0fs ago  "
                "this_latency=%.0fs  first_latency=%.0fs",
                item.symbol, item.news_source, first_source, our_latency,
                their_latency,
                (self._seen_at.get(key) and item.fetched_at and item.latency_seconds) or 0,
            )
            return False

        # Check Redis (crash-safe: survives restarts within the 24 h window)
        if self._cache:
            try:
                existing = await self._cache.load(key, category=_REDIS_DEDUP_CATEGORY)
                if existing is not None:
                    self._seen[key]    = existing.get("source", "unknown")
                    self._seen_at[key] = item.fetched_at or datetime.now(timezone.utc)
                    return False
                await self._cache.save(
                    key,
                    {"source": item.news_source, "at": (item.fetched_at or datetime.now(timezone.utc)).isoformat()},
                    category=_REDIS_DEDUP_CATEGORY,
                    ttl=86400,
                )
            except Exception as exc:
                logger.debug("[NewsMonitorService] Redis dedup error: %s", exc)

        self._seen[key]    = item.news_source
        self._seen_at[key] = item.fetched_at or datetime.now(timezone.utc)
        return True

    def _maybe_refresh_ignore(self) -> None:
        now = datetime.now(timezone.utc)
        if (
            self._ignore_loaded_at is None
            or (now - self._ignore_loaded_at).total_seconds() > _IGNORE_REFRESH_S
        ):
            self._ignore = _load_ignore()
            self._ignore_loaded_at = now

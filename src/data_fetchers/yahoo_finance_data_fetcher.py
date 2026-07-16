"""
data_fetchers.yahoo_finance_data_fetcher

Financial data via the yfinance library (Yahoo Finance).

No API key required — yfinance scrapes Yahoo Finance directly.

Implemented:
    Ticker.news              → get_stock_news / get_news
    Ticker.history()         → get_market_data
    Ticker.fast_info         → get_price_batch / get_price
    Ticker.info              → get_company_overview / get_company_profile
    Ticker.analyst_price_targets → get_price_target_consensus
    Ticker.upgrades_downgrades   → get_upgrades_downgrades
    Ticker.calendar          → get_calendar (earnings)
    Ticker.get_earnings_history → partial get_earnings_call_transcript stub

Not available in Yahoo Finance:
    senate_trade             → returns []
    get_biggest_gainers/losers → returns []
    get_earnings_call_transcript → returns None (no transcripts)
"""
from __future__ import annotations

import datetime as dt
from core.utils.log_helper import getLogger
from typing import Any, Dict, List, Optional

from core.entities.analyst_data import Grade, PriceTargetConsensus
from core.entities.calendar_events import CalendarEventType
from core.entities.company_profile import CompanyProfile
from core.entities.earnings import EarningsCallTranscript
from core.entities.market_data import PriceQuote, StockNews
from core.entities.ohlc import OHLCData
from core.entities.time_frame import TimeFrame
from infrastructure.cache.memory_cache import MemoryCache

from .data_fetcher_base import DataFetcherBase

logger = getLogger(__name__)

ONE_DAY_SECONDS = 86_400

_INTERVAL_MAP: dict[TimeFrame, str] = {
    TimeFrame.MINUTE_1:  "1m",
    TimeFrame.MINUTE_5:  "5m",
    TimeFrame.MINUTE_15: "15m",
    TimeFrame.MINUTE_30: "30m",
    TimeFrame.HOUR_1:    "1h",
    TimeFrame.HOUR_4:    "4h",
    TimeFrame.DAY:       "1d",
    TimeFrame.WEEK:      "1wk",
    TimeFrame.MONTH:     "1mo",
}


class YahooFinanceDataFetcher(DataFetcherBase):
    """
    Fetches financial data from Yahoo Finance via the yfinance library.
    No API key required.

    Config keys (all optional):
        timeout   (int, default 10)  — request timeout in seconds
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        cfg = config or {}
        self._timeout = int(cfg.get("timeout", 10))
        self._cache   = MemoryCache()

        try:
            import yfinance  # noqa: F401
        except ImportError as e:
            raise ImportError("yfinance is required: uv add yfinance") from e

    def _ticker(self, symbol: str):
        import yfinance as yf
        return yf.Ticker(symbol)

    # ── News ──────────────────────────────────────────────────────────────────

    def get_stock_news(
        self,
        symbol: Optional[str] = None,
        page: int = 0,
        limit: int = 50,
    ) -> List[StockNews]:
        if not symbol:
            return []

        cache_key = f"news:{symbol}"
        cached = self._cache.load(cache_key, category="yf")
        if cached is not None:
            return cached[:limit]

        try:
            raw = self._ticker(symbol).news or []
        except Exception as e:
            logger.warning("[Yahoo] news fetch failed for %s: %s", symbol, e)
            return []

        items: List[StockNews] = []
        for item in raw:
            try:
                n = self._parse_news_item(item, symbol)
                if n:
                    items.append(n)
            except Exception:
                pass

        self._cache.save(cache_key, items, category="yf", metadata={"ttl": 300})
        return items[:limit]

    def _parse_news_item(self, item: dict, symbol: str) -> Optional[StockNews]:
        content = item.get("content", {})

        # Title
        title = content.get("title") or item.get("title", "")
        if not title:
            return None

        # Published date
        pub_str = content.get("pubDate", "")
        published: dt.datetime
        if pub_str:
            try:
                published = dt.datetime.strptime(pub_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
            except ValueError:
                published = dt.datetime.now(dt.timezone.utc)
        else:
            ppt = item.get("providerPublishTime", 0)
            published = (
                dt.datetime.fromtimestamp(int(ppt), tz=dt.timezone.utc)
                if ppt else dt.datetime.now(dt.timezone.utc)
            )

        # Publisher
        prov = content.get("provider", {})
        publisher = (
            prov.get("displayName", "") if isinstance(prov, dict) else str(prov)
        ) or item.get("publisher", "")

        # URL
        cu = content.get("canonicalUrl", {})
        url = (
            cu.get("url", "") if isinstance(cu, dict) else ""
        ) or item.get("link", "")

        # Thumbnail
        thumb = content.get("thumbnail", {})
        image = ""
        if isinstance(thumb, dict):
            res = thumb.get("resolutions", [])
            if res and isinstance(res[0], dict):
                image = res[0].get("url", "")

        summary = content.get("summary", "") or item.get("summary", "")

        return StockNews(
            symbol=symbol,
            published_date=published,
            publisher=publisher,
            title=title,
            url=url,
            text=summary,
            image=image,
            site="Yahoo Finance",
        )

    def get_news(self, symbol: str, page: int = 0, limit: int = 50) -> List[StockNews]:
        return self.get_stock_news(symbol=symbol, page=page, limit=limit)

    # ── Market data ───────────────────────────────────────────────────────────

    def get_market_data(
        self,
        symbol: str,
        start: dt.datetime,
        end: dt.datetime,
        timeframe: TimeFrame,
        use_cache: bool = True,
    ) -> List[OHLCData]:
        import pandas as pd

        interval = _INTERVAL_MAP.get(timeframe)
        if not interval:
            raise ValueError(f"Yahoo Finance: unsupported timeframe {timeframe}")

        try:
            df = self._ticker(symbol).history(
                start=start.date().isoformat(),
                end=(end.date() + dt.timedelta(days=1)).isoformat(),
                interval=interval,
            )
        except Exception as e:
            logger.warning("[Yahoo] market data failed for %s: %s", symbol, e)
            return []

        if df is None or df.empty:
            return []

        bars: List[OHLCData] = []
        for ts, row in df.iterrows():
            try:
                bar_time = ts.to_pydatetime().replace(tzinfo=None)
                bars.append(OHLCData(
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row["Volume"]) if not pd.isna(row["Volume"]) else None,
                    time=bar_time,
                    symbol=symbol,
                ))
            except Exception:
                continue

        return bars

    # ── Prices ────────────────────────────────────────────────────────────────

    def get_price_batch(
        self,
        symbols: list[str],
        at: Optional[dt.datetime] = None,
    ) -> Dict[str, PriceQuote]:
        result: Dict[str, PriceQuote] = {}
        for symbol in symbols:
            try:
                fi = self._ticker(symbol).fast_info
                price = float(getattr(fi, "last_price", 0) or 0)
                result[symbol] = PriceQuote(
                    symbol=symbol,
                    bid_price=price,
                    ask_price=price,
                    volume=int(getattr(fi, "three_month_average_volume", 0) or 0),
                    day_high=float(getattr(fi, "day_high", 0) or 0),
                    day_low=float(getattr(fi, "day_low", 0) or 0),
                    change_percentage=float(getattr(fi, "previous_close", 0) or 0),
                )
            except Exception as e:
                logger.warning("[Yahoo] price failed for %s: %s", symbol, e)
        return result

    # ── Company ───────────────────────────────────────────────────────────────

    def get_company_overview(self, symbol: str) -> Optional[dict]:
        cache_key = f"info:{symbol}"
        cached = self._cache.load(cache_key, category="yf")
        if cached is not None:
            return cached
        try:
            info = self._ticker(symbol).info
            self._cache.save(cache_key, info, category="yf", metadata={"ttl": ONE_DAY_SECONDS})
            return info
        except Exception as e:
            logger.warning("[Yahoo] company info failed for %s: %s", symbol, e)
            return None

    def get_company_profile(self, symbol: str) -> Optional[CompanyProfile]:
        info = self.get_company_overview(symbol)
        if not info:
            return None
        return CompanyProfile(
            symbol=info.get("symbol"),
            company_name=info.get("longName") or info.get("shortName"),
            exchange=info.get("exchange"),
            currency=info.get("currency"),
            country=info.get("country"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            description=info.get("longBusinessSummary"),
            website=info.get("website"),
            market_cap=info.get("marketCap"),
            beta=info.get("beta"),
            full_time_employees=str(info.get("fullTimeEmployees", "0")),
        )

    # ── Analyst ───────────────────────────────────────────────────────────────

    def get_price_target_consensus(self, symbol: str) -> Optional[PriceTargetConsensus]:
        try:
            targets = self._ticker(symbol).analyst_price_targets
            if targets is None:
                return None

            def _f(attr: str) -> Optional[float]:
                v = getattr(targets, attr, None)
                try:
                    return float(v) if v is not None else None
                except (TypeError, ValueError):
                    return None

            return PriceTargetConsensus(
                symbol=symbol,
                target_high=_f("high"),
                target_low=_f("low"),
                target_consensus=_f("mean"),
                target_median=_f("median"),
            )
        except Exception as e:
            logger.warning("[Yahoo] price targets failed for %s: %s", symbol, e)
            return None

    def get_upgrades_downgrades(self, symbol: str) -> List[Grade]:
        try:
            df = self._ticker(symbol).upgrades_downgrades
            if df is None or df.empty:
                return []
        except Exception as e:
            logger.warning("[Yahoo] upgrades failed for %s: %s", symbol, e)
            return []

        grades: List[Grade] = []
        for idx, row in df.head(20).iterrows():
            try:
                date = idx.date() if hasattr(idx, "date") else None
                grades.append(Grade(
                    symbol=symbol,
                    date=date,
                    grading_company=str(row.get("Firm", "")),
                    action=str(row.get("Action", "")),
                ))
            except Exception:
                continue
        return grades

    def get_analyst_recommendations(self, symbol: str) -> List[Grade]:
        return self.get_upgrades_downgrades(symbol)

    # ── Calendar ──────────────────────────────────────────────────────────────

    def get_calendar(
        self,
        symbol: str,
        start: dt.date,
        end: dt.date,
        event_types: List[CalendarEventType] | None = None,
    ) -> List[Dict[str, Any]]:
        try:
            cal = self._ticker(symbol).calendar
            if not cal:
                return []
            # yfinance returns a dict like {"Earnings Date": [...], "Earnings High": ...}
            if isinstance(cal, dict):
                return [cal]
            return list(cal)
        except Exception as e:
            logger.warning("[Yahoo] calendar failed for %s: %s", symbol, e)
            return []

    # ── Screener / misc — not available ──────────────────────────────────────

    def get_biggest_gainers(self) -> list:
        return []

    def get_biggest_losers(self) -> list:
        return []

    def senate_trade(self, symbol: str) -> list:
        return []

    def get_earnings_call_transcript(
        self,
        symbol: str,
        year: int | None = None,
        quarter: int | None = None,
    ) -> Optional[EarningsCallTranscript]:
        return None

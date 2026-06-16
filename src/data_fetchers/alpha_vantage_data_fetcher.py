"""
data_fetchers.alpha_vantage_data_fetcher

Financial data via the Alpha Vantage REST API.

Implemented endpoints:
    NEWS_SENTIMENT         → get_stock_news / get_news
    TIME_SERIES_DAILY      → get_market_data (DAY)
    TIME_SERIES_INTRADAY   → get_market_data (1m / 5m / 15m / 30m / 1h)
    GLOBAL_QUOTE           → get_price_batch / get_price
    OVERVIEW               → get_company_overview / get_company_profile /
                             get_price_target_consensus
    TOP_GAINERS_LOSERS     → get_biggest_gainers / get_biggest_losers

Not available in Alpha Vantage:
    senate_trade, get_earnings_call_transcript  → return []  / None
    get_upgrades_downgrades                     → return []

Rate limits (free key):
    25 calls / day, 5 calls / minute.
    Rate-limit responses contain {"Information": "..."}; those are caught
    and logged — the method returns an empty result rather than raising.

Environment:
    AV_API_KEY  or  ALPHA_VANTAGE_API_KEY
"""
from __future__ import annotations

import datetime as dt
from core.utils.log_helper import getLogger
from typing import Any, Dict, List, Optional

import requests

from core.entities.analyst_data import PriceTargetConsensus
from core.entities.calendar_events import CalendarEventType
from core.entities.company_profile import CompanyProfile
from core.entities.earnings import EarningsCallTranscript
from core.entities.market_data import PriceQuote, StockNews
from core.entities.ohlc import OHLCData
from core.entities.time_frame import TimeFrame
from core.utils.mapping import read_dict_key
from infrastructure.cache.memory_cache import MemoryCache

from .data_fetcher_base import DataFetcherBase

logger = getLogger(__name__)

BASE_URL         = "https://www.alphavantage.co/query"
ONE_DAY_SECONDS  = 86_400

# AV intraday interval strings
_INTRADAY_MAP: dict[TimeFrame, str] = {
    TimeFrame.MINUTE_1:  "1min",
    TimeFrame.MINUTE_5:  "5min",
    TimeFrame.MINUTE_15: "15min",
    TimeFrame.MINUTE_30: "30min",
    TimeFrame.HOUR_1:    "60min",
}


class AlphaVantageDataFetcher(DataFetcherBase):
    """
    Fetches financial data from the Alpha Vantage API.

    Config keys:
        api_key  (required) — Alpha Vantage API key
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        self.api_key = read_dict_key("api_key", config, required=True)
        self.cache   = MemoryCache()

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get(
        self,
        params: dict,
        cache_ttl: int = 300,
    ) -> Optional[Any]:
        """
        GET from Alpha Vantage.  Handles rate-limit / error responses.
        Returns parsed JSON or None on any error.
        """
        params = {**params, "apikey": self.api_key}
        cache_key = str(sorted(params.items()))

        if cache_ttl > 0:
            cached = self.cache.load(cache_key, category="av")
            if cached is not None:
                return cached

        try:
            resp = requests.get(BASE_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("AV request failed: %s", e)
            return None

        if isinstance(data, dict):
            if "Information" in data:
                logger.warning("AV rate limit / demo key: %s", data["Information"])
                return None
            if "Error Message" in data:
                logger.error("AV error: %s", data["Error Message"])
                return None
            if "Note" in data:
                logger.warning("AV API note: %s", data["Note"])
                return None

        if cache_ttl > 0 and data is not None:
            self.cache.save(cache_key, data, category="av", metadata={"ttl": cache_ttl})

        return data

    # ── News ──────────────────────────────────────────────────────────────────

    def get_stock_news(
        self,
        symbol: Optional[str] = None,
        page: int = 0,
        limit: int = 50,
    ) -> List[StockNews]:
        params: dict[str, Any] = {"function": "NEWS_SENTIMENT", "limit": min(limit, 200)}
        if symbol:
            params["tickers"] = symbol

        data = self._get(params, cache_ttl=300)
        if not data:
            return []

        feed   = data.get("feed", [])
        items: List[StockNews] = []
        for item in feed:
            ts_raw = item.get("time_published", "")
            try:
                published = dt.datetime.strptime(ts_raw, "%Y%m%dT%H%M%S")
            except ValueError:
                published = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

            items.append(StockNews(
                symbol=symbol or "",
                published_date=published,
                publisher=item.get("source", ""),
                title=item.get("title", ""),
                url=item.get("url", ""),
                text=item.get("summary", ""),
                image=item.get("banner_image", ""),
                site=item.get("source_domain", ""),
            ))
        return items

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
        cache_ttl = 300 if use_cache else 0

        if timeframe == TimeFrame.DAY:
            data = self._get(
                {"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": "full"},
                cache_ttl=3600,
            )
            if not data:
                return []
            ts = data.get("Time Series (Daily)", {})
            return self._parse_daily_series(ts, symbol, start.date(), end.date())

        if timeframe in _INTRADAY_MAP:
            interval = _INTRADAY_MAP[timeframe]
            data = self._get(
                {
                    "function":       "TIME_SERIES_INTRADAY",
                    "symbol":         symbol,
                    "interval":       interval,
                    "outputsize":     "full",
                    "extended_hours": "true",
                },
                cache_ttl=cache_ttl,
            )
            if not data:
                return []
            ts = data.get(f"Time Series ({interval})", {})
            return self._parse_intraday_series(ts, symbol, start, end)

        raise ValueError(f"AlphaVantage: unsupported timeframe {timeframe}")

    def _parse_daily_series(
        self,
        ts: dict,
        symbol: str,
        start: dt.date,
        end: dt.date,
    ) -> List[OHLCData]:
        bars: List[OHLCData] = []
        for date_str, ohlcv in ts.items():
            try:
                bar_date = dt.datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue
            if not (start <= bar_date.date() <= end):
                continue
            bars.append(OHLCData(
                open=float(ohlcv["1. open"]),
                high=float(ohlcv["2. high"]),
                low=float(ohlcv["3. low"]),
                close=float(ohlcv["4. close"]),
                volume=float(ohlcv["5. volume"]),
                time=bar_date,
                symbol=symbol,
            ))
        bars.sort(key=lambda b: b.time)
        return bars

    def _parse_intraday_series(
        self,
        ts: dict,
        symbol: str,
        start: dt.datetime,
        end: dt.datetime,
    ) -> List[OHLCData]:
        bars: List[OHLCData] = []
        for dt_str, ohlcv in ts.items():
            try:
                bar_time = dt.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if not (start <= bar_time <= end):
                continue
            bars.append(OHLCData(
                open=float(ohlcv["1. open"]),
                high=float(ohlcv["2. high"]),
                low=float(ohlcv["3. low"]),
                close=float(ohlcv["4. close"]),
                volume=float(ohlcv["5. volume"]),
                time=bar_time,
                symbol=symbol,
            ))
        bars.sort(key=lambda b: b.time)
        return bars

    # ── Prices ────────────────────────────────────────────────────────────────

    def get_price_batch(
        self,
        symbols: list[str],
        at: Optional[dt.datetime] = None,
    ) -> Dict[str, PriceQuote]:
        result: Dict[str, PriceQuote] = {}
        for symbol in symbols:
            data = self._get(
                {"function": "GLOBAL_QUOTE", "symbol": symbol},
                cache_ttl=5,
            )
            if not data:
                continue
            q = data.get("Global Quote", {})
            if not q:
                continue
            chg_pct_raw = q.get("10. change percent", "0%").rstrip("%")
            try:
                chg_pct = float(chg_pct_raw)
            except ValueError:
                chg_pct = 0.0
            price = float(q.get("05. price", 0) or 0)
            result[symbol] = PriceQuote(
                symbol=symbol,
                bid_price=price,
                ask_price=price,
                volume=int(float(q.get("06. volume", 0) or 0)),
                change_percentage=chg_pct,
                day_high=float(q.get("03. high", 0) or 0),
                day_low=float(q.get("04. low", 0) or 0),
            )
        return result

    # ── Company ───────────────────────────────────────────────────────────────

    def get_company_overview(self, symbol: str) -> Optional[dict]:
        return self._get(
            {"function": "OVERVIEW", "symbol": symbol},
            cache_ttl=ONE_DAY_SECONDS,
        )

    def get_company_profile(self, symbol: str) -> Optional[CompanyProfile]:
        data = self.get_company_overview(symbol)
        if not data or not isinstance(data, dict):
            return None

        def _f(key: str) -> Optional[float]:
            try:
                return float(data.get(key, "") or 0) or None
            except (ValueError, TypeError):
                return None

        return CompanyProfile(
            symbol=data.get("Symbol"),
            company_name=data.get("Name"),
            cik=data.get("CIK"),
            exchange=data.get("Exchange"),
            currency=data.get("Currency"),
            country=data.get("Country"),
            sector=data.get("Sector"),
            industry=data.get("Industry"),
            description=data.get("Description"),
            website=data.get("OfficialSite"),
            market_cap=int(float(data["MarketCapitalization"])) if data.get("MarketCapitalization") else None,
            beta=_f("Beta"),
        )

    def get_price_target_consensus(self, symbol: str) -> Optional[PriceTargetConsensus]:
        data = self.get_company_overview(symbol)
        if not data or not isinstance(data, dict):
            return None

        target = data.get("AnalystTargetPrice")
        if not target:
            return None

        def _f(v) -> Optional[float]:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        return PriceTargetConsensus(
            symbol=symbol,
            target_consensus=_f(target),
            target_median=_f(target),
        )

    # ── Screener ──────────────────────────────────────────────────────────────

    def get_biggest_gainers(self) -> list:
        data = self._get({"function": "TOP_GAINERS_LOSERS"}, cache_ttl=60)
        return (data or {}).get("top_gainers", [])

    def get_biggest_losers(self) -> list:
        data = self._get({"function": "TOP_GAINERS_LOSERS"}, cache_ttl=60)
        return (data or {}).get("top_losers", [])

    # ── Analyst — not available in AV ─────────────────────────────────────────

    def get_upgrades_downgrades(self, symbol: str) -> list:
        return []

    def get_analyst_recommendations(self, symbol: str) -> list:
        return []

    # ── Calendar ──────────────────────────────────────────────────────────────

    def get_calendar(
        self,
        symbol: str,
        start: dt.date,
        end: dt.date,
        event_types: List[CalendarEventType] | None = None,
    ) -> List[Dict[str, Any]]:
        data = self._get(
            {
                "function": "EARNINGS_CALENDAR",
                "symbol":   symbol,
                "horizon":  "3month",
            },
            cache_ttl=ONE_DAY_SECONDS,
        )
        if not data or not isinstance(data, str):
            return []
        # AV returns CSV for EARNINGS_CALENDAR
        lines = [l.strip() for l in data.strip().splitlines() if l.strip()]
        if len(lines) < 2:
            return []
        headers = [h.strip() for h in lines[0].split(",")]
        results = []
        for line in lines[1:]:
            values = [v.strip() for v in line.split(",")]
            results.append(dict(zip(headers, values)))
        return results

    # ── Not supported ─────────────────────────────────────────────────────────

    def senate_trade(self, symbol: str) -> list:
        return []

    def get_earnings_call_transcript(
        self,
        symbol: str,
        year: int | None = None,
        quarter: int | None = None,
    ) -> Optional[EarningsCallTranscript]:
        return None

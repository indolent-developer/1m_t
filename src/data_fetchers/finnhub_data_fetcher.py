"""
data_fetchers.finnhub_data_fetcher

Financial data via the Finnhub REST API.

Implemented endpoints:
    company-news           → get_stock_news / get_news
    quote                  → get_price_batch / get_price
    stock/candles          → get_market_data
    stock/profile2         → get_company_overview / get_company_profile
    stock/recommendation   → get_upgrades_downgrades / get_analyst_recommendations
    stock/price-target     → get_price_target_consensus
    calendar/earnings      → get_calendar

Not available in Finnhub free tier:
    senate_trade                  → returns []
    get_earnings_call_transcript  → returns None
    get_biggest_gainers/losers    → returns []

Rate limits (free key): 60 calls / minute.

Environment:
    FINNHUB_API_KEY
"""
from __future__ import annotations

import datetime as dt
from core.utils.log_helper import getLogger
from typing import Any, Dict, List, Optional

import requests

from core.entities.analyst_data import Grade, PriceTargetConsensus
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

BASE_URL        = "https://finnhub.io/api/v1"
ONE_DAY_SECONDS = 86_400

# Finnhub resolution strings
_RESOLUTION_MAP: dict[TimeFrame, str] = {
    TimeFrame.MINUTE_1:  "1",
    TimeFrame.MINUTE_5:  "5",
    TimeFrame.MINUTE_15: "15",
    TimeFrame.MINUTE_30: "30",
    TimeFrame.HOUR_1:    "60",
    TimeFrame.DAY:       "D",
    TimeFrame.WEEK:      "W",
    TimeFrame.MONTH:     "M",
}


class FinnhubDataFetcher(DataFetcherBase):
    """
    Fetches financial data from the Finnhub API.

    Config keys:
        api_key  (required) — Finnhub API key
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        self.api_key = read_dict_key("api_key", config, required=True)
        self.cache   = MemoryCache()

    # ── HTTP helper ───────────────────────────────────────────────────────────

    def _get(
        self,
        endpoint: str,
        params: dict | None = None,
        cache_ttl: int = 300,
    ) -> Optional[Any]:
        """GET from Finnhub with caching. Returns parsed JSON or None."""
        params = dict(params or {})
        params["token"] = self.api_key
        cache_key = f"{endpoint}:{sorted((k, v) for k, v in params.items() if k != 'token')}"

        if cache_ttl > 0:
            cached = self.cache.load(cache_key, category="finnhub")
            if cached is not None:
                return cached

        try:
            resp = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("Finnhub request failed [%s]: %s", endpoint, e)
            return None

        if isinstance(data, dict) and data.get("error"):
            logger.error("Finnhub API error [%s]: %s", endpoint, data["error"])
            return None

        if cache_ttl > 0 and data is not None:
            self.cache.save(cache_key, data, category="finnhub", metadata={"ttl": cache_ttl})

        return data

    # ── News ──────────────────────────────────────────────────────────────────

    def get_stock_news(
        self,
        symbol: Optional[str] = None,
        page: int = 0,
        limit: int = 50,
    ) -> List[StockNews]:
        if not symbol:
            logger.debug("Finnhub requires a symbol for company news — skipping general news")
            return []

        to_date   = dt.date.today()
        from_date = to_date - dt.timedelta(days=7)

        raw = self._get(
            "company-news",
            {
                "symbol": symbol,
                "from":   from_date.isoformat(),
                "to":     to_date.isoformat(),
            },
            cache_ttl=300,
        )
        if not isinstance(raw, list):
            return []

        items: List[StockNews] = []
        for item in raw[:limit]:
            ts = item.get("datetime", 0)
            try:
                published = dt.datetime.fromtimestamp(int(ts)) if ts else dt.datetime.now()
            except (OSError, OverflowError, ValueError):
                published = dt.datetime.now()

            items.append(StockNews(
                symbol=symbol,
                published_date=published,
                publisher=item.get("source", ""),
                title=item.get("headline", ""),
                url=item.get("url", ""),
                text=item.get("summary", ""),
                image=item.get("image", ""),
                site=item.get("source", ""),
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
        resolution = _RESOLUTION_MAP.get(timeframe)
        if not resolution:
            raise ValueError(f"Finnhub: unsupported timeframe {timeframe}")

        data = self._get(
            "stock/candles",
            {
                "symbol":     symbol,
                "resolution": resolution,
                "from":       int(start.timestamp()),
                "to":         int(end.timestamp()),
            },
            cache_ttl=300 if use_cache else 0,
        )
        if not data or data.get("s") != "ok":
            return []

        timestamps = data.get("t", [])
        opens      = data.get("o", [])
        highs      = data.get("h", [])
        lows       = data.get("l", [])
        closes     = data.get("c", [])
        volumes    = data.get("v", [])

        bars: List[OHLCData] = []
        for i, ts in enumerate(timestamps):
            try:
                bar_time = dt.datetime.fromtimestamp(int(ts))
            except (OSError, OverflowError, ValueError):
                continue
            bars.append(OHLCData(
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                volume=float(volumes[i]) if volumes else None,
                time=bar_time,
                symbol=symbol,
            ))
        return bars

    # ── Prices ────────────────────────────────────────────────────────────────

    def get_price_batch(
        self,
        symbols: list[str],
        at: Optional[dt.datetime] = None,
    ) -> Dict[str, PriceQuote]:
        result: Dict[str, PriceQuote] = {}
        for symbol in symbols:
            data = self._get("quote", {"symbol": symbol}, cache_ttl=5)
            if not data:
                continue
            price = float(data.get("c", 0) or 0)
            result[symbol] = PriceQuote(
                symbol=symbol,
                bid_price=price,
                ask_price=price,
                volume=int(data.get("v", 0) or 0),
                change_percentage=float(data.get("dp", 0) or 0),
                day_high=float(data.get("h", 0) or 0),
                day_low=float(data.get("l", 0) or 0),
                timestamp=int(data.get("t", 0) or 0),
            )
        return result

    # ── Company ───────────────────────────────────────────────────────────────

    def get_company_overview(self, symbol: str) -> Optional[dict]:
        return self._get("stock/profile2", {"symbol": symbol}, cache_ttl=ONE_DAY_SECONDS)

    def get_company_profile(self, symbol: str) -> Optional[CompanyProfile]:
        data = self.get_company_overview(symbol)
        if not data or not isinstance(data, dict):
            return None

        # marketCapitalization is in millions
        mcap_m = data.get("marketCapitalization")
        market_cap = int(float(mcap_m) * 1_000_000) if mcap_m else None

        return CompanyProfile(
            symbol=data.get("ticker"),
            company_name=data.get("name"),
            exchange=data.get("exchange"),
            currency=data.get("currency"),
            country=data.get("country"),
            industry=data.get("finnhubIndustry"),
            website=data.get("weburl"),
            market_cap=market_cap,
            phone=data.get("phone", ""),
            image=data.get("logo", ""),
        )

    # ── Analyst ───────────────────────────────────────────────────────────────

    def get_upgrades_downgrades(self, symbol: str) -> List[Grade]:
        """
        Maps Finnhub stock/recommendation (consensus counts per period) to
        Grade objects.  Each period becomes one Grade entry showing the
        consensus action (buy / hold / sell) for that month.
        """
        raw = self._get(
            "stock/recommendation",
            {"symbol": symbol},
            cache_ttl=3600,
        )
        if not isinstance(raw, list):
            return []

        grades: List[Grade] = []
        for item in raw:
            period = item.get("period", "")
            try:
                date = dt.datetime.strptime(period, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                date = None

            # Determine dominant action from counts
            counts = {
                "strongBuy":  item.get("strongBuy",  0),
                "buy":        item.get("buy",         0),
                "hold":       item.get("hold",        0),
                "sell":       item.get("sell",        0),
                "strongSell": item.get("strongSell",  0),
            }
            dominant = max(counts, key=lambda k: counts[k])
            action_label = {
                "strongBuy":  "Strong Buy",
                "buy":        "Buy",
                "hold":       "Hold",
                "sell":       "Sell",
                "strongSell": "Strong Sell",
            }.get(dominant, "Hold")

            grades.append(Grade(
                symbol=symbol,
                date=date,
                grading_company="Finnhub Consensus",
                action=action_label,
            ))
        return grades

    def get_analyst_recommendations(self, symbol: str) -> List[Grade]:
        return self.get_upgrades_downgrades(symbol)

    def get_price_target_consensus(self, symbol: str) -> Optional[PriceTargetConsensus]:
        data = self._get("stock/price-target", {"symbol": symbol}, cache_ttl=3600)
        if not data or not isinstance(data, dict):
            return None

        def _f(key: str) -> Optional[float]:
            try:
                return float(data[key])
            except (KeyError, TypeError, ValueError):
                return None

        return PriceTargetConsensus(
            symbol=symbol,
            target_high=_f("targetHigh"),
            target_low=_f("targetLow"),
            target_consensus=_f("targetMean"),
            target_median=_f("targetMedian"),
        )

    # ── Calendar ──────────────────────────────────────────────────────────────

    def get_calendar(
        self,
        symbol: str,
        start: dt.date,
        end: dt.date,
        event_types: List[CalendarEventType] | None = None,
    ) -> List[Dict[str, Any]]:
        data = self._get(
            "calendar/earnings",
            {
                "symbol": symbol,
                "from":   start.isoformat(),
                "to":     end.isoformat(),
            },
            cache_ttl=ONE_DAY_SECONDS,
        )
        if not data:
            return []
        return data.get("earningsCalendar", [])

    # ── Screener — not available ──────────────────────────────────────────────

    def get_biggest_gainers(self) -> list:
        return []

    def get_biggest_losers(self) -> list:
        return []

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

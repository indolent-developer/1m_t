from __future__ import annotations

import datetime as dt
from core.utils.log_helper import getLogger
from typing import Any, Dict, List, Optional

import requests

from core.entities.analyst_data import (
    AnalystRatingSnapshot,
    Grade,
    GradesSummary,
    PriceTargetConsensus,
    PriceTargetNews,
)
from core.entities.calendar_events import CalendarEventType
from core.entities.company_profile import CompanyProfile
from core.entities.market_data import PriceQuote, StockNews
from core.entities.ohlc import OHLCData
from core.entities.time_frame import TimeFrame
from core.utils.mapping import (
    dict_arr_to_obj_arr,
    dict_arr_to_obj_arr_dict_in,
    read_dict_key,
)
from core.utils.market import is_extended_market_time
from infrastructure.cache.memory_cache import MemoryCache

from .data_fetcher_base import DataFetcherBase

logger = getLogger(__name__)

ONE_DAY_SECONDS = 86_400


class FmpDataFetcher(DataFetcherBase):
    """
    Fetches financial data from the Financial Modeling Prep (FMP) API.

    Config keys:
        api_key   (required) — FMP API key
        base_url  (optional) — defaults to FMP v3 stable base
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        self.api_key    = read_dict_key("api_key",  config, required=True)
        self.base_url   = read_dict_key(
            "base_url", config,
            default="https://financialmodelingprep.com/api/v3/",
        )
        # Intraday chart endpoints moved to /stable/ in the current FMP API
        self.stable_url = read_dict_key(
            "stable_url", config,
            default="https://financialmodelingprep.com/stable/",
        )
        self.cache = MemoryCache()

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def handle_response(self, response: requests.Response, throw_error: bool = True) -> Any:
        if response.status_code == 200:
            return response.json()
        if throw_error:
            raise RuntimeError(f"FMP API error {response.status_code}: {response.text}")
        logger.error("FMP API error %s: %s", response.status_code, response.text)
        return None

    def run_query(
        self,
        endpoint: str,
        params: dict | None = None,
        cache_time_seconds: int = 300,
        base_url: str | None = None,
    ) -> Any:
        cache_key = f"{endpoint}:{params}"

        if cache_time_seconds > 0:
            cached = self.cache.load(cache_key, category="fmp")
            if cached is not None:
                logger.debug("Cache hit: %s", cache_key)
                return cached

        params = dict(params or {})
        params["apikey"] = self.api_key

        url = f"{base_url or self.base_url}{endpoint}"
        response = requests.get(url, params=params, timeout=10)
        data = self.handle_response(response, throw_error=False)

        if cache_time_seconds > 0 and data is not None:
            self.cache.save(cache_key, data, category="fmp", metadata={"ttl": cache_time_seconds})

        return data

    # ── Grades / analyst ──────────────────────────────────────────────────────

    def get_upgrades_downgrades(self, symbol: str) -> List[Grade]:
        res = self.run_query("grades", params={"symbol": symbol}, cache_time_seconds=3600)
        return dict_arr_to_obj_arr(res, Grade)

    def get_grades_summary(self, symbol: str) -> Optional[GradesSummary]:
        res = self.run_query("grades-consensus", params={"symbol": symbol}, cache_time_seconds=3600)
        items = dict_arr_to_obj_arr(res, GradesSummary, mapping={
            "strongBuy":  "strong_buy",
            "strongSell": "strong_sell",
        })
        return items[0] if items else None

    def get_analyst_recommendations(self, symbol: str) -> list:
        return self.get_upgrades_downgrades(symbol)

    def get_rating_snapshot(self, symbol: str) -> Optional[AnalystRatingSnapshot]:
        res = self.run_query("ratings-snapshot", params={"symbol": symbol})
        if res and isinstance(res, dict):
            return AnalystRatingSnapshot.from_dict(res)
        return None

    # ── Price targets ─────────────────────────────────────────────────────────

    def get_price_target_news(self, symbol: str) -> List[PriceTargetNews]:
        res = self.run_query(
            "price-target-news",
            params={"symbol": symbol, "limit": 50},
            cache_time_seconds=3600,
        )
        return dict_arr_to_obj_arr(res, PriceTargetNews, mapping={
            "publishedDate":  "published_date",
            "newsURL":        "news_url",
            "newsTitle":      "news_title",
            "analystName":    "analyst_name",
            "priceTarget":    "price_target",
            "adjPriceTarget": "adj_price_target",
            "priceWhenPosted":"price_when_posted",
            "newsPublisher":  "news_publisher",
            "newsBaseURL":    "news_base_url",
            "analystCompany": "analyst_company",
        })

    def get_price_target_consensus(self, symbol: str) -> Optional[PriceTargetConsensus]:
        res = self.run_query(
            "price-target-consensus",
            params={"symbol": symbol},
            cache_time_seconds=3600,
        )
        if res and isinstance(res, list) and res:
            return PriceTargetConsensus.from_dict(res[0])
        return None

    # ── Company ───────────────────────────────────────────────────────────────

    def get_company_profile(self, symbol: str) -> Optional[CompanyProfile]:
        res = self.run_query("profile", params={"symbol": symbol}, cache_time_seconds=ONE_DAY_SECONDS)
        if res and isinstance(res, list) and res:
            return dict_arr_to_obj_arr_dict_in(res, CompanyProfile)[0]
        return None

    def get_company_overview(self, symbol: str) -> Optional[dict]:
        return self.run_query("profile", params={"symbol": symbol}, cache_time_seconds=ONE_DAY_SECONDS)

    # ── News ──────────────────────────────────────────────────────────────────

    def get_news(self, symbol: str, page: int = 0, limit: int = 100) -> List[StockNews]:
        return self.get_stock_news(symbol=symbol, page=page, limit=limit)

    def get_stock_news(
        self,
        symbol: Optional[str] = None,
        page: int = 0,
        limit: int = 100,
    ) -> List[StockNews]:
        mapping = {"publishedDate": "published_date"}

        if not symbol:
            res = self.run_query(
                "news/stock-latest",
                params={"page": page, "limit": limit},
                cache_time_seconds=300,
                base_url=self.stable_url,
            )
            return dict_arr_to_obj_arr(res, StockNews, mapping=mapping)

        params = {"symbols": symbol, "limit": limit}
        stock_res  = self.run_query("news/stock",          params=params, cache_time_seconds=300, base_url=self.stable_url)
        press_res  = self.run_query("news/press-releases", params=params, cache_time_seconds=300, base_url=self.stable_url)

        stock_items = dict_arr_to_obj_arr(stock_res  or [], StockNews, mapping=mapping)
        press_items = dict_arr_to_obj_arr(press_res  or [], StockNews, mapping=mapping)

        # Dedupe by URL before returning; news_service dedupes by title across sources
        seen_urls: set[str] = set()
        merged: List[StockNews] = []
        for item in stock_items + press_items:
            url = getattr(item, "url", None) or getattr(item, "link", None) or ""
            if url not in seen_urls:
                seen_urls.add(url)
                merged.append(item)
        return merged

    # ── Market data ───────────────────────────────────────────────────────────

    def get_market_data(
        self,
        symbol: str,
        start: dt.datetime,
        end: dt.datetime,
        timeframe: TimeFrame,
        use_cache: bool = True,
        cache_time: int = 250,
    ) -> List[OHLCData]:
        if timeframe == TimeFrame.DAY:
            res = self.run_query(
                "historical-price-eod/full",
                params={"symbol": symbol, "from": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d")},
                cache_time_seconds=3600,
            )
            if not res or not isinstance(res, list):
                return []
            data = []
            for item in res:
                item.update({"o": item["open"], "h": item["high"], "l": item["low"],
                             "c": item["close"], "v": item["volume"], "t": item["date"]})
                data.append(OHLCData.from_dict(item, dt_format="%Y-%m-%d"))
            return data

        if timeframe in (TimeFrame.MINUTE_1, TimeFrame.MINUTE_5, TimeFrame.MINUTE_15,
                         TimeFrame.MINUTE_30, TimeFrame.HOUR_1):
            res = self.run_query(
                f"historical-chart/{self._convert_timeframe_to_api_param(timeframe)}",
                params={
                    "symbol": symbol,
                    "from":   start.strftime("%Y-%m-%d"),
                    "to":     end.strftime("%Y-%m-%d"),
                    "extended": "true",
                },
                cache_time_seconds=cache_time if use_cache else 0,
                base_url=self.stable_url,
            )
            if not res or not isinstance(res, list):
                return []
            data = []
            for item in res:
                item.update({"o": item["open"], "h": item["high"], "l": item["low"],
                             "c": item["close"], "v": item["volume"], "t": item["date"]})
                data.append(OHLCData.from_dict(item, dt_format="%Y-%m-%d %H:%M:%S"))
            data.reverse()
            return data

        raise ValueError(f"Unsupported timeframe: {timeframe}")

    def _convert_timeframe_to_api_param(self, timeframe: TimeFrame) -> str:
        return {
            TimeFrame.MINUTE_1:  "1min",
            TimeFrame.MINUTE_5:  "5min",
            TimeFrame.MINUTE_15: "15min",
            TimeFrame.MINUTE_30: "30min",
            TimeFrame.HOUR_1:    "1hour",
            TimeFrame.DAY:       "1day",
            TimeFrame.WEEK:      "1week",
            TimeFrame.MONTH:     "1month",
        }.get(timeframe, "1day")

    # ── Prices ────────────────────────────────────────────────────────────────

    def get_price_batch(
        self,
        symbols: list[str],
        at: Optional[dt.datetime] = None,
    ) -> Dict[str, PriceQuote]:
        symbols_str = ",".join(symbols)

        if is_extended_market_time():
            res = self.run_query(
                "batch-aftermarket-quote",
                params={"symbols": symbols_str},
                cache_time_seconds=5,
            )
            if res and isinstance(res, list):
                quotes = dict_arr_to_obj_arr(res, PriceQuote, mapping={
                    "bidSize":   "bid_size",
                    "bidPrice":  "bid_price",
                    "askSize":   "ask_size",
                    "askPrice":  "ask_price",
                })
                return {q.symbol: q for q in quotes}
        else:
            res = self.run_query(
                "batch-quote",
                params={"symbols": symbols_str},
                cache_time_seconds=5,
            )
            if res and isinstance(res, list):
                quotes = []
                for item in res:
                    quotes.append(PriceQuote(
                        symbol=item.get("symbol", ""),
                        bid_price=item.get("price", 0.0),
                        ask_price=item.get("price", 0.0),
                        bid_size=1,
                        ask_size=1,
                        volume=item.get("volume", 0),
                        timestamp=item.get("timestamp", 0),
                        change_percentage=item.get("changePercentage", 0.0),
                        day_high=item.get("dayHigh", 0.0),
                        day_low=item.get("dayLow", 0.0),
                    ))
                return {q.symbol: q for q in quotes}
        return {}

    def get_price(self, symbol: str, at: Optional[dt.datetime] = None) -> float:
        result = self.get_price_batch([symbol], at=at)
        quote  = result.get(symbol)
        return quote.price if quote else 0.0

    def get_last_price(self, symbol: str) -> float:
        return self.get_price(symbol)

    # ── Calendar / misc ───────────────────────────────────────────────────────

    def get_calendar(
        self,
        symbol: str,
        start: dt.date,
        end: dt.date,
        event_types: List[CalendarEventType] | None = None,
    ) -> List[Dict[str, Any]]:
        return []

    def get_biggest_gainers(self) -> list:
        return self.run_query("stock_market/gainers", cache_time_seconds=60) or []

    def get_biggest_losers(self) -> list:
        return self.run_query("stock_market/losers", cache_time_seconds=60) or []

    def senate_trade(self, symbol: str) -> list:
        return self.run_query("senate-trading", params={"symbol": symbol}, cache_time_seconds=3600) or []

    def get_earnings_call_transcript(
        self,
        symbol: str,
        year: int | None = None,
        quarter: int | None = None,
    ):
        params: dict = {"symbol": symbol}
        if year:
            params["year"] = year
        if quarter:
            params["quarter"] = quarter
        return self.run_query("earning_call_transcript", params=params, cache_time_seconds=ONE_DAY_SECONDS)

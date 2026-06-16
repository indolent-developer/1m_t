"""
data_fetchers.benzinga_data_fetcher

Financial news via the Benzinga REST API v2.

Benzinga is primarily a news source.  All other DataFetcherBase methods
return empty / None stubs since Benzinga doesn't offer OHLCV, quotes, etc.

Implemented:
    GET /api/v2/news/        → get_stock_news / get_news

Not available via Benzinga:
    get_market_data, get_price_batch, get_company_profile,
    get_price_target_consensus, get_upgrades_downgrades, get_calendar,
    get_biggest_gainers/losers, senate_trade,
    get_earnings_call_transcript   → all return [] / None

Environment:
    BENZINGA_API_KEY  — primary
    BENZIGA_API_KEY   — accepted too (common typo in older configs)

API reference: https://docs.benzinga.com/benzinga/newsfeed-v2.html
"""
from __future__ import annotations

import datetime as dt
from core.utils.log_helper import getLogger
import re
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

import requests

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

BASE_URL = "https://api.benzinga.com/api/v2"

# Benzinga `created` can arrive in several formats
_DATE_FMTS = [
    "%Y-%m-%dT%H:%M:%S.%f",   # 2026-06-09T05:12:33.000000
    "%Y-%m-%dT%H:%M:%S",      # 2026-06-09T05:12:33
    "%Y-%m-%d %H:%M:%S",      # 2026-06-09 05:12:33
    "%Y-%m-%d",               # 2026-06-09
]


def _parse_date(raw: str) -> dt.datetime:
    """Parse Benzinga date strings; falls back to now() on failure."""
    if not raw:
        return dt.datetime.now()

    # Strip trailing timezone offset before trying strptime patterns
    clean = re.sub(r"[+-]\d{2}:\d{2}$", "", raw.strip())

    for fmt in _DATE_FMTS:
        try:
            return dt.datetime.strptime(clean, fmt)
        except ValueError:
            pass

    # RFC 2822 — "Mon, 09 Jun 2026 05:12:33 -0400"
    try:
        return parsedate_to_datetime(raw).replace(tzinfo=None)
    except Exception:
        pass

    return dt.datetime.now()


class BenzingaDataFetcher(DataFetcherBase):
    """
    Fetches news from the Benzinga REST API v2.

    Config keys:
        api_key   (required) — Benzinga API token
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        self.api_key = config["api_key"]
        self._cache  = MemoryCache()
        self._session = requests.Session()
        self._session.headers.update({"accept": "application/json"})

    # ── HTTP helper ───────────────────────────────────────────────────────────

    def _get(
        self,
        endpoint: str,
        params: dict,
        cache_ttl: int = 300,
    ) -> Optional[Any]:
        cache_key = f"{endpoint}:{sorted((k, v) for k, v in params.items() if k != 'token')}"
        cached = self._cache.load(cache_key, category="benzinga")
        if cached is not None:
            return cached

        all_params = {**params, "token": self.api_key}
        try:
            resp = self._session.get(
                f"{BASE_URL}/{endpoint}/",
                params=all_params,
                timeout=10,
            )
        except requests.RequestException as e:
            logger.warning("[Benzinga] request failed: %s", e)
            return None

        if resp.status_code == 401:
            logger.warning(
                "[Benzinga] 401 — check BENZINGA_API_KEY; "
                "key may need a paid Benzinga subscription"
            )
            return None
        if resp.status_code != 200:
            logger.warning("[Benzinga] API error %d for /%s", resp.status_code, endpoint)
            return None

        try:
            data = resp.json()
        except Exception as e:
            logger.warning("[Benzinga] JSON decode failed: %s", e)
            return None

        self._cache.save(cache_key, data, category="benzinga", metadata={"ttl": cache_ttl})
        return data

    # ── News ──────────────────────────────────────────────────────────────────

    def get_stock_news(
        self,
        symbol: Optional[str] = None,
        page: int = 0,
        limit: int = 50,
    ) -> List[StockNews]:
        today     = dt.date.today()
        date_from = (today - dt.timedelta(days=7)).isoformat()
        date_to   = today.isoformat()

        params: dict = {
            "pageSize":     min(limit, 100),
            "page":         page,
            "displayOutput": "abstract",
            "dateFrom":     date_from,
            "dateTo":       date_to,
        }
        if symbol:
            params["tickers"] = symbol

        data = self._get("news", params)
        if not data or not isinstance(data, list):
            return []

        items: List[StockNews] = []
        for item in data:
            title = item.get("title", "")
            if not title:
                continue
            published = _parse_date(item.get("created", ""))
            items.append(StockNews(
                symbol=symbol or "",
                published_date=published,
                publisher=item.get("author", "Benzinga"),
                title=title,
                url=item.get("url", ""),
                text=item.get("teaser", ""),
                site="Benzinga",
            ))

        return items[:limit]

    def get_news(self, symbol: str, page: int = 0, limit: int = 50) -> List[StockNews]:
        return self.get_stock_news(symbol=symbol, page=page, limit=limit)

    # ── Not available — stubs ─────────────────────────────────────────────────

    def get_market_data(
        self,
        symbol: str,
        start: dt.datetime,
        end: dt.datetime,
        timeframe: TimeFrame,
        use_cache: bool = True,
    ) -> List[OHLCData]:
        return []

    def get_price_batch(
        self,
        symbols: list[str],
        at: Optional[dt.datetime] = None,
    ) -> Dict[str, PriceQuote]:
        return {}

    def get_company_overview(self, symbol: str) -> Optional[dict]:
        return None

    def get_company_profile(self, symbol: str) -> Optional[CompanyProfile]:
        return None

    def get_price_target_consensus(self, symbol: str) -> Optional[PriceTargetConsensus]:
        return None

    def get_upgrades_downgrades(self, symbol: str) -> List[Grade]:
        return []

    def get_analyst_recommendations(self, symbol: str) -> list:
        return []

    def get_calendar(
        self,
        symbol: str,
        start: dt.date,
        end: dt.date,
        event_types: List[CalendarEventType] | None = None,
    ) -> List[Dict[str, Any]]:
        return []

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

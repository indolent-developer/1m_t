"""
services.price_history_service

Async OHLC bar fetcher with Redis caching on top of any DataFetcherBase.

Cache keys (stored under Redis prefix "1m"):
    1m:{symbol}:price_history_tf_1d_start_{start}_end_{end}
    1m:{symbol}:price_history_tf_1h_start_{start}_end_{end}
    1m:{symbol}:price_history_tf_5m_start_{start}_end_{end}
    ...
"""
from __future__ import annotations

import asyncio
import datetime as dt
from core.utils.log_helper import getLogger

from core.entities.ohlc import OHLCData
from core.entities.time_frame import TimeFrame
from data_fetchers.data_fetcher_base import DataFetcherBase
from infrastructure.cache.redis_cache import RedisCache

logger = getLogger(__name__)

_TF_ABBREV: dict[TimeFrame, str] = {
    TimeFrame.DAY:       "1d",
    TimeFrame.HOUR_1:    "1h",
    TimeFrame.HOUR_4:    "4h",
    TimeFrame.MINUTE_1:  "1m",
    TimeFrame.MINUTE_5:  "5m",
    TimeFrame.MINUTE_15: "15m",
    TimeFrame.MINUTE_30: "30m",
}

_TTL: dict[TimeFrame, int] = {
    TimeFrame.DAY:       6 * 3600,   # 6h — daily bars rarely change intraday
    TimeFrame.HOUR_1:    30 * 60,    # 30min
    TimeFrame.HOUR_4:    60 * 60,    # 1h
    TimeFrame.MINUTE_5:  5 * 60,     # 5min — matches bar interval
    TimeFrame.MINUTE_1:  60,
    TimeFrame.MINUTE_15: 5 * 60,
    TimeFrame.MINUTE_30: 10 * 60,
}
_DEFAULT_TTL = 3600


class PriceHistoryService:
    """Async OHLC bar fetcher with Redis caching.

    Uses symbol as the Redis category and includes the fetcher name in the key
    so Capital, FMP, IBKR etc. never share cached bars:
        1m:{symbol}:price_history_tf_{tf}_{fetcher}_start_{start}_end_{end}

    Args:
        fetcher_name: Short identifier for the data source, e.g. "fmp", "capital",
                      "ibkr". Defaults to the fetcher's class name lowercased.
    """

    def __init__(
        self,
        fetcher: DataFetcherBase,
        cache: RedisCache,
        fetcher_name: str = "",
    ) -> None:
        self._fetcher      = fetcher
        self._cache        = cache
        self._fetcher_name = fetcher_name or type(fetcher).__name__.lower()

    async def get_bars(
        self,
        symbol: str,
        timeframe: TimeFrame,
        start: dt.date,
        end: dt.date,
    ) -> list[OHLCData]:
        tf_str = _TF_ABBREV.get(timeframe, timeframe.value)
        key = f"price_history_tf_{tf_str}_{self._fetcher_name}_start_{start}_end_{end}"

        cached = await self._cache.load(key, category=symbol)
        if cached is not None:
            return [OHLCData.from_dict(b) for b in cached]

        start_dt = dt.datetime.combine(start, dt.time.min)
        end_dt   = dt.datetime.combine(end,   dt.time.max)
        bars: list[OHLCData] = await asyncio.to_thread(
            self._fetcher.get_market_data,
            symbol=symbol,
            start=start_dt,
            end=end_dt,
            timeframe=timeframe,
            use_cache=False,
        )
        if bars:
            ttl = _TTL.get(timeframe, _DEFAULT_TTL)
            await self._cache.save(
                key,
                [b.to_dict() for b in bars],
                category=symbol,
                ttl=ttl,
            )
            logger.info(
                "PriceHistoryService: [%s] %s %s — %d bars fetched and cached (TTL %ds)",
                self._fetcher_name, symbol, tf_str, len(bars), ttl,
            )
        return bars or []

    async def get_daily_bars(self, symbol: str, days: int = 60) -> list[OHLCData]:
        """Last `days` calendar days of daily bars. Returns newest→oldest (FMP order)."""
        end   = dt.date.today()
        start = end - dt.timedelta(days=days + 5)  # +5 to absorb weekends
        return await self.get_bars(symbol, TimeFrame.DAY, start, end)

    async def get_hourly_bars(self, symbol: str, days: int = 5) -> list[OHLCData]:
        """Last `days` calendar days of 1H bars. Returns oldest→newest."""
        end   = dt.date.today()
        start = end - dt.timedelta(days=days + 2)
        return await self.get_bars(symbol, TimeFrame.HOUR_1, start, end)

    async def get_intraday_bars(
        self,
        symbol: str,
        timeframe: TimeFrame = TimeFrame.MINUTE_5,
    ) -> list[OHLCData]:
        """Today's intraday bars (short TTL cache). Returns oldest→newest."""
        today = dt.date.today()
        return await self.get_bars(symbol, timeframe, today, today)

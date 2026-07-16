"""
scripts.scanners.vol_loop — VolumeScannerLoop

Continuous daily high-volume scanner. Replicates the TradingView
"nk-daily-high-volumes" screener. Uses a fixed rel-vol threshold of 3.0x.

Criteria:
    Price > $2  |  MCap > $300M  |  AvgVol30D > 500K
    RelVol10D > 3.0x
"""
from __future__ import annotations

import asyncio

import pytz

from core.adapters.event_bus import IEventBus
from core.entities.scanner_event import ScannerEvent, ScannerHit
from core.utils.log_helper import getLogger

from .base_loop import BaseScannerLoop

logger = getLogger(__name__)

_COLS = [
    "name", "description", "close", "change",
    "volume", "relative_volume_10d_calc",
    "market_cap_basic", "average_volume_30d_calc",
    "sector", "exchange",
]

_ET          = pytz.timezone("America/New_York")
_OPEN_H      = 9
_OPEN_M      = 30
_CLOSE_H     = 16
_CLOSE_M     = 0

def _smart_threshold() -> float:
    return 3.0


class VolumeScannerLoop(BaseScannerLoop):

    def __init__(
        self,
        bus: IEventBus,
        interval_seconds: int = 300,
        ttl_seconds: int = 3600,
        cache = None,
        min_price: float = 2.0,
        min_mktcap: float = 300_000_000,
        min_avgvol: float = 500_000,
        limit: int = 50,
    ) -> None:
        super().__init__(bus, interval_seconds, ttl_seconds, cache=cache)
        self._min_price  = min_price
        self._min_mktcap = min_mktcap
        self._min_avgvol = min_avgvol
        self._limit      = limit

    @property
    def name(self) -> str:
        return "volume"

    async def scan(self) -> list[ScannerHit]:
        from datetime import datetime
        now   = datetime.now(_ET)
        open_ = now.replace(hour=_OPEN_H, minute=_OPEN_M, second=0, microsecond=0)
        close = now.replace(hour=_CLOSE_H, minute=_CLOSE_M, second=0, microsecond=0)
        if now < open_ or now >= close:
            logger.debug("[volume] outside market hours — skipping scan")
            return []

        threshold = _smart_threshold()
        try:
            df = await asyncio.get_event_loop().run_in_executor(None, self._query, threshold)
        except Exception:
            logger.exception("[volume] query failed")
            return []
        hits = []
        for _, row in df.iterrows():
            hits.append(ScannerHit(
                event=ScannerEvent.SYMBOL_DETECTED,
                symbol=row["name"],
                scanner_name="volume",
                session="intraday",
                price=float(row.get("close") or 0),
                change_pct=float(row.get("change") or 0),
                exchange=row.get("exchange"),
                description=row.get("description"),
                sector=row.get("sector"),
                market_cap=float(row["market_cap_basic"]) if row.get("market_cap_basic") else None,
                volume=float(row["volume"]) if row.get("volume") else None,
                avg_vol_30d=float(row["average_volume_30d_calc"]) if row.get("average_volume_30d_calc") else None,
                rel_vol=float(row["relative_volume_10d_calc"]) if row.get("relative_volume_10d_calc") else None,
            ))
        return hits

    def _query(self, threshold: float):
        from tradingview_screener import Query, col

        _, df = (
            Query()
            .set_markets("america")
            .select(*_COLS)
            .where(
                col("close") > self._min_price,
                col("market_cap_basic") > self._min_mktcap,
                col("average_volume_30d_calc") > self._min_avgvol,
                col("relative_volume_10d_calc") > threshold,
            )
            .order_by("relative_volume_10d_calc", ascending=False)
            .limit(self._limit)
            .get_scanner_data()
        )
        return df

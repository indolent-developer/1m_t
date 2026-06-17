"""
scripts.scanners.spikes_loop — SpikeScannerLoop

Continuous intraday spikes scanner. Replicates the TradingView "nk-spikes"
screener and emits ScannerEvent.SYMBOL_DETECTED for every new symbol that
meets the criteria within the TTL window.

Criteria (defaults match the one-shot run_spikes_scanner.py):
    Price > $2  |  MCap > $300M  |  AvgVol30D > 500K
    |ChgFromOpen| > 2%  |  RelVol10D > 4x
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytz

from core.adapters.event_bus import IEventBus
from core.entities.scanner_event import ScannerEvent, ScannerHit
from core.utils.log_helper import getLogger

from .base_loop import BaseScannerLoop

_ET = pytz.timezone("America/New_York")

logger = getLogger(__name__)

_COLS = [
    "name", "description", "close", "change", "change_from_open",
    "volume", "relative_volume_10d_calc", "market_cap_basic",
    "average_volume_30d_calc", "sector", "exchange",
]


class SpikeScannerLoop(BaseScannerLoop):

    def __init__(
        self,
        bus: IEventBus,
        interval_seconds: int = 60,
        ttl_seconds: int = 3600,
        min_chg: float = 2.0,
        min_relvol: float = 4.0,
        min_price: float = 2.0,
        min_mktcap: float = 300_000_000,
        min_avgvol: float = 500_000,
        limit: int = 50,
    ) -> None:
        super().__init__(bus, interval_seconds, ttl_seconds)
        self._min_chg    = min_chg
        self._min_relvol = min_relvol
        self._min_price  = min_price
        self._min_mktcap = min_mktcap
        self._min_avgvol = min_avgvol
        self._limit      = limit

    @property
    def name(self) -> str:
        return "spikes"

    async def scan(self) -> list[ScannerHit]:
        now = datetime.now(_ET)
        open_ = now.replace(hour=9, minute=30, second=0, microsecond=0)
        close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
        if now < open_ or now >= close:
            logger.debug("[spikes] outside RTH — skipping scan")
            return []
        try:
            df = await asyncio.get_event_loop().run_in_executor(None, self._query)
        except Exception:
            logger.exception("[spikes] query failed")
            return []
        hits = []
        for _, row in df.iterrows():
            hits.append(ScannerHit(
                event=ScannerEvent.SYMBOL_DETECTED,
                symbol=row["name"],
                scanner_name="spikes",
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
                session_change_pct=float(row["change_from_open"]) if row.get("change_from_open") else None,
            ))
        return hits

    def _query(self):
        import pandas as pd
        from tradingview_screener import Query, col

        base = [
            col("close") > self._min_price,
            col("market_cap_basic") > self._min_mktcap,
            col("average_volume_30d_calc") > self._min_avgvol,
            col("relative_volume_10d_calc") > self._min_relvol,
        ]
        up   = Query().set_markets("america").select(*_COLS).where(*base, col("change_from_open") >  self._min_chg).order_by("relative_volume_10d_calc", ascending=False).limit(500).get_scanner_data()[1]
        down = Query().set_markets("america").select(*_COLS).where(*base, col("change_from_open") < -self._min_chg).order_by("relative_volume_10d_calc", ascending=False).limit(500).get_scanner_data()[1]
        frames = [f for f in [up, down] if not f.empty]
        df     = pd.concat(frames, ignore_index=True).drop_duplicates(subset="name") if frames else pd.DataFrame()
        return df.sort_values("change_from_open", ascending=False).head(self._limit)

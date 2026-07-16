"""
scripts.scanners.pre_mkt_loop — PreMarketScannerLoop

Continuous pre-market movers scanner. Replicates the TradingView
"nk-pre-market-movers" screener. Intended to run from ~04:00 ET to 09:29 ET
but the loop itself does not enforce session hours.

Criteria (defaults match run_pre_market_scanner.py):
    Price > $2  |  MCap > $300M  |  AvgVol30D > 500K
    |PreMktChg| > 5%  |  PreMktVol > 100K
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytz

from core.adapters.event_bus import IEventBus

_ET = pytz.timezone("America/New_York")
from core.entities.scanner_event import ScannerEvent, ScannerHit
from core.utils.log_helper import getLogger

from .base_loop import BaseScannerLoop

logger = getLogger(__name__)

_COLS = [
    "name", "description", "close", "volume",
    "market_cap_basic", "average_volume_30d_calc",
    "premarket_close", "premarket_change", "premarket_volume", "sector", "exchange",
]


class PreMarketScannerLoop(BaseScannerLoop):

    def __init__(
        self,
        bus: IEventBus,
        interval_seconds: int = 120,
        ttl_seconds: int = 3600,
        cache = None,
        min_pmchg: float = 5.0,
        min_price: float = 2.0,
        min_mktcap: float = 300_000_000,
        min_avgvol: float = 500_000,
        min_pmvol: float = 100_000,
        limit: int = 50,
    ) -> None:
        super().__init__(bus, interval_seconds, ttl_seconds, cache=cache)
        self._min_pmchg  = min_pmchg
        self._min_price  = min_price
        self._min_mktcap = min_mktcap
        self._min_avgvol = min_avgvol
        self._min_pmvol  = min_pmvol
        self._limit      = limit

    @property
    def name(self) -> str:
        return "pre_market"

    async def scan(self) -> list[ScannerHit]:
        now = datetime.now(_ET)
        rth_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now >= rth_open:
            logger.debug("[pre_market] RTH has started — skipping scan")
            return []
        try:
            df = await asyncio.get_event_loop().run_in_executor(None, self._query)
        except Exception:
            logger.exception("[pre_market] query failed")
            return []
        hits = []
        for _, row in df.iterrows():
            pm_vol  = float(row["premarket_volume"]) if row.get("premarket_volume") else None
            avg_vol = float(row["average_volume_30d_calc"]) if row.get("average_volume_30d_calc") else None
            rel_vol = round(pm_vol / avg_vol, 1) if pm_vol and avg_vol else None
            hits.append(ScannerHit(
                event=ScannerEvent.SYMBOL_DETECTED,
                symbol=row["name"],
                scanner_name="pre_market",
                session="pre",
                price=float(row.get("premarket_close") or row.get("close") or 0),
                change_pct=float(row.get("premarket_change") or 0),
                exchange=row.get("exchange"),
                description=row.get("description"),
                sector=row.get("sector"),
                market_cap=float(row["market_cap_basic"]) if row.get("market_cap_basic") else None,
                volume=pm_vol,
                avg_vol_30d=avg_vol,
                rel_vol=rel_vol,
            ))
        return hits

    def _query(self):
        import pandas as pd
        from tradingview_screener import Query, col

        base = [
            col("close") > self._min_price,
            col("market_cap_basic") > self._min_mktcap,
            col("average_volume_30d_calc") > self._min_avgvol,
            col("premarket_volume") > self._min_pmvol,
        ]
        up   = Query().set_markets("america").select(*_COLS).where(*base, col("premarket_change") >  self._min_pmchg).order_by("premarket_volume", ascending=False).limit(500).get_scanner_data()[1]
        down = Query().set_markets("america").select(*_COLS).where(*base, col("premarket_change") < -self._min_pmchg).order_by("premarket_volume", ascending=False).limit(500).get_scanner_data()[1]
        frames = [f for f in [up, down] if not f.empty]
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="name")
        return df.sort_values("premarket_change", ascending=False).head(self._limit)

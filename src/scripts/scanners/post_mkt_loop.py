"""
scripts.scanners.post_mkt_loop — PostMarketScannerLoop

Continuous post-market movers scanner. Replicates the TradingView
"nk-post-market-movers" screener. Intended to run from 16:00 ET to ~20:00 ET
but the loop itself does not enforce session hours.

Criteria (defaults match run_post_market_scanner.py):
    Price > $2  |  MCap > $300M  |  AvgVol30D > 500K
    |PostMktChg| > 3%  |  PostMktVol > 100K
"""
from __future__ import annotations

import asyncio

from core.adapters.event_bus import IEventBus
from core.entities.scanner_event import ScannerEvent, ScannerHit
from core.utils.log_helper import getLogger

from .base_loop import BaseScannerLoop

logger = getLogger(__name__)

_COLS = [
    "name", "description", "close", "change", "volume",
    "market_cap_basic", "average_volume_30d_calc",
    "postmarket_change", "postmarket_volume", "sector", "exchange",
]


class PostMarketScannerLoop(BaseScannerLoop):

    def __init__(
        self,
        bus: IEventBus,
        interval_seconds: int = 120,
        ttl_seconds: int = 3600,
        cache = None,
        min_pmchg: float = 3.0,
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
        return "post_market"

    async def scan(self) -> list[ScannerHit]:
        try:
            df = await asyncio.get_event_loop().run_in_executor(None, self._query)
        except Exception:
            logger.exception("[post_market] query failed")
            return []
        hits = []
        for _, row in df.iterrows():
            hits.append(ScannerHit(
                event=ScannerEvent.SYMBOL_DETECTED,
                symbol=row["name"],
                scanner_name="post_market",
                session="post",
                price=float(row.get("close") or 0),
                change_pct=float(row.get("change") or 0),
                exchange=row.get("exchange"),
                description=row.get("description"),
                sector=row.get("sector"),
                market_cap=float(row["market_cap_basic"]) if row.get("market_cap_basic") else None,
                volume=float(row["postmarket_volume"]) if row.get("postmarket_volume") else None,
                avg_vol_30d=float(row["average_volume_30d_calc"]) if row.get("average_volume_30d_calc") else None,
                session_change_pct=float(row["postmarket_change"]) if row.get("postmarket_change") else None,
            ))
        return hits

    def _query(self):
        import pandas as pd
        from tradingview_screener import Query, col

        base = [
            col("close") > self._min_price,
            col("market_cap_basic") > self._min_mktcap,
            col("average_volume_30d_calc") > self._min_avgvol,
            col("postmarket_volume") > self._min_pmvol,
        ]
        up   = Query().set_markets("america").select(*_COLS).where(*base, col("postmarket_change") >  self._min_pmchg).order_by("postmarket_volume", ascending=False).limit(500).get_scanner_data()[1]
        down = Query().set_markets("america").select(*_COLS).where(*base, col("postmarket_change") < -self._min_pmchg).order_by("postmarket_volume", ascending=False).limit(500).get_scanner_data()[1]
        df   = pd.concat([up, down], ignore_index=True).drop_duplicates(subset="name")
        return df.sort_values("postmarket_change", ascending=False).head(self._limit)

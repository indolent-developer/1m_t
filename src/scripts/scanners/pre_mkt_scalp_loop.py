"""
scripts.scanners.pre_mkt_scalp_loop — PreMarketScalpScannerLoop

Continuous pre-market scalp scanner. Replicates the TradingView
"nk-pre-market-scalph" screener. Intended to run from ~04:00 ET to 09:29 ET.

Criteria (defaults match screener):
    Price 2–30 USD  |  MCap > $300M  |  AvgVol30D > 500K
    PreMktChg > 10%  |  PreMktVol > 100K  |  Float 10M–100M shares
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
    "premarket_close", "premarket_change", "premarket_volume",
    "float_shares_outstanding", "sector", "exchange",
]


class PreMarketScalpScannerLoop(BaseScannerLoop):

    def __init__(
        self,
        bus: IEventBus,
        interval_seconds: int = 120,
        ttl_seconds: int = 3600,
        cache = None,
        min_pmchg: float = 10.0,
        min_price: float = 2.0,
        max_price: float = 30.0,
        min_mktcap: float = 300_000_000,
        min_avgvol: float = 500_000,
        min_pmvol: float = 100_000,
        min_float: float = 10_000_000,
        max_float: float = 100_000_000,
        limit: int = 50,
    ) -> None:
        super().__init__(bus, interval_seconds, ttl_seconds, cache=cache)
        self._min_pmchg  = min_pmchg
        self._min_price  = min_price
        self._max_price  = max_price
        self._min_mktcap = min_mktcap
        self._min_avgvol = min_avgvol
        self._min_pmvol  = min_pmvol
        self._min_float  = min_float
        self._max_float  = max_float
        self._limit      = limit

    @property
    def name(self) -> str:
        return "pre_market_scalp"

    async def scan(self) -> list[ScannerHit]:
        now = datetime.now(_ET)
        rth_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now >= rth_open:
            logger.debug("[pre_market_scalp] RTH has started — skipping scan")
            return []
        try:
            df = await asyncio.get_event_loop().run_in_executor(None, self._query)
        except Exception:
            logger.exception("[pre_market_scalp] query failed")
            return []
        hits = []
        for _, row in df.iterrows():
            hits.append(ScannerHit(
                event=ScannerEvent.SYMBOL_DETECTED,
                symbol=row["name"],
                scanner_name="pre_market_scalp",
                session="pre",
                price=float(row.get("premarket_close") or row.get("close") or 0),
                change_pct=float(row.get("premarket_change") or 0),
                exchange=row.get("exchange"),
                description=row.get("description"),
                sector=row.get("sector"),
                market_cap=float(row["market_cap_basic"]) if row.get("market_cap_basic") else None,
                volume=float(row["premarket_volume"]) if row.get("premarket_volume") else None,
                avg_vol_30d=float(row["average_volume_30d_calc"]) if row.get("average_volume_30d_calc") else None,
                float_shares=float(row["float_shares_outstanding"]) if row.get("float_shares_outstanding") else None,
            ))
        return hits

    def _query(self):
        from tradingview_screener import Query, col

        filters = [
            col("close").between(self._min_price, self._max_price),
            col("market_cap_basic") > self._min_mktcap,
            col("average_volume_30d_calc") > self._min_avgvol,
            col("premarket_volume") > self._min_pmvol,
            col("premarket_change") > self._min_pmchg,
            col("float_shares_outstanding").between(self._min_float, self._max_float),
        ]
        _, df = (
            Query()
            .set_markets("america")
            .select(*_COLS)
            .where(*filters)
            .order_by("premarket_volume", ascending=False)
            .limit(self._limit)
            .get_scanner_data()
        )
        return df

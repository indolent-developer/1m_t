"""Persist scanner results for archival and backreference."""
from __future__ import annotations

from core.utils.log_helper import getLogger
from datetime import datetime, timezone
from typing import Any

from infrastructure.repositories.base import BaseRepository

logger = getLogger(__name__)


class ScannerRepository(BaseRepository):

    async def save_results(self, rows: list[dict[str, Any]], scanner: str) -> None:
        """Bulk-insert scanner output rows. Each dict is one result row."""
        if not rows:
            return
        now = datetime.now(timezone.utc)
        try:
            pool = await self._pool()
            async with pool.acquire() as con:
                await con.executemany(
                    """
                    INSERT INTO scanner_results
                        (scanner, symbol, name, price, chg_pct, chg_1m_pct,
                         relvol, market_cap, rsi, sector, scanned_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    """,
                    [
                        (
                            scanner,
                            str(r.get("name", "")),
                            str(r.get("description", "")),
                            float(r["close"])                        if r.get("close")                      else None,
                            float(r["change"])                       if r.get("change")                     else None,
                            float(r["Perf.1M"])                      if r.get("Perf.1M")                    else None,
                            float(r["relative_volume_10d_calc"])     if r.get("relative_volume_10d_calc")   else None,
                            float(r["market_cap_basic"])             if r.get("market_cap_basic")           else None,
                            float(r["RSI"])                          if r.get("RSI")                        else None,
                            str(r.get("sector", "")),
                            now,
                        )
                        for r in rows
                    ],
                )
            logger.info("[ScannerRepository] Saved %d %s results", len(rows), scanner)
        except Exception as e:
            logger.warning("[ScannerRepository] save_results failed: %s", e)

    async def latest(self, scanner: str, limit: int = 50) -> list:
        try:
            return await self._fetch(
                """
                SELECT symbol, name, price, chg_pct, chg_1m_pct,
                       relvol, market_cap, rsi, sector, scanned_at
                FROM scanner_results
                WHERE scanner = $1
                ORDER BY scanned_at DESC
                LIMIT $2
                """,
                scanner, limit,
            )
        except Exception as e:
            logger.warning("[ScannerRepository] latest failed: %s", e)
            return []

"""
services.key_level_service

Computes key technical levels for a symbol from historical OHLC bars.

Levels computed
---------------
  prev_day_high / prev_day_low     — previous complete trading day
  pivot_pp, pivot_r1–r3, pivot_s1–s3  — standard floor pivot points from prev day OHLC
  daily_resistance / daily_support  — swing highs/lows from last 60 1D bars
  hourly_resistance / hourly_support — swing highs/lows from last 5 days of 1H bars
  hod_5min_ago / lod_5min_ago      — intraday HOD/LOD excluding the latest 5min bar

FMP bar ordering notes
----------------------
  Daily bars  → newest→oldest (FMP does NOT reverse them)
  1H / 5min   → oldest→newest (FMP reverses intraday bars)
"""
from __future__ import annotations

import asyncio
import datetime as dt
from core.utils.log_helper import getLogger
from dataclasses import dataclass, field

from core.entities.ohlc import OHLCData
from services.price_history_service import PriceHistoryService

logger = getLogger(__name__)

_CLUSTER_PCT          = 0.005   # 0.5% — levels within this band get merged
_SWING_WINDOW_DAILY   = 3       # bars each side for daily swing detection
_SWING_WINDOW_HOURLY  = 3       # bars each side for hourly swing detection (stricter → fewer levels)
_MAX_HOURLY_LEVELS    = 4       # max resistance + max support from 1H swings


# ── Output dataclass ──────────────────────────────────────────────────────────

@dataclass
class KeyLevels:
    symbol:       str
    computed_at:  dt.datetime

    prev_day_high: float = 0.0
    prev_day_low:  float = 0.0

    pivot_pp: float = 0.0
    pivot_r1: float = 0.0
    pivot_r2: float = 0.0
    pivot_r3: float = 0.0
    pivot_s1: float = 0.0
    pivot_s2: float = 0.0
    pivot_s3: float = 0.0

    hourly_resistance: list[float] = field(default_factory=list)
    hourly_support:    list[float] = field(default_factory=list)
    daily_resistance:  list[float] = field(default_factory=list)
    daily_support:     list[float] = field(default_factory=list)

    hod_5min_ago: float = 0.0
    lod_5min_ago: float = 0.0

    # ── Label helpers ─────────────────────────────────────────────────────────

    def labels(self) -> dict[float, str]:
        """level_value → human-readable label for logging and notifications."""
        r = round
        m: dict[float, str] = {}
        if self.prev_day_high: m[r(self.prev_day_high, 4)] = "Prev Day High"
        if self.prev_day_low:  m[r(self.prev_day_low,  4)] = "Prev Day Low"
        if self.pivot_pp: m[r(self.pivot_pp, 4)] = "Pivot PP"
        if self.pivot_r1: m[r(self.pivot_r1, 4)] = "Pivot R1"
        if self.pivot_r2: m[r(self.pivot_r2, 4)] = "Pivot R2"
        if self.pivot_r3: m[r(self.pivot_r3, 4)] = "Pivot R3"
        if self.pivot_s1: m[r(self.pivot_s1, 4)] = "Pivot S1"
        if self.pivot_s2: m[r(self.pivot_s2, 4)] = "Pivot S2"
        if self.pivot_s3: m[r(self.pivot_s3, 4)] = "Pivot S3"
        for v in self.hourly_resistance: m[r(v, 4)] = "1H Resistance"
        for v in self.hourly_support:    m[r(v, 4)] = "1H Support"
        for v in self.daily_resistance:  m[r(v, 4)] = "1D Resistance"
        for v in self.daily_support:     m[r(v, 4)] = "1D Support"
        if self.hod_5min_ago: m[r(self.hod_5min_ago, 4)] = "HOD"
        if self.lod_5min_ago: m[r(self.lod_5min_ago, 4)] = "LOD"
        return m

    def static_levels(self) -> list[float]:
        """All levels except HOD/LOD-5min-ago (stable across the trading day)."""
        candidates = [
            self.prev_day_high, self.prev_day_low,
            self.pivot_pp,
            self.pivot_r1, self.pivot_r2, self.pivot_r3,
            self.pivot_s1, self.pivot_s2, self.pivot_s3,
            *self.hourly_resistance, *self.hourly_support,
            *self.daily_resistance,  *self.daily_support,
        ]
        return sorted({round(v, 4) for v in candidates if v > 0})

    def hod_lod_levels(self) -> list[float]:
        """HOD and LOD from 5 minutes ago — refreshed every 5min by the monitor."""
        return [round(v, 4) for v in [self.hod_5min_ago, self.lod_5min_ago] if v > 0]

    def all_levels(self) -> list[float]:
        return sorted(set(self.static_levels() + self.hod_lod_levels()))


# ── Service ───────────────────────────────────────────────────────────────────

class KeyLevelService:
    """Computes KeyLevels for one or many symbols using PriceHistoryService."""

    def __init__(self, history: PriceHistoryService) -> None:
        self._history = history

    async def compute_levels(self, symbol: str) -> KeyLevels:
        daily_bars, hourly_bars, intraday_bars = await asyncio.gather(
            self._history.get_daily_bars(symbol, days=60),
            self._history.get_hourly_bars(symbol, days=5),
            self._history.get_intraday_bars(symbol),
        )

        kl = KeyLevels(symbol=symbol, computed_at=dt.datetime.now(dt.timezone.utc))

        # Daily bars come newest→oldest from FMP.
        # Index 0 = today/latest (possibly partial), index 1 = prev complete day.
        if len(daily_bars) >= 2:
            prev = daily_bars[1]
            kl.prev_day_high = prev.high
            kl.prev_day_low  = prev.low
            _compute_pivots(kl, prev)

        if daily_bars:
            # Reverse to chronological for swing detection
            kl.daily_resistance, kl.daily_support = _swing_levels(
                list(reversed(daily_bars)), _SWING_WINDOW_DAILY
            )

        # Hourly bars come oldest→newest (FMP reverses intraday).
        if hourly_bars:
            last_close = hourly_bars[-1].close if hourly_bars else 0
            res, sup = _swing_levels(hourly_bars, _SWING_WINDOW_HOURLY)
            # Keep only the N levels closest to current price (above/below respectively)
            kl.hourly_resistance = sorted(res, key=lambda v: abs(v - last_close))[:_MAX_HOURLY_LEVELS]
            kl.hourly_support    = sorted(sup, key=lambda v: abs(v - last_close))[:_MAX_HOURLY_LEVELS]

        # Intraday 5min bars: oldest→newest.
        # Exclude the last bar (currently open) so HOD/LOD reflects confirmed closes.
        if intraday_bars:
            confirmed = intraday_bars[:-1] if len(intraday_bars) > 1 else intraday_bars
            kl.hod_5min_ago = max(b.high for b in confirmed)
            kl.lod_5min_ago = min(b.low  for b in confirmed)

        hod_s = f"{kl.hod_5min_ago:.4f}" if kl.hod_5min_ago else "not-set"
        lod_s = f"{kl.lod_5min_ago:.4f}" if kl.lod_5min_ago else "not-set"
        logger.info(
            "KeyLevelService: %s — %d static levels + HOD/LOD %s/%s",
            symbol, len(kl.static_levels()), hod_s, lod_s,
        )
        return kl

    async def compute_levels_multi(self, symbols: list[str]) -> dict[str, KeyLevels]:
        results = await asyncio.gather(*[self.compute_levels(s) for s in symbols])
        return dict(zip(symbols, results))

    async def refresh_hod_lod(self, symbol: str, existing: KeyLevels) -> KeyLevels:
        """Re-fetch today's intraday bars and update HOD/LOD on an existing KeyLevels.

        Always bypasses the Redis cache so HOD/LOD reflects bars that closed in
        the last few minutes, not the 5-minute-old cached snapshot.
        """
        intraday_bars = await self._history.get_intraday_bars(symbol, force_fresh=True)
        if intraday_bars:
            confirmed = intraday_bars[:-1] if len(intraday_bars) > 1 else intraday_bars
            existing.hod_5min_ago = max(b.high for b in confirmed)
            existing.lod_5min_ago = min(b.low  for b in confirmed)
        return existing


# ── Private helpers ───────────────────────────────────────────────────────────

def _compute_pivots(kl: KeyLevels, bar: OHLCData) -> None:
    """Standard floor pivot points from a single OHLC bar."""
    h, l, c = bar.high, bar.low, bar.close
    pp = (h + l + c) / 3
    kl.pivot_pp = pp
    kl.pivot_r1 = 2 * pp - l
    kl.pivot_r2 = pp + (h - l)
    kl.pivot_r3 = h + 2 * (pp - l)
    kl.pivot_s1 = 2 * pp - h
    kl.pivot_s2 = pp - (h - l)
    kl.pivot_s3 = l - 2 * (h - pp)


def _swing_levels(
    bars: list[OHLCData],
    window: int,
) -> tuple[list[float], list[float]]:
    """
    Detect swing highs and lows using a symmetric window.
    A bar at index i is a swing high if its high is >= all bars within `window`
    positions on both sides. Nearby levels are clustered to avoid duplicates.
    """
    highs: list[float] = []
    lows:  list[float] = []
    n = len(bars)
    for i in range(window, n - window):
        hi = bars[i].high
        lo = bars[i].low
        if all(hi >= bars[i - j].high and hi >= bars[i + j].high for j in range(1, window + 1)):
            highs.append(hi)
        if all(lo <= bars[i - j].low  and lo <= bars[i + j].low  for j in range(1, window + 1)):
            lows.append(lo)
    return _cluster(highs), _cluster(lows)


def _cluster(levels: list[float]) -> list[float]:
    """Merge levels that are within _CLUSTER_PCT of each other into their mean."""
    if not levels:
        return []
    merged: list[float] = []
    group: list[float]  = [sorted(levels)[0]]
    for lvl in sorted(levels)[1:]:
        if group and (lvl - group[-1]) / group[-1] < _CLUSTER_PCT:
            group.append(lvl)
        else:
            merged.append(sum(group) / len(group))
            group = [lvl]
    merged.append(sum(group) / len(group))
    return [round(v, 4) for v in merged]

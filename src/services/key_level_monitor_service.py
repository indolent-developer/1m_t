"""
services.key_level_monitor_service

Monitors a set of symbols against automatically computed key technical levels
and emits PriceLevelEvent on the bus when price breaks, bounces, or rejects.

Key levels tracked
------------------
  Static (loaded once at start of day):
    • Previous day high / low
    • Floor pivot points: PP, R1, R2, R3, S1, S2, S3
    • 1H support & resistance (swing highs/lows, last 5 days)
    • 1D support & resistance (swing highs/lows, last 60 days)

  Dynamic (refreshed every 5 minutes):
    • HOD-5min-ago  — intraday high excluding the current 5min bar
    • LOD-5min-ago  — intraday low  excluding the current 5min bar

Break detection is handled by LevelTracker (one per level per symbol), the same
state machine used by PriceStateManager. Events emitted on the bus:
    LevelEvent.BREAK_ABOVE, BREAK_BELOW, BOUNCE, REJECTION, FALSE_BREAK

Downstream consumers:
    bus.subscribe(LevelEvent.BREAK_ABOVE, my_handler)
    bus.subscribe_all(my_handler)

Usage
-----
    history = PriceHistoryService(fmp_fetcher, redis_cache)
    monitor = KeyLevelMonitorService(
        symbols=["AAPL", "TSLA", "NVDA"],
        bus=bus,
        history_service=history,
        fetcher=fmp_fetcher,
    )
    await monitor.start()
    ...
    await monitor.stop()
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import datetime as dt
from datetime import datetime, timezone
from core.utils.log_helper import getLogger
from typing import Optional

from adapters.brokers.entities.broker_event import BrokerEvent
from adapters.brokers.entities.broker_event_payload import BrokerEventPayload
from core.adapters.base_subscriber import BaseSubscriber
from core.adapters.event_bus import IEventBus
from core.entities.level_event import LevelEvent, PriceLevelEvent
from core.entities.market_data import PriceTick
from core.entities.time_frame import TimeFrame
from services.indicator_provider import IndicatorProvider
from services.key_level_service import KeyLevelService, KeyLevels
from services.level_tracker import LevelTracker, effective_atr, DEFAULT_MIN_ATR_PCT
from services.price_history_service import PriceHistoryService

logger = getLogger(__name__)

_BOUNCE_TIMEOUT_SECS  = 300   # cancel pending confirmation after 5 min
_ST_LENGTH            = 7
_ST_MULTIPLIER        = 3.0

# Events that require two-stage bounce confirmation before being emitted
_PARK_EVENTS = {
    ("LOD", LevelEvent.BOUNCE),
    ("HOD", LevelEvent.BREAK_BELOW),
    ("LOD", LevelEvent.BREAK_ABOVE),   # "reversed LOD" — same reversal gate as BOUNCE
}


@dataclass
class _PendingBounce:
    evt:          PriceLevelEvent
    direction:    int    # +1 = price must go UP (LOD), -1 = price must go DOWN (HOD)
    target_price: float  # 1% from zone boundary in the break direction
    st_at_bounce: Optional[int]   # supertrend direction when bounce was first detected
    started:      datetime
    zone_lo:      float
    zone_hi:      float


class KeyLevelMonitorService(BaseSubscriber):
    """
    Subscribes to QUOTE_UPDATE ticks and emits PriceLevelEvent when price
    meaningfully interacts with any computed key level.

    Static levels (prev day H/L, pivots, 1H/1D S&R) are loaded once at startup.
    HOD/LOD-5min-ago trackers are replaced every `hod_lod_refresh_seconds`.
    """

    def __init__(
        self,
        symbols: list[str],
        bus: IEventBus,
        history_service: PriceHistoryService,
        # ATR band timeframe for LevelTracker zone width
        band_timeframe: TimeFrame = TimeFrame.MINUTE_5,
        band_mult: float = 0.3,
        atr_period: int = 14,
        min_atr_pct: float = DEFAULT_MIN_ATR_PCT,
        # LevelTracker confirmation params
        dwell_seconds: int = 120,
        break_confirm_seconds: int = 30,
        break_confirm_ticks: int = 2,
        break_max_zone_dwell_seconds: int = 300,
        false_break_window_seconds: int = 180,
        false_break_confirm_seconds: int = 15,
        false_break_confirm_ticks: int = 2,
        # HOD/LOD refresh cadence
        hod_lod_refresh_seconds: int = 300,
    ) -> None:
        super().__init__(bus)
        self._symbols = list(symbols)
        self._kl_service = KeyLevelService(history_service)
        self._provider   = IndicatorProvider(
            history=history_service,
            symbols=self._symbols,
            timeframe=band_timeframe,
            default_period=atr_period,
        )
        self._provider_1m = IndicatorProvider(
            history=history_service,
            symbols=self._symbols,
            timeframe=TimeFrame.MINUTE_1,
            default_period=atr_period,
        )
        self._tracker_cfg = dict(
            band_mult=band_mult,
            atr_period=atr_period,
            min_atr_pct=min_atr_pct,
            dwell_seconds=dwell_seconds,
            break_confirm_seconds=break_confirm_seconds,
            break_confirm_ticks=break_confirm_ticks,
            break_max_zone_dwell_seconds=break_max_zone_dwell_seconds,
            false_break_window_seconds=false_break_window_seconds,
            false_break_confirm_seconds=false_break_confirm_seconds,
            false_break_confirm_ticks=false_break_confirm_ticks,
        )
        self._hod_lod_refresh = hod_lod_refresh_seconds

        # Populated in start()
        self._static_trackers:  dict[str, list[LevelTracker]] = {}
        self._hod_lod_trackers: dict[str, list[LevelTracker]] = {}
        self._key_levels:       dict[str, KeyLevels]          = {}
        self._label_map:        dict[str, dict[float, str]]   = {}

        self._hod_lod_task:  Optional[asyncio.Task] = None
        self._provider_task: Optional[asyncio.Task] = None
        self._add_symbol_lock = asyncio.Lock()
        self._adding_symbols: set[str] = set()

        # level_value → datetime when price last entered that level's zone (HOD/LOD only)
        self._level_touch_times: dict[str, dict[float, datetime]] = {}
        # Two-stage bounce confirmation: symbol → pending confirmation
        self._pending_bounces: dict[str, _PendingBounce] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info(
            "KeyLevelMonitorService: loading ATR data + key levels for %d symbols...",
            len(self._symbols),
        )
        await self._provider.load()
        await self._load_all_levels()

        n_static  = sum(len(v) for v in self._static_trackers.values())
        n_hod_lod = sum(len(v) for v in self._hod_lod_trackers.values())
        logger.info(
            "KeyLevelMonitorService: ready — %d static + %d HOD/LOD trackers across %d symbols",
            n_static, n_hod_lod, len(self._symbols),
        )

        self._subscribe(BrokerEvent.QUOTE_UPDATE, self._on_tick)
        self._hod_lod_task  = asyncio.create_task(self._hod_lod_refresh_loop())
        self._provider_task = asyncio.create_task(self._provider.refresh_loop())
        # _provider_1m is loaded on-demand only when a bounce is parked

    async def stop(self) -> None:
        self.detach()
        for task in (self._hod_lod_task, self._provider_task):
            if task:
                task.cancel()
        logger.info("KeyLevelMonitorService: stopped")

    # ── Dynamic symbol management ─────────────────────────────────────────────

    async def add_symbol(self, symbol: str) -> None:
        if symbol in self._symbols or symbol in self._adding_symbols:
            return
        async with self._add_symbol_lock:
            if symbol in self._symbols or symbol in self._adding_symbols:
                return
            self._adding_symbols.add(symbol)
        try:
            levels = await self._kl_service.compute_levels(symbol)
            self._key_levels[symbol]       = levels
            self._static_trackers[symbol]  = self._make_trackers(symbol, levels.static_levels())
            self._hod_lod_trackers[symbol] = self._make_trackers(symbol, levels.hod_lod_levels())
            self._label_map[symbol]        = levels.labels()
            self._provider._symbols.append(symbol)
            await self._provider.load_one(symbol)
            self._symbols.append(symbol)
            logger.info("KeyLevelMonitorService: added %s (%d static levels)", symbol, len(levels.static_levels()))
        finally:
            self._adding_symbols.discard(symbol)

    async def remove_symbol(self, symbol: str) -> None:
        if symbol not in self._symbols:
            return
        self._symbols.remove(symbol)
        self._static_trackers.pop(symbol, None)
        self._hod_lod_trackers.pop(symbol, None)
        self._key_levels.pop(symbol, None)
        self._label_map.pop(symbol, None)
        if symbol in self._provider._symbols:
            self._provider._symbols.remove(symbol)
        logger.info("KeyLevelMonitorService: removed %s", symbol)

    # ── Accessors ─────────────────────────────────────────────────────────────

    def get_levels(self, symbol: str) -> Optional[KeyLevels]:
        return self._key_levels.get(symbol)

    def get_label(self, symbol: str, level_value: float) -> str:
        return self._label_map.get(symbol, {}).get(round(level_value, 4), "unknown")

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _load_all_levels(self) -> None:
        levels_map = await self._kl_service.compute_levels_multi(self._symbols)
        for symbol, levels in levels_map.items():
            self._key_levels[symbol]       = levels
            self._static_trackers[symbol]  = self._make_trackers(symbol, levels.static_levels())
            self._hod_lod_trackers[symbol] = self._make_trackers(symbol, levels.hod_lod_levels())
            self._label_map[symbol]        = levels.labels()

    def _make_trackers(self, symbol: str, levels: list[float]) -> list[LevelTracker]:
        return [
            LevelTracker(
                symbol=symbol,
                level=lvl,
                indicator_provider=self._provider,
                **self._tracker_cfg,
            )
            for lvl in levels
        ]

    async def _on_tick(self, payload: BrokerEventPayload) -> None:
        if not isinstance(payload.data, PriceTick):
            return
        tick   = payload.data
        now    = (
            datetime.fromtimestamp(tick.timestamp / 1000, tz=timezone.utc)
            if tick.timestamp else datetime.now(timezone.utc)
        )
        labels = self._label_map.get(tick.symbol, {})

        # ── Track HOD/LOD zone entries ────────────────────────────────────────
        self._update_hod_lod_touch(tick.symbol, tick.price, now)

        # ── Check any pending bounce confirmation for this symbol ─────────────
        confirmed = await self._check_pending_bounce(tick.symbol, tick.price, now)
        if confirmed:
            _log_event(confirmed, labels)
            await self._bus.emit(confirmed)

        # ── Run trackers ──────────────────────────────────────────────────────
        trackers = (
            self._static_trackers.get(tick.symbol, []) +
            self._hod_lod_trackers.get(tick.symbol, [])
        )
        for tracker in trackers:
            for evt in tracker.update(tick):
                evt.label = labels.get(round(evt.level, 4), "")
                if not _event_makes_sense(evt.event, evt.label):
                    continue
                if evt.label in ("HOD", "LOD"):
                    evt.level_touched_at = self._level_touch_times.get(tick.symbol, {}).get(round(evt.level, 4))

                if (evt.label, evt.event) in _PARK_EVENTS:
                    await self._park_bounce(tick.symbol, evt)
                    _log_event(evt, labels)   # log but hold — emit only after confirmation
                else:
                    _log_event(evt, labels)
                    await self._bus.emit(evt)

    def _update_hod_lod_touch(self, symbol: str, price: float, now: datetime) -> None:
        if not price:
            return
        atr = self._provider.compute(symbol, "atr", length=self._tracker_cfg.get("atr_period", 14))
        if not atr:
            return
        atr   = effective_atr(atr, price, self._tracker_cfg.get("min_atr_pct", DEFAULT_MIN_ATR_PCT))
        band  = atr * self._tracker_cfg.get("band_mult", 0.3)
        touch = self._level_touch_times.setdefault(symbol, {})
        for tracker in self._hod_lod_trackers.get(symbol, []):
            if tracker.level - band <= price <= tracker.level + band:
                touch[round(tracker.level, 4)] = now

    async def _park_bounce(self, symbol: str, evt: PriceLevelEvent) -> None:
        direction    = 1 if evt.label == "LOD" else -1
        target_price = evt.zone_lo * 1.01 if direction == 1 else evt.zone_hi * 0.99
        await self._provider_1m.load_one(symbol)   # fresh 1m bars only when needed
        st           = self._provider_1m.supertrend_direction(symbol, _ST_LENGTH, _ST_MULTIPLIER)
        self._pending_bounces[symbol] = _PendingBounce(
            evt=evt, direction=direction, target_price=target_price,
            st_at_bounce=st, started=datetime.now(timezone.utc),
            zone_lo=evt.zone_lo, zone_hi=evt.zone_hi,
        )
        logger.debug(
            "KeyLevelMonitorService: %s parked %s [%s] — target=%.4f  st_at_bounce=%s",
            symbol, evt.event.value, evt.label, target_price, st,
        )

    async def _check_pending_bounce(
        self, symbol: str, price: float, now: datetime
    ) -> Optional[PriceLevelEvent]:
        pb = self._pending_bounces.get(symbol)
        if pb is None:
            return None

        elapsed = (now - pb.started).total_seconds()

        # ── Cancel: timeout ───────────────────────────────────────────────────
        if elapsed > _BOUNCE_TIMEOUT_SECS:
            del self._pending_bounces[symbol]
            logger.debug("KeyLevelMonitorService: %s pending bounce timeout", symbol)
            return None

        # ── Cancel: price returned to zone ────────────────────────────────────
        if pb.zone_lo <= price <= pb.zone_hi:
            del self._pending_bounces[symbol]
            logger.debug("KeyLevelMonitorService: %s pending bounce cancelled (price back in zone)", symbol)
            return None

        # ── Cancel: price drifted too far from zone (stale / already exhausted) ─
        atr = self._provider.compute(symbol, "atr", length=self._tracker_cfg.get("atr_period", 14)) or 0
        if atr > 0:
            zone_dist = (
                (price - pb.zone_hi) if pb.direction == 1   # LOD bounce: price above zone
                else (pb.zone_lo - price)                    # HOD bounce: price below zone
            )
            if zone_dist > 5 * atr:
                del self._pending_bounces[symbol]
                logger.debug(
                    "KeyLevelMonitorService: %s pending bounce cancelled (%.4f is %.1f ATRs from zone)",
                    symbol, price, zone_dist / atr,
                )
                return None

        # ── Check gates ───────────────────────────────────────────────────────
        price_ok = (
            (pb.direction == 1  and price >= pb.target_price) or
            (pb.direction == -1 and price <= pb.target_price)
        )
        if not price_ok:
            return None

        await self._provider_1m.load_one(symbol)   # refresh 1m so supertrend is current
        st_now = self._provider_1m.supertrend_direction(symbol, _ST_LENGTH, _ST_MULTIPLIER)

        # Normal confirmation: supertrend has flipped to agree with the bounce direction.
        st_ok = (
            st_now is not None and
            pb.st_at_bounce is not None and
            st_now != pb.st_at_bounce and
            st_now == pb.direction
        )

        # Strong-price bypass: if price has already moved 2× ATR beyond the target,
        # the bounce is confirmed by price action alone — don't wait for the trend flip.
        price_gap    = abs(price - pb.target_price)
        strong_price = atr > 0 and price_gap >= 2 * atr

        if st_ok or strong_price:
            del self._pending_bounces[symbol]
            reason = "st_flip" if st_ok else "strong_price"
            logger.info(
                "KeyLevelMonitorService: %s bounce confirmed (%s) — %s [%s]  price=%.4f  st=%s",
                symbol, reason, pb.evt.event.value, pb.evt.label, price, st_now,
            )
            return pb.evt

        return None


    async def _hod_lod_refresh_loop(self) -> None:
        import pytz
        _ET = pytz.timezone("America/New_York")
        _last_session_date: Optional[dt.date] = None

        while True:
            await asyncio.sleep(self._hod_lod_refresh)

            now_et       = datetime.now(_ET)
            session_date = now_et.date()

            # Force-reset all HOD/LOD trackers at the start of each new ET session.
            # This prevents overnight dwell state (17h+ zones) from carrying into
            # the next day's open and delaying/corrupting break alerts.
            new_session = _last_session_date is not None and session_date != _last_session_date
            _last_session_date = session_date

            for symbol in list(self._symbols):
                try:
                    existing = self._key_levels.get(symbol)
                    if existing is None:
                        continue

                    old_hod = existing.hod_5min_ago
                    old_lod = existing.lod_5min_ago

                    updated = await self._kl_service.refresh_hod_lod(symbol, existing)

                    hod_moved = abs(updated.hod_5min_ago - old_hod) > 0.001
                    lod_moved = abs(updated.lod_5min_ago - old_lod) > 0.001

                    if not hod_moved and not lod_moved and not new_session:
                        # Level unchanged — keep existing trackers so in-progress
                        # dwell/confirmation state is not thrown away.
                        logger.debug(
                            "KeyLevelMonitorService: %s HOD/LOD unchanged (%.4f / %.4f)",
                            symbol, updated.hod_5min_ago, updated.lod_5min_ago,
                        )
                        continue

                    self._hod_lod_trackers[symbol] = self._make_trackers(
                        symbol, updated.hod_lod_levels()
                    )
                    self._label_map[symbol] = updated.labels()
                    hod_str = f"{old_hod:.4f} → {updated.hod_5min_ago:.4f}" if hod_moved else f"{old_hod:.4f}"
                    lod_str = f"{old_lod:.4f} → {updated.lod_5min_ago:.4f}" if lod_moved else f"{old_lod:.4f}"
                    logger.info(
                        "KeyLevelMonitorService: %s HOD/LOD updated  HOD=%s  LOD=%s",
                        symbol, hod_str, lod_str,
                    )
                except Exception as exc:
                    logger.warning(
                        "KeyLevelMonitorService: HOD/LOD refresh failed for %s: %s",
                        symbol, exc,
                    )


# ── Event semantics filter ────────────────────────────────────────────────────

_SUPPORT_LABELS = {
    "LOD", "Prev Day Low",
    "1H Support", "1D Support",
    "Pivot S1", "Pivot S2", "Pivot S3",
}
_RESISTANCE_LABELS = {
    "HOD", "Prev Day High",
    "1H Resistance", "1D Resistance",
    "Pivot R1", "Pivot R2", "Pivot R3",
}


def _event_makes_sense(event: LevelEvent, label: str) -> bool:
    """Suppress semantically wrong events: rejection at support, bounce at resistance."""
    if label in _SUPPORT_LABELS and event == LevelEvent.REJECTION:
        return False
    if label in _RESISTANCE_LABELS and event == LevelEvent.BOUNCE:
        return False
    return True


# ── Logging helper ────────────────────────────────────────────────────────────

def _log_event(evt: PriceLevelEvent, labels: dict[float, str]) -> None:
    label = labels.get(round(evt.level, 4), "")
    label_str = f"  [{label}]" if label else ""
    extra = f"  orig={evt.original_break.value}" if evt.original_break else ""
    dwell = f"  dwell={evt.dwell_seconds:.0f}s"  if evt.dwell_seconds  else ""
    logger.info(
        "%-13s  %s  level=%.4f%s  price=%.4f  zone=[%.4f–%.4f]  "
        "atr=%.4f  convincing=%s%s%s",
        f"[{evt.event.value}]", evt.symbol, evt.level, label_str,
        evt.price, evt.zone_lo, evt.zone_hi,
        evt.atr, evt.convincing, dwell, extra,
    )

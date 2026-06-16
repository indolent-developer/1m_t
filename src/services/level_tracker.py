from __future__ import annotations

from core.utils.log_helper import getLogger
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Optional

from core.entities.level_event import LevelEvent, PriceLevelEvent
from core.entities.market_data import PriceTick

if TYPE_CHECKING:
    from services.indicator_provider import IndicatorProvider

logger = getLogger(__name__)

_CONVICTION_MULT = 0.5


class _Pos(str, Enum):
    UNKNOWN = "unknown"
    ABOVE   = "above"
    IN_ZONE = "in_zone"
    BELOW   = "below"


@dataclass
class _Break:
    kind: LevelEvent
    time: datetime


@dataclass
class _PendingBreak:
    """Price exited zone on break side; waiting for dwell outside to confirm."""
    kind:    LevelEvent
    started: datetime
    zone_lo: float     # snapshotted at state entry — ATR drift cannot cancel this
    zone_hi: float
    ticks:   int = 1


@dataclass
class _PendingFalseBreak:
    """Price returned to zone after a confirmed break; waiting for dwell inside to call it false."""
    orig_break: LevelEvent
    started:    datetime
    zone_lo:    float  # snapshotted at re-entry
    zone_hi:    float
    ticks:      int = 1


class LevelTracker:
    """
    State machine for one (symbol, level) pair.

    update(tick) → list[PriceLevelEvent]

    Zone:          [level - atr*band_mult,  level + atr*band_mult]
    Convincing:    price beyond level ± atr*0.5

    Bounce:        came from ABOVE, spent ≥ dwell_seconds in zone, returned ABOVE
    Rejection:     came from BELOW, spent ≥ dwell_seconds in zone, returned BELOW

    Break:         exited zone on break side AND stayed outside for ≥ break_confirm_seconds
                   AND ≥ break_confirm_ticks; returns before that → silently cancelled
    FalseBreak:    after a confirmed break, price returned to zone within false_break_window_seconds
                   AND stayed inside for ≥ false_break_confirm_seconds AND ≥ false_break_confirm_ticks;
                   exits zone again before those thresholds → brief dip, break still stands

    Time source:   tick.timestamp (Unix ms); falls back to wall clock only when tick carries no ts.
    Zone stability: zone is snapshotted at the start of each pending state so ATR drift cannot
                   generate phantom transitions while a dwell is in progress.
    """

    def __init__(
        self,
        symbol: str,
        level: float,
        indicator_provider: IndicatorProvider,
        band_mult: float = 0.3,
        dwell_seconds: int = 120,
        atr_period: int = 14,
        # ── break confirmation (outside zone) ─────────────────────────────────
        break_confirm_seconds: int = 60,
        break_confirm_ticks: int = 3,
        break_max_zone_dwell_seconds: int = 300,   # if price lingered in zone longer than this, exit is ambiguous
        # ── false-break detection (back inside zone after confirmed break) ─────
        false_break_window_seconds: int = 180,
        false_break_confirm_seconds: int = 15,
        false_break_confirm_ticks: int = 2,
    ) -> None:
        self.symbol  = symbol
        self.level   = level
        self._indp   = indicator_provider
        self._bmult  = band_mult
        self._dwell  = dwell_seconds
        self._atrp   = atr_period

        self._confirm_secs   = break_confirm_seconds
        self._confirm_ticks  = break_confirm_ticks
        self._break_max_dwell = break_max_zone_dwell_seconds
        self._fb_window     = false_break_window_seconds
        self._fb_conf_secs  = false_break_confirm_seconds
        self._fb_conf_ticks = false_break_confirm_ticks

        self._pos:     _Pos                        = _Pos.UNKNOWN
        self._from:    Optional[_Pos]              = None
        self._entry:   Optional[datetime]          = None
        self._pending: Optional[_PendingBreak]     = None
        self._pfb:     Optional[_PendingFalseBreak] = None
        self._break:   Optional[_Break]            = None

    # ── public ────────────────────────────────────────────────────────────────

    def update(self, tick: PriceTick) -> list[PriceLevelEvent]:
        atr = self._indp.compute(self.symbol, "atr", length=self._atrp)
        if not atr:
            return []

        # Use tick's own timestamp so replays and delayed feeds produce correct dwells.
        now = (
            datetime.fromtimestamp(tick.timestamp / 1000, tz=timezone.utc)
            if tick.timestamp else
            datetime.now(timezone.utc)
        )

        # Live ATR zone — used for reporting and for starting new pending states.
        band    = atr * self._bmult
        zone_lo = self.level - band
        zone_hi = self.level + band
        price   = tick.price

        # While tracking a pending state, classify price against the snapshotted zone
        # so ATR expansion/contraction cannot phantom-cancel an in-progress dwell.
        if self._pending is not None:
            eff_lo, eff_hi = self._pending.zone_lo, self._pending.zone_hi
        elif self._pfb is not None:
            eff_lo, eff_hi = self._pfb.zone_lo, self._pfb.zone_hi
        else:
            eff_lo, eff_hi = zone_lo, zone_hi

        new_pos = (
            _Pos.ABOVE   if price > eff_hi else
            _Pos.BELOW   if price < eff_lo else
            _Pos.IN_ZONE
        )

        def make(kind: LevelEvent, dwell: float = 0.0, orig: Optional[LevelEvent] = None) -> PriceLevelEvent:
            convincing = (
                price >= self.level + _CONVICTION_MULT * atr if kind == LevelEvent.BREAK_ABOVE else
                price <= self.level - _CONVICTION_MULT * atr if kind == LevelEvent.BREAK_BELOW else
                True
            )
            return PriceLevelEvent(
                event=kind, symbol=self.symbol, level=self.level,
                price=price, zone_lo=zone_lo, zone_hi=zone_hi,
                atr=atr, convincing=convincing, tick_source=tick.source,
                timestamp=now, dwell_seconds=dwell, original_break=orig,
            )

        # ── same position ─────────────────────────────────────────────────────
        if new_pos == self._pos:
            events  = self._tick_break_pending(now, make)
            events += self._tick_false_break_pending(now, make)
            self._expire_break(now)
            return events

        old       = self._pos
        self._pos = new_pos
        events: list[PriceLevelEvent] = []

        # ── silent init ───────────────────────────────────────────────────────
        if old == _Pos.UNKNOWN:
            if new_pos == _Pos.IN_ZONE:
                self._from  = _Pos.UNKNOWN
                self._entry = now
            return []

        # ── entering zone ─────────────────────────────────────────────────────
        if new_pos == _Pos.IN_ZONE:
            self._pending = None  # unconfirmed break cancelled silently
            if self._break and self._pfb is None:
                elapsed = (now - self._break.time).total_seconds()
                if elapsed <= self._fb_window:
                    self._pfb = _PendingFalseBreak(self._break.kind, now, zone_lo, zone_hi, ticks=1)
                else:
                    self._break = None
            self._from  = old
            self._entry = now
            return events

        # ── exiting zone ──────────────────────────────────────────────────────
        if old == _Pos.IN_ZONE:
            self._pfb = None  # brief dip back into zone — break still stands

            dwell     = (now - self._entry).total_seconds() if self._entry else 0.0
            came_from = self._from
            self._from  = None
            self._entry = None

            if new_pos == _Pos.ABOVE:
                if came_from == _Pos.ABOVE and dwell >= self._dwell:
                    events.append(make(LevelEvent.BOUNCE, dwell=dwell))
                elif came_from == _Pos.BELOW and dwell <= self._break_max_dwell:
                    self._pending = _PendingBreak(LevelEvent.BREAK_ABOVE, now, zone_lo, zone_hi, ticks=1)
                # came_from UNKNOWN, or dwell too long (consolidation) → silent no-op

            elif new_pos == _Pos.BELOW:
                if came_from == _Pos.BELOW and dwell >= self._dwell:
                    events.append(make(LevelEvent.REJECTION, dwell=dwell))
                elif came_from == _Pos.ABOVE and dwell <= self._break_max_dwell:
                    self._pending = _PendingBreak(LevelEvent.BREAK_BELOW, now, zone_lo, zone_hi, ticks=1)
                # came_from UNKNOWN, or dwell too long (consolidation) → silent no-op

            return events

        # ── gap through zone (price jumped directly ABOVE↔BELOW) ─────────────
        self._pending = None
        self._pfb     = None
        if old == _Pos.ABOVE and new_pos == _Pos.BELOW:
            self._pending = _PendingBreak(LevelEvent.BREAK_BELOW, now, zone_lo, zone_hi, ticks=1)
        elif old == _Pos.BELOW and new_pos == _Pos.ABOVE:
            self._pending = _PendingBreak(LevelEvent.BREAK_ABOVE, now, zone_lo, zone_hi, ticks=1)

        return events

    # ── Pending-break confirmation (outside zone) ─────────────────────────────

    def _tick_break_pending(self, now: datetime, make) -> list[PriceLevelEvent]:
        if not self._pending:
            return []
        self._pending.ticks += 1
        elapsed = (now - self._pending.started).total_seconds()
        if elapsed >= self._confirm_secs and self._pending.ticks >= self._confirm_ticks:
            kind = self._pending.kind
            self._pending = None
            self._break   = _Break(kind, now)
            return [make(kind, dwell=elapsed)]
        return []

    # ── Pending-false-break confirmation (inside zone) ────────────────────────

    def _tick_false_break_pending(self, now: datetime, make) -> list[PriceLevelEvent]:
        if not self._pfb:
            return []
        if self._break and (now - self._break.time).total_seconds() > self._fb_window:
            self._pfb   = None
            self._break = None
            return []
        self._pfb.ticks += 1
        elapsed = (now - self._pfb.started).total_seconds()
        if elapsed >= self._fb_conf_secs and self._pfb.ticks >= self._fb_conf_ticks:
            orig = self._pfb.orig_break
            self._pfb   = None
            self._break = None
            return [make(LevelEvent.FALSE_BREAK, dwell=elapsed, orig=orig)]
        return []

    # ── Expiry ────────────────────────────────────────────────────────────────

    def _expire_break(self, now: datetime) -> None:
        if self._break and (now - self._break.time).total_seconds() > self._fb_window:
            self._break = None
            self._pfb   = None

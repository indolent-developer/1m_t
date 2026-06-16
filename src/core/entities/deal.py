"""
core.entities.deal — Deal, OrderLeg, TpLevel, DealState.

A Deal represents a complete round-trip position: one or more entry fills
(averaging in) plus one or more exit fills (scaling out). It is the canonical
unit of position tracking for both backtest and live trading.

While the deal is OPEN, `position` holds the live broker snapshot for the
remaining qty — giving access to unrealized PnL and current market value.
When the deal is fully CLOSED, position is cleared and realized_pnl is final.

Key invariant: remaining_qty shrinks with each exit leg. When it reaches zero
the deal transitions to CLOSED and realized_pnl is complete.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum

from core.entities.broker_entities import TradeSide
from core.entities.position_types import Position


class DealState(str, Enum):
    PENDING = "PENDING"
    OPEN    = "OPEN"
    CLOSED  = "CLOSED"


@dataclass
class OrderLeg:
    order_id:   str
    qty:        float
    fill_price: float
    filled_at:  dt.datetime
    commission: float
    trigger:    str         # "SIGNAL" | "TP1" | "TP2" | "SL" | "MANUAL" | "TIMEOUT"
    bar_index:  int = -1


@dataclass
class TpLevel:
    price:    float
    qty_pct:  float                  # e.g. 0.4 = close 40% of remaining at this TP
    order_id: str | None = None
    hit_at:   dt.datetime | None = None


@dataclass
class Deal:
    """
    A round-trip trade tracked through its full lifecycle.

    Entry fills accumulate into entry_legs; each updates avg_entry_price and
    total_qty / remaining_qty.

    Exit fills accumulate into exit_legs; each reduces remaining_qty and
    increments realized_pnl. When remaining_qty hits zero the state becomes
    CLOSED and position is cleared.

    While OPEN, attach the broker Position to `position` so unrealized_pnl
    and current market_value are always reachable from the deal.
    """
    deal_id:   str
    symbol:    str
    direction: str          # TradeSide.LONG.value | TradeSide.SHORT.value

    entry_legs: list[OrderLeg] = field(default_factory=list)
    exit_legs:  list[OrderLeg] = field(default_factory=list)

    total_qty:       float = 0.0
    remaining_qty:   float = 0.0
    avg_entry_price: float = 0.0
    avg_exit_price:  float = 0.0

    realized_pnl: float = 0.0

    stop_loss:          float | None = None
    take_profit_levels: list[TpLevel] = field(default_factory=list)

    state:     DealState      = DealState.PENDING
    positions: list[Position] = field(default_factory=list)  # live broker snapshots

    # Proportional entry commission still to be allocated to future exits.
    _remaining_entry_commission: float = field(default=0.0, init=False, repr=False)

    # ── Unrealized PnL — aggregated across all open positions ────────────────
    # Capital.com: one deal → one position (their dealId maps 1-to-1).
    # IBKR: one netted position per symbol; list will have 0 or 1 entry.
    # Both brokers work — IBKR just always has at most one item in the list.

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions)

    @property
    def market_value(self) -> float:
        return sum(p.market_value for p in self.positions)

    @property
    def total_pnl(self) -> float:
        """Realized + unrealized — full picture while the deal is open."""
        return self.realized_pnl + self.unrealized_pnl

    # ── Mutation helpers ──────────────────────────────────────────────────────

    def add_entry(self, leg: OrderLeg) -> None:
        """Record an entry fill. Safe to call multiple times for averaging-in."""
        total_cost = self.avg_entry_price * self.total_qty + leg.fill_price * leg.qty
        self.total_qty     += leg.qty
        self.remaining_qty += leg.qty
        self.avg_entry_price = total_cost / self.total_qty
        self._remaining_entry_commission += leg.commission
        self.entry_legs.append(leg)
        self.state = DealState.OPEN

    def add_exit(self, leg: OrderLeg) -> tuple[float, float]:
        """
        Record an exit fill. Updates remaining_qty, realized_pnl, avg_exit_price.

        Returns (gross_pnl, entry_commission_portion) for this leg so the caller
        can build a TradeRecord without duplicating accounting math.

        When remaining_qty reaches zero, state → CLOSED and position is cleared.
        """
        proportion = min(leg.qty / self.remaining_qty, 1.0) if self.remaining_qty > 0 else 0.0
        entry_comm = self._remaining_entry_commission * proportion
        self._remaining_entry_commission *= (1.0 - proportion)

        sign  = 1 if self.direction == TradeSide.LONG.value else -1
        gross = sign * (leg.fill_price - self.avg_entry_price) * leg.qty
        net   = gross - entry_comm - leg.commission

        self.remaining_qty = max(0.0, self.remaining_qty - leg.qty)
        self.realized_pnl += net
        self.exit_legs.append(leg)

        total_exit_qty = sum(l.qty for l in self.exit_legs)
        self.avg_exit_price = (
            sum(l.fill_price * l.qty for l in self.exit_legs) / total_exit_qty
        )

        if self.remaining_qty < 0.001:
            self.remaining_qty = 0.0
            self.state     = DealState.CLOSED
            self.positions = []         # no open exposure left

        return gross, entry_comm

    # ── Convenience properties ────────────────────────────────────────────────

    @property
    def entry_date(self) -> dt.datetime | None:
        return self.entry_legs[0].filled_at if self.entry_legs else None

    @property
    def entry_bar_index(self) -> int:
        return self.entry_legs[0].bar_index if self.entry_legs else -1

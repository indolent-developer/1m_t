"""
core.entities.position_types

Typed position dataclasses per instrument type.
Base Position mirrors broker_entities.Position; subtypes add instrument-specific fields.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from core.entities.broker_entities import TradeSide


@dataclass
class Position:
    """Live or historical position — all instrument types."""
    id:                        str
    symbol:                    str
    side:                      TradeSide
    open_date:                 dt.datetime
    close_date:                Optional[dt.datetime]
    quantity:                  float
    average_price:             float
    leverage:                  float
    market_value:              float
    unrealized_pnl:            float
    unrealized_pnl_percentage: float
    realized_pnl:              float
    realized_pnl_percentage:   float
    stop_loss_price:           float
    take_profit_price:         float
    additional_info:           Optional[dict] = None


@dataclass
class OptionPosition(Position):
    strike:             Decimal           = field(default_factory=lambda: Decimal(0))
    expiry:             Optional[date]    = None
    option_right:       str               = "call"   # "call" | "put"
    delta:              Optional[float]   = None
    implied_volatility: Optional[float]   = None


@dataclass
class KnockOutPosition(Position):
    knock_out_level: Decimal           = field(default_factory=lambda: Decimal(0))
    financing_daily: Optional[float]   = None
    distance_to_ko:  Optional[Decimal] = None


@dataclass
class FuturePosition(Position):
    contract_month: Optional[str]   = None
    contract_size:  Optional[float] = None
    expiry:         Optional[date]  = None

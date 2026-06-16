"""
core.entities.broker_entities

Enums and dataclasses for orders, trades, accounts, and decisions.
Canonical for both live trading and backtest replay.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# ── Enums ─────────────────────────────────────────────────────────────────────

class DataSource(Enum):
    CAPITAL = "capital"
    FMP     = "fmp"

    @classmethod
    def parse(cls, value: str) -> "DataSource":
        try:
            return cls(value)
        except ValueError:
            valid = [e.value for e in cls]
            raise ValueError(f"Invalid data_source '{value}'. Must be one of: {valid}")


class OrderSide(Enum):
    BUY  = "buy"
    SELL = "sell"


class TradeSide(Enum):
    LONG  = "long"
    SHORT = "short"


class OrderType(Enum):
    MARKET     = "market"
    LIMIT      = "limit"
    STOP       = "stop"
    STOP_LIMIT = "stop_limit"


class Action(Enum):
    WAIT           = "WAIT"
    ENTER          = "ENTER"
    EXIT           = "EXIT"
    PARTIAL_PROFIT = "PARTIAL_PROFIT"


class OrderStatus(Enum):
    PENDING          = "pending"
    FILLED           = "filled"
    CANCELLED        = "cancelled"
    REJECTED         = "rejected"
    INACTIVE         = "inactive"
    SUBMITTED        = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    PRE_SUBMITTED    = "presubmitted"
    PENDING_SUBMIT   = "pendingsubmit"


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class TradeDecision:
    action:          Action           = Action.WAIT
    direction:       Optional[TradeSide] = None
    size:            int              = 0
    quantity:        int              = 0
    reason:          str              = ""
    setup:           str              = ""   # trend | pullback | reversal | counter
    additional_info: Optional[dict]   = None


@dataclass
class OrderLog:
    error_code: str
    message:    str
    status:     str
    time:       dt.datetime


@dataclass
class Order:
    id:                   str
    symbol:               str
    order_type:           OrderType
    side:                 OrderSide
    quantity:             float
    price:                float
    status:               OrderStatus
    placed_timestamp:     Optional[dt.datetime]
    filled_timestamp:     Optional[dt.datetime]
    cancelled_timestamp:  Optional[dt.datetime]
    average_fill_price:   float
    fees:                 float
    leverage:             float
    deal_reference:       Optional[str]        = None
    broker_order_id:      Optional[str]        = None
    broker_deal_id:       Optional[str]        = None
    broker_specific_data: Optional[dict]       = None
    logs:                 Optional[list[OrderLog]] = None
    enter_reason:         Optional[str]        = None
    exit_reason:          Optional[str]        = None
    reject_reason:        Optional[str]        = None


@dataclass
class AccountInfo:
    account_id:           str
    account_name:         str
    status:               str
    account_type:         str
    currency:             str
    cash_in_hand:         float
    current_value:        float
    margin_used:          float
    margin_available:     float
    leverage:             float
    broker_specific_data: Optional[dict] = None

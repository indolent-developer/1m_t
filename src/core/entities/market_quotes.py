"""
core.entities.market_quotes

Typed quote dataclasses per instrument type.
Uses Decimal for price precision; optional greeks on OptionQuote.

Replaces old: Quote (broker_entities), PriceQuote
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from core.entities.instrument_type import InstrumentType


@dataclass
class Quote:
    """Base real-time quote — all instrument types."""
    symbol:          str
    instrument_type: InstrumentType
    bid:             Decimal
    ask:             Decimal
    last:            Decimal
    bid_size:        int
    ask_size:        int
    volume:          int
    timestamp:       dt.datetime

    @property
    def mid(self) -> Decimal:
        """Mid-point of bid/ask spread."""
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @classmethod
    def from_dict(cls, data: dict) -> "Quote":
        ts_raw = data.get("timestamp", 0)
        # Handle millisecond timestamps
        ts = dt.datetime.fromtimestamp(ts_raw / 1000 if ts_raw > 1_000_000_000_000 else ts_raw)
        return cls(
            symbol=data.get("symbol", ""),
            instrument_type=InstrumentType(data.get("instrument_type", "stock")),
            bid=Decimal(str(data.get("bidPrice", data.get("bid", 0)))),
            ask=Decimal(str(data.get("askPrice", data.get("ask", 0)))),
            last=Decimal(str(data.get("lastPrice", data.get("last", 0)))),
            bid_size=int(data.get("bidSize", 0)),
            ask_size=int(data.get("askSize", 0)),
            volume=int(data.get("volume", 0)),
            timestamp=ts,
        )


@dataclass
class OptionQuote(Quote):
    """Quote enriched with options greeks and contract details."""
    delta:              Optional[float]   = None
    gamma:              Optional[float]   = None
    theta:              Optional[float]   = None
    vega:               Optional[float]   = None
    implied_volatility: Optional[float]   = None
    open_interest:      Optional[int]     = None
    expiry:             Optional[dt.date] = None
    strike:             Optional[Decimal] = None
    option_right:       Optional[str]     = None   # "call" | "put"


@dataclass
class KnockOutQuote(Quote):
    knock_out_level: Decimal           = field(default_factory=lambda: Decimal(0))
    distance_to_ko:  Optional[Decimal] = None
    financing_cost:  Optional[float]   = None
    barrier:         Optional[Decimal] = None


@dataclass
class CommodityQuote(Quote):
    contract_month: Optional[str]   = None
    contract_size:  Optional[float] = None
    unit:           Optional[str]   = None   # "barrel", "troy oz", etc.


@dataclass
class ForexQuote(Quote):
    pip_value: Optional[float] = None
    lot_size:  Optional[float] = None


@dataclass
class CryptoQuote(Quote):
    volume_24h: Optional[float] = None
    change_24h: Optional[float] = None

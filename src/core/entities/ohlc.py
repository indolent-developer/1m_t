"""
core.entities.ohlc

OHLCV bar dataclass.  Clean dataclass rewrite — no mutable __init__ logic,
derived fields computed via __post_init__.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from core.entities.instrument_type import InstrumentType

DT_STR_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class OHLCData:
    """A single OHLCV candlestick bar."""
    open:   float
    high:   float
    low:    float
    close:  float
    volume: Optional[float]      = None
    time:   Optional[dt.datetime] = None
    symbol: Optional[str]        = None
    asset_type: Optional[InstrumentType] = None

    # Derived — set by __post_init__
    p_change:  float = field(init=False, default=0.0)
    t_str:     Optional[str] = field(init=False, default=None)
    timestamp: Optional[int] = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.p_change = (
            round(((self.close - self.open) / self.open) * 100, 3)
            if self.open != 0 else 0.0
        )
        if self.time:
            self.t_str    = self.time.strftime(DT_STR_FORMAT)
            self.timestamp = int(self.time.timestamp())

    # ── Short-hand aliases ─────────────────────────────────────────────────────
    @property
    def o(self) -> float: return self.open
    @property
    def h(self) -> float: return self.high
    @property
    def l(self) -> float: return self.low
    @property
    def c(self) -> float: return self.close
    @property
    def v(self) -> Optional[float]: return self.volume
    @property
    def t(self) -> Optional[str]: return self.t_str

    # ── Serialisation ──────────────────────────────────────────────────────────
    @staticmethod
    def from_dict(data: dict, dt_format: str = DT_STR_FORMAT) -> "OHLCData":
        time_raw = data.get("t")
        time = dt.datetime.strptime(time_raw, dt_format) if time_raw else None
        return OHLCData(
            open=float(data["o"]),
            high=float(data["h"]),
            low=float(data["l"]),
            close=float(data["c"]),
            volume=float(data["v"]) if data.get("v") is not None else None,
            time=time,
        )

    def to_dict(self) -> dict:
        return {
            "o": self.open,
            "h": self.high,
            "l": self.low,
            "c": self.close,
            "v": self.volume,
            "t": self.t_str,
        }

    def __str__(self) -> str:
        return (
            f"OHLCData(o={self.open} h={self.high} l={self.low} c={self.close} "
            f"v={self.volume} t={self.t_str} chg={self.p_change}%)"
        )

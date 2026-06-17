from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class ScannerEvent(str, Enum):
    SYMBOL_DETECTED = "symbol_detected"


@dataclass
class ScannerHit:
    """Emitted by a scanner loop when a new symbol meets its criteria."""

    event:        ScannerEvent
    symbol:       str
    scanner_name: str
    session:      str     # "pre" | "intraday" | "post"
    price:        float
    change_pct:   float   # regular-session day change %

    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Enrichment — not all scanners populate every field
    exchange:           Optional[str]   = None   # "NASDAQ" | "NYSE" | "AMEX"
    description:        Optional[str]   = None   # company name
    sector:             Optional[str]   = None
    market_cap:         Optional[float] = None
    volume:             Optional[float] = None
    avg_vol_30d:        Optional[float] = None
    rel_vol:            Optional[float] = None
    session_change_pct: Optional[float] = None   # pre/post/from-open % change
    float_shares:       Optional[float] = None

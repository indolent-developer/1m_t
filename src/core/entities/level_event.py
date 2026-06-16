from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class LevelEvent(str, Enum):
    BREAK_ABOVE = "break_above"
    BREAK_BELOW = "break_below"
    BOUNCE      = "bounce"       # price dipped into zone from above and returned up
    REJECTION   = "rejection"    # price rose into zone from below and got pushed back down
    FALSE_BREAK = "false_break"  # broke level but reversed within reversal window


@dataclass
class PriceLevelEvent:
    """Emitted when a tick triggers a meaningful interaction with a price level."""
    event:          LevelEvent
    symbol:         str
    level:          float
    price:          float
    zone_lo:        float
    zone_hi:        float
    atr:            float
    convincing:     bool              # price cleared level ± 0.5*atr
    tick_source:    str               # "finnhub" | "fmp"
    timestamp:      datetime          = field(default_factory=lambda: datetime.now(timezone.utc))
    dwell_seconds:  float             = 0.0
    original_break: Optional[LevelEvent] = None

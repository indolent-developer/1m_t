from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from adapters.brokers.entities.broker_event import BrokerEvent


@dataclass
class BrokerEventPayload:
    """Envelope wrapping every broker event."""
    event:     BrokerEvent
    broker_id: str                              # e.g. "ibkr_demo", "capital_live"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    data:      Any = None                       # Order, Quote, Position, AccountInfo …
    error:     Optional[str] = None            # set on REJECTED / CONNECTION_LOST

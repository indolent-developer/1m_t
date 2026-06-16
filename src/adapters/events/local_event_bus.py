"""
adapters.events.local_event_bus — LocalEventBus.

In-process event bus. Supports multiple listeners per event and wildcard
(subscribe_all). Sync and async handlers are both supported.

Use this for single-machine deployments where all components (broker adapter,
risk engine, journal, notifier) run in the same Python process.

Upgrade path: swap for RedisEventBus in BaseBroker.__init__() when you need
multi-machine deployment. All callers and handlers are unchanged.
"""
from __future__ import annotations

import asyncio
from core.utils.log_helper import getLogger
from collections import defaultdict
from typing import Any, Dict, List, Optional

from core.adapters.event_bus import EventHandler

logger = getLogger(__name__)


class LocalEventBus:
    """
    In-process event bus — implements IEventBus.

    Handlers are called in registration order. One failing handler never
    blocks the others; exceptions are logged and swallowed.
    """

    def __init__(self) -> None:
        self._listeners: Dict[Any, List[EventHandler]] = defaultdict(list)
        self._wildcard:  List[EventHandler] = []

    def subscribe(self, event: Any, handler: EventHandler) -> None:
        """Subscribe handler to a specific event type."""
        self._listeners[event].append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Subscribe to every event — useful for audit logging, journaling."""
        self._wildcard.append(handler)

    def unsubscribe(self, event: Any, handler: EventHandler) -> None:
        self._listeners[event] = [
            h for h in self._listeners[event] if h is not handler
        ]

    def unsubscribe_all(self, handler: EventHandler) -> None:
        self._wildcard = [h for h in self._wildcard if h is not handler]

    async def emit(self, payload: Any) -> None:
        """
        Fire all handlers registered for payload.event, then wildcard handlers.
        Handler exceptions are caught individually so one bad handler never
        silences the rest.
        """
        event    = getattr(payload, "event", None)
        handlers = self._listeners[event] + self._wildcard
        for handler in handlers:
            try:
                result = handler(payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "[LocalEventBus] handler error event=%s broker=%s",
                    getattr(event, "value", event),
                    getattr(payload, "broker_id", "?"),
                )

    def listener_count(self, event: Optional[Any] = None) -> int:
        """Diagnostic: count listeners for one event, or all events."""
        if event is not None:
            return len(self._listeners[event]) + len(self._wildcard)
        return sum(len(v) for v in self._listeners.values()) + len(self._wildcard)

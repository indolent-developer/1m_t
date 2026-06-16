"""
core.adapters.event_bus — IEventBus protocol.

Defines the contract every event bus implementation must satisfy.
Core code (BaseBroker, risk engine, router) depends only on this interface —
never on a concrete implementation.

Implementations:
    adapters.events.local_event_bus.LocalEventBus   — in-process, zero latency
    adapters.events.redis_event_bus.RedisEventBus   — pub/sub, multi-machine
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, runtime_checkable

# Handler: sync or async callable that receives a payload
EventHandler = Callable[[Any], Any]


@runtime_checkable
class IEventBus(Protocol):
    """
    Minimal event bus interface.

    subscribe / subscribe_all / unsubscribe / unsubscribe_all manage handlers.
    emit fires all handlers registered for a payload's event type.
    listener_count is a diagnostic helper.
    """

    def subscribe(self, event: Any, handler: EventHandler) -> None: ...

    def subscribe_all(self, handler: EventHandler) -> None: ...

    def unsubscribe(self, event: Any, handler: EventHandler) -> None: ...

    def unsubscribe_all(self, handler: EventHandler) -> None: ...

    async def emit(self, payload: Any) -> None: ...

    def listener_count(self, event: Optional[Any] = None) -> int: ...

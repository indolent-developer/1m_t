from abc import ABC
from typing import Any, List

from core.adapters.event_bus import IEventBus


class BaseSubscriber(ABC):
    """
    Abstract base for all bus subscribers.
 
    Concrete subclasses call self._subscribe() in __init__ to register handlers.
    Unsubscribe all handlers by calling detach().
    """
 
    def __init__(self, bus: IEventBus) -> None:
        self._bus = bus
        self._registered: List[tuple] = []  # (event, handler) for cleanup
 
    def _subscribe(self, event: Any, handler) -> None:
        self._bus.subscribe(event, handler)
        self._registered.append((event, handler))
 
    def _subscribe_all(self, handler) -> None:
        self._bus.subscribe_all(handler)
        self._registered.append((None, handler))
 
    def detach(self) -> None:
        """Unsubscribe all handlers registered by this subscriber."""
        for event, handler in self._registered:
            if event is None:
                self._bus.unsubscribe_all(handler)
            else:
                self._bus.unsubscribe(event, handler)
        self._registered.clear()
 
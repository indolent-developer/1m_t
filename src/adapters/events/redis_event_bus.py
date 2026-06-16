"""
adapters.events.redis_event_bus — RedisEventBus.

Drop-in replacement for LocalEventBus for multi-machine deployments where
strategy instances, risk engine, and journal run on separate servers.

Channel naming convention:
    broker:{broker_id}:{event.value}
    e.g.  broker:ibkr_demo:order_filled
          broker:capital_live:quote_update
          broker:*:equity_floor_hit     (wildcard — risk monitor on any machine)

Mixed-mode behaviour:
    Local listeners on the publishing machine fire WITHOUT the Redis round-trip,
    so same-machine consumers have zero extra latency. Only remote machines pay
    the network cost.

Setup:
    pip install redis
    Redis server reachable from all machines.

Swap in BaseBroker.__init__():
    # single machine (default)
    self.events = LocalEventBus()

    # distributed
    self.events = RedisEventBus(
        redis_url=config.redis_url,     # e.g. "redis://localhost:6379"
        broker_id=self.broker_id,
    )

On each REMOTE machine that needs to receive events:
    asyncio.create_task(broker.events.start_subscriber())
"""
from __future__ import annotations

import asyncio
import json
from core.utils.log_helper import getLogger
from collections import defaultdict
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from core.adapters.event_bus import EventHandler

logger = getLogger(__name__)


class RedisEventBus:
    """
    Redis pub/sub event bus — implements IEventBus.

    emit()  publishes to a Redis channel AND fires local handlers (no round-trip).
    start_subscriber() is a background task for remote machines that re-fires
    local handlers when a published message arrives via Redis.
    """

    def __init__(self, redis_url: str, broker_id: str) -> None:
        try:
            import redis as redis_lib
            self._redis     = redis_lib.from_url(redis_url)
        except ImportError:
            raise ImportError("RedisEventBus requires 'redis' — pip install redis")
        self._redis_url  = redis_url
        self._broker_id  = broker_id
        self._listeners: Dict[Any, List[EventHandler]] = defaultdict(list)
        self._wildcard:  List[EventHandler] = []

    # ── Subscription management (same API as LocalEventBus) ───────────────────

    def subscribe(self, event: Any, handler: EventHandler) -> None:
        self._listeners[event].append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        self._wildcard.append(handler)

    def unsubscribe(self, event: Any, handler: EventHandler) -> None:
        self._listeners[event] = [
            h for h in self._listeners[event] if h is not handler
        ]

    def unsubscribe_all(self, handler: EventHandler) -> None:
        self._wildcard = [h for h in self._wildcard if h is not handler]

    # ── Emit ──────────────────────────────────────────────────────────────────

    async def emit(self, payload: Any) -> None:
        """
        1. Publish to Redis so remote machines receive it.
        2. Fire local handlers immediately (no round-trip latency).
        """
        event   = getattr(payload, "event", None)
        channel = f"broker:{self._broker_id}:{getattr(event, 'value', event)}"
        try:
            self._redis.publish(channel, self._serialize(payload))
        except Exception:
            logger.exception("[RedisEventBus] publish failed channel=%s", channel)

        await self._fire_local(payload)

    async def _fire_local(self, payload: Any) -> None:
        event    = getattr(payload, "event", None)
        handlers = self._listeners[event] + self._wildcard
        for handler in handlers:
            try:
                result = handler(payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("[RedisEventBus] local handler error")

    # ── Remote subscriber (run on each remote machine) ────────────────────────

    async def start_subscriber(self) -> None:
        """
        Background task for REMOTE machines.

        Subscribes to all channels for this broker_id and re-fires local
        handlers when a message arrives. Run on every machine that needs events:

            asyncio.create_task(broker.events.start_subscriber())
        """
        try:
            import redis.asyncio as aioredis
        except ImportError:
            raise ImportError("RedisEventBus.start_subscriber requires 'redis[asyncio]'")

        client = aioredis.from_url(self._redis_url)
        pubsub = client.pubsub()
        pattern = f"broker:{self._broker_id}:*"
        await pubsub.psubscribe(pattern)
        logger.info("[RedisEventBus] subscribed to %s", pattern)

        async for message in pubsub.listen():
            if message["type"] != "pmessage":
                continue
            try:
                payload = self._deserialize(message["data"])
                await self._fire_local(payload)
            except Exception:
                logger.exception("[RedisEventBus] subscriber error")

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def listener_count(self, event: Optional[Any] = None) -> int:
        if event is not None:
            return len(self._listeners[event]) + len(self._wildcard)
        return sum(len(v) for v in self._listeners.values()) + len(self._wildcard)

    # ── Serialisation helpers ─────────────────────────────────────────────────

    @staticmethod
    def _serialize(payload: Any) -> str:
        try:
            d = asdict(payload)
            # Enums → their .value for JSON compatibility
            for k, v in d.items():
                if hasattr(v, "value"):
                    d[k] = v.value
            return json.dumps(d, default=str)
        except Exception:
            return json.dumps({"error": "serialisation_failed"})

    @staticmethod
    def _deserialize(raw: bytes | str) -> Any:
        """
        Returns a plain dict — callers that need a typed payload should
        reconstruct from BrokerEventPayload.from_dict(data) once that is added.
        """
        return json.loads(raw)

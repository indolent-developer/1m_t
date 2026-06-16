"""
interfaces.telegram.notifier — TelegramNotifier

Subscribes to broker events and forwards them as Telegram messages.
Wire into the event bus after broker.connect():

    notifier = TelegramNotifier(token="...", chat_id="...")
    broker.events.subscribe(BrokerEvent.ORDER_FILLED,     notifier.on_order)
    broker.events.subscribe(BrokerEvent.ORDER_REJECTED,   notifier.on_order)
    broker.events.subscribe(BrokerEvent.POSITION_OPENED,  notifier.on_position)
    broker.events.subscribe(BrokerEvent.POSITION_CLOSED,  notifier.on_position)
    broker.events.subscribe(BrokerEvent.EQUITY_FLOOR_HIT, notifier.on_risk)
    broker.events.subscribe(BrokerEvent.DAILY_LOSS_LIMIT, notifier.on_risk)
    broker.events.subscribe(BrokerEvent.CONNECTION_LOST,  notifier.on_connection)
    broker.events.subscribe(BrokerEvent.RECONNECTING,     notifier.on_connection)
"""
from __future__ import annotations

from core.utils.log_helper import getLogger as _getLogger
from adapters.brokers.entities.broker_event_payload import BrokerEventPayload
from core.entities.broker_entities import Order
from core.entities.position_types import Position
from interfaces.telegram.formatters import (
    fmt_connection_lost,
    fmt_order,
    fmt_position,
    fmt_reconnecting,
    fmt_risk_alert,
)

logger = _getLogger(__name__, app_name="tg-bot")


class TelegramNotifier:

    def __init__(self, token: str, chat_id: str | int) -> None:
        try:
            from telegram import Bot
            self._bot = Bot(token=token)
        except ImportError:
            raise ImportError("TelegramNotifier requires 'python-telegram-bot' — pip install python-telegram-bot")

        self._chat_id = str(chat_id)

    async def _send(self, text: str) -> None:
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception:
            logger.exception("[TelegramNotifier] Failed to send message")

    # ── Event handlers ────────────────────────────────────────────────────────

    async def on_order(self, payload: BrokerEventPayload) -> None:
        if isinstance(payload.data, Order):
            await self._send(fmt_order(payload.data))

    async def on_position(self, payload: BrokerEventPayload) -> None:
        if isinstance(payload.data, Position):
            await self._send(fmt_position(payload.data))

    async def on_risk(self, payload: BrokerEventPayload) -> None:
        data = payload.data or {}
        await self._send(fmt_risk_alert(payload.event.value, data))

    async def on_connection(self, payload: BrokerEventPayload) -> None:
        event = payload.event.value
        data  = payload.data or {}
        if event == "connection_lost":
            await self._send(fmt_connection_lost(payload.broker_id))
        elif event == "reconnecting":
            await self._send(fmt_reconnecting(
                payload.broker_id,
                data.get("attempt", "?"),
                data.get("max_attempts", "?"),
            ))

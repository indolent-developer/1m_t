"""
core.adapters.brokers.base_broker

Abstract base class for all broker adapters.

Concrete implementations:
    IBKRBroker       — ib_async, TWS / IB Gateway
    CapitalBroker    — Capital.com REST + WebSocket
    eToroBroker      — eToro REST + WebSocket
    ScalableBroker   — sc CLI subprocess

Event bus:
    Every broker emits typed BrokerEvents via an IEventBus instance.
    Multiple handlers can subscribe to the same event (journal, risk engine,
    Telegram notifier all fire independently on ORDER_FILLED).

    Current:  LocalEventBus  — in-process, same PID, zero latency
    Upgrade:  RedisEventBus  — drop-in replacement for multi-machine deployment
                               where strategy instances run on separate servers.
              Swap point: pass RedisEventBus(...) to BaseBroker.__init__().
              Broker code, caller code, and BrokerEventRouter are unchanged.

Design principles:
    - Fully async throughout
    - Capability-aware: subclasses declare what they support via BrokerCapabilities
    - Streaming-first: subscribe_quotes() is first-class; polling is the fallback
    - No raw strings for enums: OrderStatus, OrderSide, OrderType everywhere
"""

from __future__ import annotations

import asyncio
from core.utils.log_helper import getLogger, set_log_broker
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from adapters.brokers.entities.broker_event import BrokerEvent
from adapters.brokers.entities.broker_event_payload import BrokerEventPayload
from adapters.events.local_event_bus import LocalEventBus
from core.adapters.event_bus import IEventBus
from core.entities.broker_capabilities import BrokerCapabilities
from core.entities.broker_entities import (
    AccountInfo,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from core.entities.market_quotes import Quote
from core.entities.position_types import Position

logger = getLogger(__name__)


# ── Base broker ───────────────────────────────────────────────────────────────

class BaseBroker(ABC):
    """
    Abstract base for all broker adapters.

    Subclasses must implement all abstract methods. Optional methods
    (subscribe_quotes, update_position_stops, etc.) default to no-ops —
    implement only what the broker supports, declare the rest in capabilities.

    Config:
        Each concrete class accepts its own typed config dataclass
        (IBKRConfig, CapitalConfig, eToroConfig, ScalableConfig).
        All configs share: is_demo, broker_id property, risk thresholds.

    Usage:
        broker = IBKRBroker(IBKRConfig(is_demo=True))
        broker.events.subscribe(BrokerEvent.ORDER_FILLED, journal.on_fill)
        broker.events.subscribe(BrokerEvent.ORDER_FILLED, risk.on_fill)
        broker.events.subscribe_all(audit.log_everything)
        await broker.connect()
        order = await broker.place_order("AAPL", 10, OrderSide.BUY, OrderType.LIMIT, price=182.50)
    """

    def __init__(self, config: Any, event_bus: IEventBus | None = None) -> None:
        self.config    = config
        self.broker_id: str = getattr(config, "broker_id", None) or self.__class__.__name__.lower()
        set_log_broker(self.broker_id)

        # Inject any IEventBus impl; defaults to in-process LocalEventBus.
        # Swap to RedisEventBus(redis_url=..., broker_id=...) for multi-machine.
        self.events: IEventBus = event_bus or LocalEventBus()

        # Active quote subscriptions: symbol → subscription handle
        self._subscribed_symbols: Dict[str, Any] = {}

        # Background tasks (polling loops, reconnect, etc.)
        self._background_tasks: List[asyncio.Task] = []

        # TTL caches — subclasses read/write these directly
        self._position_cache:    Optional[List[Position]] = None
        self._position_cache_ts: float = 0.0
        self._account_cache:     Optional[AccountInfo]   = None
        self._account_cache_ts:  float = 0.0

    # ── Capabilities ──────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities:
        """
        Declare what this broker supports.
        Callers check capabilities before calling optional methods.

        Example:
            if broker.capabilities.supports_options:
                await broker.place_option_order(...)
            if broker.capabilities.supports_streaming_quotes:
                await broker.subscribe_quotes(["AAPL"])
        """

    @property
    @abstractmethod
    def supports_fractional_shares(self) -> bool:
        """Whether this broker supports fractional share quantities."""

    # ── Emit helper ───────────────────────────────────────────────────────────

    async def _emit(
        self,
        event: BrokerEvent,
        data: Any = None,
        error: Optional[str] = None,
    ) -> None:
        """
        Fire an event on the broker's event bus.
        Subclasses always call this — never call self.events.emit() directly,
        so the payload is always constructed consistently.
        """
        await self.events.emit(BrokerEventPayload(
            event=event,
            broker_id=self.broker_id,
            data=data,
            error=error,
        ))

    # ── Connection lifecycle ──────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> bool:
        """
        Establish connection to the broker API.
        Must emit CONNECTED on success, CONNECTION_LOST on failure.
        Must wire all event handlers before emitting CONNECTED.
        """

    @abstractmethod
    async def disconnect(self) -> bool:
        """
        Graceful disconnect.
        Must cancel all subscriptions and background tasks.
        Must emit DISCONNECTED.
        """

    async def reconnect(
        self,
        max_attempts: int = 3,
        delay: float = 5.0,
    ) -> bool:
        """
        Default reconnect loop with exponential backoff.
        Subclasses may override for broker-specific behaviour (e.g. IBKR
        requires a new clientId on reconnect).

        Emits RECONNECTING before each attempt.
        Returns True if reconnected successfully, False if all attempts fail.
        """
        for attempt in range(1, max_attempts + 1):
            await self._emit(
                BrokerEvent.RECONNECTING,
                data={"attempt": attempt, "max_attempts": max_attempts},
            )
            logger.info(
                "[%s] Reconnect attempt %d/%d",
                self.broker_id, attempt, max_attempts,
            )
            try:
                if await self.connect():
                    logger.info("[%s] Reconnected successfully", self.broker_id)
                    return True
            except Exception:
                logger.exception(
                    "[%s] Reconnect attempt %d failed",
                    self.broker_id, attempt,
                )
            # Exponential backoff: 5s, 10s, 15s …
            await asyncio.sleep(delay * attempt)

        logger.error(
            "[%s] All %d reconnect attempts failed",
            self.broker_id, max_attempts,
        )
        return False

    # ── Account ───────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_account_info(self) -> AccountInfo:
        """
        Get account snapshot (equity, buying power, cash, P&L).
        Implementations should also emit ACCOUNT_UPDATE and call
        check_risk_limits() so listeners stay in sync.
        """

    # ── Positions ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[Position]:
        """Get open position for a symbol, or None if no position exists."""

    @abstractmethod
    async def get_positions(
        self,
        symbol: Optional[str] = None,
    ) -> List[Position]:
        """
        Get all open positions, optionally filtered by symbol.
        Returns empty list if no positions exist.
        """

    @abstractmethod
    async def close_position(
        self,
        position_id: str,
        quantity: Optional[float] = None,
    ) -> bool:
        """
        Close a position fully (quantity=None) or partially.
        Must emit POSITION_CLOSED on full close, POSITION_UPDATED on partial.

        Position ID convention per broker:
            IBKR:     "{account}_{conId}"   e.g. "DU123456_265598"
            Capital:  dealId (str)           e.g. "abc123"
            eToro:    str(positionId)        e.g. "987654321"
            Scalable: "{isin}_{orderId}"     e.g. "US0378331005_42"
        """

    async def update_position_stops(
        self,
        position_id: str,
        stop_loss_price:    Optional[float] = None,
        stop_loss_distance: Optional[float] = None,
        stop_trailing:      Optional[bool]  = None,
        profit_price:       Optional[float] = None,
        profit_distance:    Optional[float] = None,
    ) -> bool:
        """
        Update stop loss / take profit on a live position.
        Default: no-op (returns False) — implement if broker supports it.
        Implementations must emit POSITION_UPDATED on success.
        Check capabilities.supports_stop_orders before calling.
        """
        return False

    # ── Orders ────────────────────────────────────────────────────────────────

    @abstractmethod
    async def place_order(
        self,
        symbol:      str,
        quantity:    float,
        side:        OrderSide,
        order_type:  OrderType,
        price:       Optional[float] = None,
        *,
        stop_loss:      Optional[float] = None,
        take_profit:    Optional[float] = None,
        time_in_force:  str = "GTC",
        notes:          Optional[str]  = None,  # journaling hook
    ) -> Order:
        """
        Place a new order.

        Fill model varies by broker:
            IBKR:     Returns SUBMITTED immediately. ORDER_FILLED fires later
                      via orderStatusEvent when IB confirms execution.
            Capital:  Returns FILLED synchronously (market orders only).
            eToro:    Market orders fill synchronously. MIT/limit orders return
                      SUBMITTED and poll via background task.
            Scalable: Returns SUBMITTED immediately. Background task polls sc CLI
                      until filled or rejected.

        Implementations must emit ORDER_SUBMITTED on acceptance, then
        ORDER_FILLED or ORDER_REJECTED when the terminal state is known.
        """

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a pending order.
        Must emit ORDER_CANCELLED on success.
        Returns False (not an error) if the broker doesn't support cancellation
        (e.g. Capital.com market-only orders).
        """

    @abstractmethod
    async def get_order(self, order_id: str) -> Optional[Order]:
        """Fetch a single order by ID. Returns None if not found."""

    @abstractmethod
    async def get_orders(
        self,
        symbol: Optional[str]        = None,
        status: Optional[OrderStatus] = None,  # enum, never raw string
    ) -> List[Order]:
        """Get orders, optionally filtered by symbol and/or status."""

    # ── Quotes ────────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote:
        """
        One-shot quote fetch (polling fallback).
        Prefer subscribe_quotes() for live strategies.
        """

    @abstractmethod
    async def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        """Batch one-shot quote fetch."""

    async def subscribe_quotes(self, symbols: List[str]) -> bool:
        """
        Subscribe to streaming quote updates for symbols.
        Implementations emit QUOTE_UPDATE for every tick received.
        Returns False if streaming not supported — use get_quote() polling.
        Check capabilities.supports_streaming_quotes before calling.

        Lazy-initialised: safe to call multiple times for the same symbol.
        """
        return False

    async def unsubscribe_quotes(self, symbols: List[str]) -> bool:
        """Unsubscribe from streaming quote updates."""
        return False

    # ── Risk guardrails ───────────────────────────────────────────────────────

    async def check_risk_limits(self, account: AccountInfo) -> None:
        """
        Fire guardrail events when account breaches strategy thresholds.

        Call this inside get_account_info() implementations after mapping the
        account data, so risk checks happen automatically on every account poll.

        Thresholds (from Master_Strategy_1M_Plan.md, read from config):
            Hard max loss:  $20,000 total drawdown → DAILY_LOSS_LIMIT
                            → strategy should go 100% cash and reassess
            Equity floor:   own equity < $55,000   → EQUITY_FLOOR_HIT
                            → loan guardrail breach (never let own equity drop
                               below $55k — 25% buffer on $50k loan)

        Config keys read (all optional with sane defaults):
            config.loan_amount      default: 50_000
            config.equity_floor     default: 55_000
            config.hard_max_loss    default: 20_000
            config.starting_equity  default: 122_562
        """
        loan_amount     = getattr(self.config, "loan_amount",     50_000.0)
        equity_floor    = getattr(self.config, "equity_floor",    55_000.0)
        hard_max_loss   = getattr(self.config, "hard_max_loss",   20_000.0)
        starting_equity = getattr(self.config, "starting_equity", 122_562.0)

        # Own equity = total account value minus the loan
        own_equity = (account.current_value or 0) - loan_amount

        if own_equity < equity_floor:
            logger.warning(
                "[%s] EQUITY FLOOR HIT: own_equity=%.2f < floor=%.2f",
                self.broker_id, own_equity, equity_floor,
            )
            await self._emit(
                BrokerEvent.EQUITY_FLOOR_HIT,
                data={
                    "own_equity":   own_equity,
                    "equity_floor": equity_floor,
                    "loan_amount":  loan_amount,
                },
            )

        total_drawdown = starting_equity - (account.current_value or 0)
        if total_drawdown >= hard_max_loss:
            logger.critical(
                "[%s] DAILY LOSS LIMIT: drawdown=%.2f >= max=%.2f — go to cash",
                self.broker_id, total_drawdown, hard_max_loss,
            )
            await self._emit(
                BrokerEvent.DAILY_LOSS_LIMIT,
                data={
                    "drawdown":       total_drawdown,
                    "hard_max_loss":  hard_max_loss,
                    "current_equity": account.current_value,
                },
            )

    # ── Background task helpers ───────────────────────────────────────────────

    def _start_background_task(self, coro) -> asyncio.Task:
        """Schedule a background coroutine and track it for cleanup on disconnect."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(
            lambda t: self._background_tasks.remove(t)
            if t in self._background_tasks else None
        )
        return task

    async def _cancel_background_tasks(self) -> None:
        """Cancel all tracked background tasks. Called by disconnect()."""
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._background_tasks.clear()

    # ── Dunder ────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"broker_id={self.broker_id!r} "
            f"subscribed={list(self._subscribed_symbols.keys())} "
            f"listeners={self.events.listener_count()}>"
        )

"""
adapters.brokers.ibkr_broker

Interactive Brokers broker adapter — ib_async (IB Gateway / TWS).

Prerequisites:
  1. IB Gateway running in Docker:
       cd docker/ibkr_gateway && docker compose up -d
     Ports: 4001 = live, 4002 = paper
  2. uv add ib_async

Connection:
  Live:  IBKRBrokerConfig(port=4001, is_demo=False)
  Paper: IBKRBrokerConfig(port=4002, is_demo=True)

Order flow (no 2-phase like Scalable):
  preview_order()  → fetches live quote, stores qualified contract, returns
                     estimated cost dict + a UUID confirm_id
  submit_order()   → retrieves stored contract, places ib_async order,
                     blocks until fill (market) or spawns background task (stop/limit)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from core.utils.log_helper import getLogger

from adapters.brokers.base_broker import BaseBroker
from adapters.brokers.entities.broker_event import BrokerEvent
from core.config.config_models import IBKRBrokerConfig
from core.entities.broker_capabilities import BrokerCapabilities
from core.entities.broker_entities import (
    AccountInfo,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TradeSide,
)
from core.entities.instrument_type import InstrumentType
from core.entities.market_quotes import Quote
from core.entities.position_types import Position

logger = getLogger(__name__)

_FILL_TIMEOUT = 120.0   # seconds to wait for a market order fill
_POLL_SLEEP   = 0.5     # seconds between fill-status checks


def _require_ib_async():
    try:
        import ib_async
        return ib_async
    except ImportError:
        raise ImportError(
            "ib_async is not installed. Run: uv add ib_async\n"
            "Then start IB Gateway: cd docker/ibkr_gateway && docker compose up -d"
        )


class IBKRBroker(BaseBroker):
    """
    Interactive Brokers adapter via ib_async.

    Usage:
        broker = IBKRBroker(IBKRBrokerConfig(port=4001, is_demo=False))
        await broker.connect()
        quote  = await broker.get_quote("AAPL")
        prev, cid = await broker.preview_order("AAPL", 10, OrderSide.BUY, OrderType.MARKET)
        order  = await broker.submit_order("AAPL", 10, OrderSide.BUY, cid, prev)
    """

    def __init__(self, config: IBKRBrokerConfig) -> None:
        super().__init__(config)
        self.config: IBKRBrokerConfig = config
        self._ib          = None
        self._account: Optional[str] = None
        self._connected   = False
        # confirm_id → qualified ib_async.Contract (cleared after submit)
        self._pending_contracts: Dict[str, Any] = {}
        # order_id → ib_async.Trade (for cancel / status lookup)
        self._trades: Dict[str, Any] = {}
        # order_id → background asyncio.Task (stop/limit orders)
        self._order_poll_tasks: Dict[str, asyncio.Task] = {}

    # ── Capabilities ──────────────────────────────────────────────────────────

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            stock_trading=True,
            etf_trading=True,
            knock_out_trading=False,
            fractional_shares=True,
            bracket_orders=True,
            short_selling=True,
            real_time_quotes=True,
            historical_data=True,
        )

    @property
    def supports_fractional_shares(self) -> bool:
        return True

    # ── Connection ────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        ib_async = _require_ib_async()
        try:
            self._ib = ib_async.IB()
            self._ib.errorEvent += self._on_ib_error
            # Suppress ib_async's own console logger — we handle all output above
            logging.getLogger("ib_async").setLevel(logging.ERROR)
            await self._ib.connectAsync(
                host=self.config.host,
                port=self.config.port,
                clientId=self.config.client_id_broker,
                readonly=self.config.readonly,
                timeout=self.config.timeout_seconds,
            )
            accounts = self._ib.managedAccounts()
            self._account = accounts[0] if accounts else None
            self._connected = True
            # 1 = live, 3 = delayed (no market data subscription needed)
            self._ib.reqMarketDataType(self.config.market_data_type)
            await self._emit(BrokerEvent.CONNECTED)
            logger.info(
                "[%s] Connected — account=%s  market_data_type=%d",
                self.broker_id, self._account, self.config.market_data_type,
            )
            return True
        except Exception as e:
            msg = str(e) or type(e).__name__
            logger.error(
                "[%s] connect() failed — %s:%s — %s",
                self.broker_id, self.config.host, self.config.port, msg,
            )
            await self._emit(BrokerEvent.CONNECTION_LOST, error=msg)
            return False

    async def disconnect(self) -> bool:
        self._connected = False
        for task in list(self._order_poll_tasks.values()):
            task.cancel()
        await asyncio.gather(*self._order_poll_tasks.values(), return_exceptions=True)
        self._order_poll_tasks.clear()
        await self._cancel_background_tasks()
        if self._ib and self._ib.isConnected():
            self._ib.disconnect()
        self._ib = None
        await self._emit(BrokerEvent.DISCONNECTED)
        logger.info("[%s] Disconnected", self.broker_id)
        return True

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_account_info(self) -> AccountInfo:
        self._require_connected()
        now = time.time()
        if self._account_cache and (now - self._account_cache_ts) < self.config.timeout_seconds:
            return self._account_cache

        await asyncio.wait_for(
            self._ib.reqAccountSummaryAsync(),
            timeout=self.config.timeout_seconds,
        )
        _TAGS = {"NetLiquidation", "TotalCashValue", "GrossPositionValue",
                 "MaintMarginReq", "AvailableFunds"}
        vals: Dict[str, float] = {}
        for item in self._ib.accountValues(self._account or ""):
            if item.tag in _TAGS and item.currency in ("USD", "EUR", "BASE"):
                vals[item.tag] = float(item.value)

        net_liq = vals.get("NetLiquidation", 0.0)
        gross   = vals.get("GrossPositionValue", 0.0)
        info = AccountInfo(
            account_id=self._account or "",
            account_name=f"IBKR {'Paper' if self.config.is_demo else 'Live'}",
            status="active",
            account_type="margin",
            currency="USD",
            cash_in_hand=vals.get("TotalCashValue", 0.0),
            current_value=net_liq,
            margin_used=vals.get("MaintMarginReq", 0.0),
            margin_available=vals.get("AvailableFunds", 0.0),
            leverage=round(gross / net_liq, 2) if net_liq else 1.0,
            broker_specific_data=vals,
        )
        self._account_cache    = info
        self._account_cache_ts = now
        await self._emit(BrokerEvent.ACCOUNT_UPDATE, data=info)
        await self.check_risk_limits(info)
        return info

    # ── Positions ─────────────────────────────────────────────────────────────

    async def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        self._require_connected()
        # portfolio() gives market value + unrealised PnL; positions() only gives avgCost
        await asyncio.wait_for(
            self._ib.reqPositionsAsync(),
            timeout=self.config.timeout_seconds,
        )
        results = []
        for item in self._ib.portfolio(self._account or ""):
            if item.position == 0:
                continue
            pos = self._map_portfolio_item(item)
            if symbol is None or pos.symbol.upper() == symbol.upper():
                results.append(pos)
        return results

    async def get_position(self, symbol: str) -> Optional[Position]:
        positions = await self.get_positions(symbol)
        return positions[0] if positions else None

    def _map_portfolio_item(self, item) -> Position:
        c = item.contract
        qty     = float(item.position)
        avg     = float(item.averageCost)
        mval    = float(item.marketValue)
        upl     = float(item.unrealizedPNL)
        upl_pct = (upl / (avg * qty) * 100) if avg and qty else 0.0
        return Position(
            id=f"{item.account}_{c.conId}",
            symbol=c.symbol,
            side=TradeSide.LONG if qty > 0 else TradeSide.SHORT,
            open_date=dt.datetime.now(dt.timezone.utc),
            close_date=None,
            quantity=abs(qty),
            average_price=avg,
            leverage=1.0,
            market_value=mval,
            unrealized_pnl=upl,
            unrealized_pnl_percentage=upl_pct,
            realized_pnl=float(item.realizedPNL),
            realized_pnl_percentage=0.0,
            stop_loss_price=0.0,
            take_profit_price=0.0,
            additional_info={"conId": c.conId, "currency": c.currency, "exchange": c.exchange},
        )

    async def close_position(self, position_id: str, quantity: Optional[float] = None) -> bool:
        self._require_connected()
        positions = await self.get_positions()
        pos = next(
            (p for p in positions if p.id == position_id or p.symbol.upper() == position_id.upper()),
            None,
        )
        if not pos:
            raise RuntimeError(f"Position {position_id} not found")
        qty = quantity or pos.quantity
        await self.place_order(pos.symbol, qty, OrderSide.SELL, OrderType.MARKET)
        return True

    # ── Quotes ────────────────────────────────────────────────────────────────

    async def get_quote(self, symbol: str) -> Quote:
        self._require_connected()
        ib_async = _require_ib_async()
        contract = ib_async.Stock(symbol, "SMART", "USD")
        await self._ib.qualifyContractsAsync(contract)
        [ticker] = await self._ib.reqTickersAsync(contract)

        def _f(v) -> float:
            try:
                x = float(v)
                return x if x == x else 0.0   # NaN check
            except (TypeError, ValueError):
                return 0.0

        close = _f(ticker.close)
        bid   = _f(ticker.bid)  or close
        ask   = _f(ticker.ask)  or close
        last  = _f(ticker.last) or close

        return Quote(
            symbol=symbol,
            instrument_type=InstrumentType.STOCK,
            bid=Decimal(str(bid)),
            ask=Decimal(str(ask)),
            last=Decimal(str(last)),
            bid_size=int(_f(ticker.bidSize)),
            ask_size=int(_f(ticker.askSize)),
            volume=int(_f(ticker.volume)),
            timestamp=dt.datetime.now(dt.timezone.utc),
        )

    async def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        results = {}
        for sym in symbols:
            try:
                results[sym] = await self.get_quote(sym)
            except Exception as e:
                logger.warning("[%s] get_quote(%s) failed: %s", self.broker_id, sym, e)
        return results

    # ── Preview / Submit (2-phase shim for CLI compatibility) ─────────────────

    async def preview_order(
        self,
        symbol:     str,
        quantity:   float,
        side:       OrderSide = OrderSide.BUY,
        order_type: OrderType = OrderType.MARKET,
        price:      Optional[float] = None,
    ) -> tuple:
        """
        Qualify the contract and fetch a live quote to show an estimated cost.
        Returns (preview_dict, confirm_id).  confirm_id is a UUID that maps to
        the cached contract so submit_order can place without re-qualifying.
        """
        self._require_connected()
        ib_async = _require_ib_async()

        quote    = await self.get_quote(symbol)
        contract = ib_async.Stock(symbol, "SMART", "USD")
        await self._ib.qualifyContractsAsync(contract)

        action    = "BUY" if side == OrderSide.BUY else "SELL"
        est_price = float(quote.ask if side == OrderSide.BUY else quote.bid) or float(quote.last)
        if price:
            est_price = price
        est_total = est_price * quantity

        confirm_id = str(uuid.uuid4())
        self._pending_contracts[confirm_id] = contract

        preview = {
            "symbol":          symbol,
            "side":            action,
            "quantity":        quantity,
            "estimated_price": est_price,
            "estimated_total": est_total,
            "currency":        contract.currency or "USD",
            "exchange":        (getattr(contract, "primaryExchange", None)
                               or getattr(contract, "primaryExch", None)
                               or contract.exchange or "SMART"),
            "bid":             float(quote.bid),
            "ask":             float(quote.ask),
        }
        return preview, confirm_id

    async def submit_order(
        self,
        symbol:     str,
        quantity:   float,
        side:       OrderSide,
        confirm_id: str,
        preview:    dict,   # noqa: ARG002 — kept for CLI interface parity with ScalableBroker
        order_type: OrderType = OrderType.MARKET,
        price:      Optional[float] = None,
    ) -> Order:
        """Place the order using the contract cached by preview_order."""
        self._require_connected()
        ib_async  = _require_ib_async()
        contract  = self._pending_contracts.pop(confirm_id, None)
        if contract is None:
            # confirm_id expired or skipped preview — re-qualify
            contract = ib_async.Stock(symbol, "SMART", "USD")
            await self._ib.qualifyContractsAsync(contract)

        action = "BUY" if side == OrderSide.BUY else "SELL"

        # IBKR does not accept market orders outside RTH — promote to limit
        if not self.config.use_rth and order_type == OrderType.MARKET and price is None:
            price      = float(preview.get("ask" if side == OrderSide.BUY else "bid", 0)
                               or preview.get("estimated_price", 0))
            order_type = OrderType.LIMIT
            logger.info("[%s] After-hours: promoted MARKET → LIMIT @ %.4f", self.broker_id, price)

        ib_order = self._build_ib_order(ib_async, action, quantity, order_type, price)

        trade    = self._ib.placeOrder(contract, ib_order)
        order_id = str(trade.order.orderId)
        self._trades[order_id] = trade

        submitted = Order(
            id=order_id, symbol=symbol,
            order_type=order_type, side=side,
            quantity=quantity, price=price or 0.0,
            status=OrderStatus.SUBMITTED,
            placed_timestamp=dt.datetime.now(dt.timezone.utc),
            filled_timestamp=None, cancelled_timestamp=None,
            average_fill_price=0.0, fees=0.0, leverage=1.0,
            broker_order_id=order_id,
        )
        await self._emit(BrokerEvent.ORDER_SUBMITTED, data=submitted)
        logger.info("[%s] Order %s submitted — %s %s x%.0f @ %s",
                    self.broker_id, order_id, action, symbol, quantity,
                    f"{price}" if price else "MKT")

        if order_type == OrderType.MARKET:
            return await self._wait_for_fill(trade, submitted)
        else:
            task = asyncio.create_task(self._poll_trade_bg(trade, submitted))
            self._order_poll_tasks[order_id] = task
            return submitted

    # ── Direct place_order (used by close_position / external callers) ────────

    async def place_order(
        self,
        symbol:     str,
        quantity:   float,
        side:       OrderSide,
        order_type: OrderType,
        price:      Optional[float] = None,
        *,
        stop_loss:     Optional[float] = None,
        take_profit:   Optional[float] = None,
        time_in_force: str = "GTC",
        notes:         Optional[str] = None,
    ) -> Order:
        """Direct order placement — qualifies contract then calls submit_order."""
        self._require_connected()
        ib_async = _require_ib_async()
        contract = ib_async.Stock(symbol, "SMART", "USD")
        await self._ib.qualifyContractsAsync(contract)

        action   = "BUY" if side == OrderSide.BUY else "SELL"
        ib_order = self._build_ib_order(ib_async, action, quantity, order_type, price, tif=time_in_force)
        trade    = self._ib.placeOrder(contract, ib_order)
        order_id = str(trade.order.orderId)
        self._trades[order_id] = trade

        submitted = Order(
            id=order_id, symbol=symbol,
            order_type=order_type, side=side,
            quantity=quantity, price=price or 0.0,
            status=OrderStatus.SUBMITTED,
            placed_timestamp=dt.datetime.now(dt.timezone.utc),
            filled_timestamp=None, cancelled_timestamp=None,
            average_fill_price=0.0, fees=0.0, leverage=1.0,
            broker_order_id=order_id,
        )
        await self._emit(BrokerEvent.ORDER_SUBMITTED, data=submitted)

        if order_type == OrderType.MARKET:
            return await self._wait_for_fill(trade, submitted)
        task = asyncio.create_task(self._poll_trade_bg(trade, submitted))
        self._order_poll_tasks[order_id] = task
        return submitted

    # ── Orders ────────────────────────────────────────────────────────────────

    async def cancel_order(self, order_id: str) -> bool:
        self._require_connected()
        trade = self._trades.get(order_id)
        if trade is None:
            for t in self._ib.trades():
                if str(t.order.orderId) == order_id:
                    trade = t
                    break
        if trade is None:
            logger.warning("[%s] cancel_order: order %s not found", self.broker_id, order_id)
            return False
        self._ib.cancelOrder(trade.order)
        await self._emit(BrokerEvent.ORDER_CANCELLED, data={"order_id": order_id})
        logger.info("[%s] Cancelled order %s", self.broker_id, order_id)
        return True

    async def get_order(self, order_id: str) -> Optional[Order]:
        self._require_connected()
        for t in self._ib.trades():
            if str(t.order.orderId) == order_id:
                return self._map_trade(t)
        return None

    async def get_orders(
        self,
        symbol: Optional[str] = None,
        status: Optional[OrderStatus] = None,
    ) -> List[Order]:
        self._require_connected()
        orders = [self._map_trade(t) for t in self._ib.trades()]
        if symbol:
            orders = [o for o in orders if o.symbol.upper() == symbol.upper()]
        if status:
            orders = [o for o in orders if o.status == status]
        return orders

    async def get_pending_orders(self) -> List[Order]:
        """Return open / working orders (stop, limit, pending)."""
        return await self.get_orders(status=OrderStatus.SUBMITTED)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _on_ib_error(self, req_id: int, code: int, msg: str, contract) -> None:  # noqa: ARG002
        # Informational codes that are not errors
        _SILENT = {
            202,   # Order Cancelled — cancel acknowledgement
            399,   # Warning: order queued until market open
            404,   # Order doesn't exist (cancel of already-filled order)
            2104,  # Market data farm connection ok
            2106,  # HMDS data farm connection ok
            2108,  # Market data farm inactive but available on demand
            2109,  # outsideRth ignored for this order type — order still processes
            2119,  # Market data farm connecting
            2158,  # Sec-def data farm connection ok
            10167, # Warning: your order will not be placed until...
        }
        if code in _SILENT:
            logger.debug("[%s] IBKR %d (reqId=%d): %s", self.broker_id, code, req_id, msg)
        else:
            logger.warning("[%s] IBKR %d (reqId=%d): %s", self.broker_id, code, req_id, msg)

    def _build_ib_order(self, ib_async, action: str, quantity: float,
                        order_type: OrderType, price: Optional[float],
                        tif: str = "GTC"):
        outside_rth = not self.config.use_rth
        if order_type == OrderType.MARKET:
            order = ib_async.MarketOrder(action, quantity)
        elif order_type == OrderType.LIMIT:
            if not price:
                raise ValueError("Limit order requires a price")
            order = ib_async.LimitOrder(action, quantity, price, tif=tif)
        elif order_type == OrderType.STOP:
            if not price:
                raise ValueError("Stop order requires a price")
            order = ib_async.StopOrder(action, quantity, price, tif=tif)
        else:
            order = ib_async.MarketOrder(action, quantity)
        order.outsideRth = outside_rth
        if outside_rth:
            order.tif = tif  # override account preset (prevents error 10349)
        return order

    async def _wait_for_fill(self, trade, submitted_order: Order) -> Order:
        """Block until market order fills, printing a live elapsed ticker."""
        t0 = time.time()
        while not trade.isDone() and time.time() - t0 < _FILL_TIMEOUT:
            elapsed = int(time.time() - t0)
            print(f"\r⏳ Waiting for fill… {elapsed}s", end="", flush=True)
            await asyncio.sleep(_POLL_SLEEP)
        print()

        status = trade.orderStatus.status
        if status == "Filled":
            return self._build_filled_order(trade, submitted_order)
        elif status in ("Cancelled", "ApiCancelled", "Inactive"):
            raise RuntimeError(f"Order {submitted_order.id} {status}")
        else:
            raise RuntimeError(
                f"Order {submitted_order.id} timed out after {_FILL_TIMEOUT:.0f}s "
                f"(status={status}) — check IBKR app"
            )

    def _build_filled_order(self, trade, submitted_order: Order) -> Order:
        fill_price = float(trade.orderStatus.avgFillPrice or 0)
        fill_qty   = float(trade.orderStatus.filled or submitted_order.quantity)
        filled = Order(
            id=submitted_order.id,
            symbol=submitted_order.symbol,
            order_type=submitted_order.order_type,
            side=submitted_order.side,
            quantity=fill_qty,
            price=fill_price,
            status=OrderStatus.FILLED,
            placed_timestamp=submitted_order.placed_timestamp,
            filled_timestamp=dt.datetime.now(dt.timezone.utc),
            cancelled_timestamp=None,
            average_fill_price=fill_price,
            fees=0.0,
            leverage=1.0,
            broker_order_id=submitted_order.broker_order_id,
        )
        asyncio.ensure_future(self._emit(BrokerEvent.ORDER_FILLED, data=filled))
        asyncio.ensure_future(self._emit(BrokerEvent.POSITION_OPENED, data=filled))
        self._position_cache = None
        logger.info("[%s] %s FILLED @ %.4f (%.0f shares)",
                    self.broker_id, submitted_order.id, fill_price, fill_qty)
        return filled

    async def _poll_trade_bg(self, trade, submitted_order: Order) -> None:
        """Background task for stop / limit orders — waits until terminal state."""
        order_id = submitted_order.id
        try:
            while not trade.isDone():
                await asyncio.sleep(5)
            status = trade.orderStatus.status
            if status == "Filled":
                self._build_filled_order(trade, submitted_order)
                logger.info("[%s] BG: %s filled", self.broker_id, order_id)
            elif status in ("Cancelled", "ApiCancelled", "Inactive"):
                rejected = Order(
                    id=order_id, symbol=submitted_order.symbol,
                    order_type=submitted_order.order_type, side=submitted_order.side,
                    quantity=submitted_order.quantity, price=submitted_order.price,
                    status=OrderStatus.REJECTED,
                    placed_timestamp=submitted_order.placed_timestamp,
                    filled_timestamp=None,
                    cancelled_timestamp=dt.datetime.now(dt.timezone.utc),
                    average_fill_price=0.0, fees=0.0, leverage=1.0,
                    broker_order_id=order_id, reject_reason=status,
                )
                await self._emit(BrokerEvent.ORDER_REJECTED, data=rejected)
        except asyncio.CancelledError:
            pass
        finally:
            self._order_poll_tasks.pop(order_id, None)

    def _map_trade(self, trade) -> Order:
        c        = trade.contract
        o        = trade.order
        os       = trade.orderStatus
        order_id = str(o.orderId)

        status_map = {
            "Submitted":    OrderStatus.SUBMITTED,
            "PreSubmitted": OrderStatus.SUBMITTED,
            "Filled":       OrderStatus.FILLED,
            "Cancelled":    OrderStatus.CANCELLED,
            "ApiCancelled": OrderStatus.CANCELLED,
            "Inactive":     OrderStatus.REJECTED,
        }
        status = status_map.get(os.status, OrderStatus.SUBMITTED)
        side   = OrderSide.BUY if o.action == "BUY" else OrderSide.SELL

        raw_type = str(o.orderType).upper()
        if "LMT" in raw_type or "LIMIT" in raw_type:
            order_type = OrderType.LIMIT
        elif "STP" in raw_type or "STOP" in raw_type:
            order_type = OrderType.STOP
        else:
            order_type = OrderType.MARKET

        # Extract actual timestamps from the trade log
        placed_ts    = None
        filled_ts    = None
        cancelled_ts = None
        for entry in (trade.log or []):
            t = entry.time
            if placed_ts is None:
                placed_ts = t
            if entry.status in ("Filled",) and filled_ts is None:
                filled_ts = t
            if entry.status in ("Cancelled", "ApiCancelled") and cancelled_ts is None:
                cancelled_ts = t

        # Use filled qty for filled orders; totalQuantity may be zeroed by IBKR after fill
        quantity   = float(os.filled if status == OrderStatus.FILLED else o.totalQuantity)
        fill_price = float(os.avgFillPrice or 0)
        return Order(
            id=order_id, symbol=c.symbol,
            order_type=order_type, side=side,
            quantity=quantity, price=float(o.lmtPrice or o.auxPrice or 0),
            status=status,
            placed_timestamp=placed_ts,
            filled_timestamp=filled_ts,
            cancelled_timestamp=cancelled_ts,
            average_fill_price=fill_price,
            fees=0.0, leverage=1.0,
            broker_order_id=order_id,
        )

    def _require_connected(self) -> None:
        if not self._connected or not self._ib:
            raise RuntimeError(f"[{self.broker_id}] Not connected — call connect() first")


# ── Cross-broker symbol search ────────────────────────────────────────────────

async def search_symbol(sym: str):
    """Returns (found, conId, name, bid, ask, flags) for cross-broker /search. Requires TWS/Gateway."""
    import asyncio
    try:
        import ib_async
    except ImportError:
        return False, "—", "ib_async not installed", 0.0, 0.0, ["ib_async not installed"]

    ib = ib_async.IB()
    try:
        await asyncio.wait_for(
            ib.connectAsync("127.0.0.1", 4002, clientId=99, readonly=True, timeout=5.0),
            timeout=6.0,
        )
        contracts = await asyncio.wait_for(ib.reqMatchingSymbolsAsync(sym), timeout=5.0)
        if not contracts:
            return False, "—", "not found", 0.0, 0.0, ["not found"]

        best = next(
            (c for c in contracts
             if c.contract.secType == "STK" and c.contract.currency == "USD"),
            contracts[0],
        )
        con    = best.contract
        name   = getattr(best, "longName", "") or con.symbol
        con_id = str(con.conId)

        ib.reqMarketDataType(3)
        ticker = ib.reqMktData(con, "", True, False)
        await asyncio.sleep(1.5)
        bid = float(ticker.bid or ticker.last or 0)
        ask = float(ticker.ask or ticker.last or 0)
        ib.cancelMktData(con)

        return True, f"conId {con_id}", name, bid, ask, ["long", "short (if shares available)", "leveraged (margin)"]
    except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
        return False, "—", "TWS not running", 0.0, 0.0, ["TWS not running"]
    except Exception as e:
        return False, "—", str(e)[:20], 0.0, 0.0, [str(e)[:20]]
    finally:
        if ib.isConnected():
            ib.disconnect()

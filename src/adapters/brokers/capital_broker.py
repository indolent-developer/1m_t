"""
adapters.brokers.capital_broker

Capital.com REST broker adapter.

Auth flow:
  POST /api/v1/session  → CST + X-SECURITY-TOKEN headers
  Session expires after ~10 min of inactivity; keep-alive pings every 8 min.

Position ID convention: dealId (str) e.g. "abc123"

Order flow (2-phase, matches CLI):
  preview_order()  → fetches live quote, caches order params, returns estimate + confirm_id
  submit_order()   → retrieves cached params, POSTs to /positions or /workingorders,
                     polls /confirms/{dealReference} until accepted/rejected
"""
from __future__ import annotations

import asyncio
import datetime as dt
from core.utils.log_helper import getLogger
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

from adapters.brokers.base_broker import BaseBroker
from adapters.brokers.entities.broker_event import BrokerEvent
from core.config.config_models import CapitalBrokerConfig
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

_CONFIRM_RETRIES = 8
_CONFIRM_SLEEP   = 0.5


class CapitalBroker(BaseBroker):
    """
    Capital.com broker adapter (REST API v1).

    Usage:
        config = CapitalBrokerConfig(api_key="...", username="...", password="...")
        broker = CapitalBroker(config)
        await broker.connect()
        prev, cid = await broker.preview_order("APPL", 10, OrderSide.BUY, OrderType.MARKET)
        order = await broker.submit_order("APPL", 10, OrderSide.BUY, cid, prev)
    """

    def __init__(self, config: CapitalBrokerConfig) -> None:
        super().__init__(config)
        self.config: CapitalBrokerConfig = config
        self._cst:            Optional[str] = None
        self._security_token: Optional[str] = None
        self._connected = False
        self._http: Optional[httpx.AsyncClient] = None
        # confirm_id → {symbol, quantity, side, order_type, price}
        self._pending_orders: Dict[str, dict] = {}

    # ── Capabilities ──────────────────────────────────────────────────────────

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            stock_trading=True,
            etf_trading=True,
            knock_out_trading=True,
            fractional_shares=True,
            bracket_orders=True,
            short_selling=True,
            real_time_quotes=True,
            historical_data=True,
        )

    @property
    def supports_fractional_shares(self) -> bool:
        return True

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _base_url(self) -> str:
        return self.config.base_url.rstrip("/")

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "X-CAP-API-KEY":    self.config.api_key,
            "CST":              self._cst or "",
            "X-SECURITY-TOKEN": self._security_token or "",
            "Content-Type":     "application/json",
        }

    async def _get(self, path: str) -> Any:
        resp = await self._http.get(
            f"{self._base_url()}{path}", headers=self._auth_headers(), timeout=10.0,
        )
        if resp.status_code == 401:
            await self._refresh_session()
            resp = await self._http.get(
                f"{self._base_url()}{path}", headers=self._auth_headers(), timeout=10.0,
            )
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, body: dict) -> Any:
        resp = await self._http.post(
            f"{self._base_url()}{path}", headers=self._auth_headers(), json=body, timeout=10.0,
        )
        if resp.status_code == 401:
            await self._refresh_session()
            resp = await self._http.post(
                f"{self._base_url()}{path}", headers=self._auth_headers(), json=body, timeout=10.0,
            )
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str) -> Any:
        resp = await self._http.delete(
            f"{self._base_url()}{path}", headers=self._auth_headers(), timeout=10.0,
        )
        if resp.status_code == 401:
            await self._refresh_session()
            resp = await self._http.delete(
                f"{self._base_url()}{path}", headers=self._auth_headers(), timeout=10.0,
            )
        resp.raise_for_status()
        return resp.json()

    # ── Session ────────────────────────────────────────────────────────────────

    async def _create_session(self) -> None:
        from infrastructure.cache.capital_session import get_capital_session
        self._cst, self._security_token = await get_capital_session(
            api_key=self.config.api_key,
            username=self.config.username,
            password=self.config.password,
            http=self._http,
            base_url=self._base_url(),
        )
        logger.info("[%s] Session ready (from cache or fresh)", self.broker_id)

    async def _refresh_session(self) -> None:
        from infrastructure.cache.capital_session import clear_capital_session
        await clear_capital_session()
        await self._create_session()

    async def _keep_alive_loop(self) -> None:
        interval = self.config.keep_alive_interval_seconds
        while self._connected:
            await asyncio.sleep(interval)
            try:
                await self._get("/api/v1/ping")
            except Exception as e:
                logger.warning("[%s] Keep-alive ping failed: %s", self.broker_id, e)

    # ── Connection lifecycle ───────────────────────────────────────────────────

    async def connect(self) -> bool:
        try:
            self._http = httpx.AsyncClient()
            await self._create_session()
            self._connected = True
            self._start_background_task(self._keep_alive_loop())
            await self._emit(BrokerEvent.CONNECTED)
            logger.info("[%s] Connected", self.broker_id)
            return True
        except Exception as e:
            logger.error("[%s] connect() failed: %s", self.broker_id, e)
            await self._emit(BrokerEvent.CONNECTION_LOST, error=str(e))
            return False

    async def disconnect(self) -> bool:
        self._connected = False
        await self._cancel_background_tasks()
        try:
            await self._delete("/api/v1/session")
        except Exception:
            pass
        if self._http:
            await self._http.aclose()
            self._http = None
        await self._emit(BrokerEvent.DISCONNECTED)
        logger.info("[%s] Disconnected", self.broker_id)
        return True

    # ── Account ────────────────────────────────────────────────────────────────

    async def get_account_info(self) -> AccountInfo:
        self._require_connected()
        now = time.time()
        if self._account_cache and (now - self._account_cache_ts) < self.config.account_cache_ttl_seconds:
            return self._account_cache

        data     = await self._get("/api/v1/accounts")
        accounts = data.get("accounts", [])
        acct     = next((a for a in accounts if a.get("preferred")), accounts[0] if accounts else {})
        bal      = acct.get("balance", {})

        info = AccountInfo(
            account_id=str(acct.get("accountId", "")),
            account_name=acct.get("accountName", "Capital.com"),
            status=acct.get("status", "ENABLED").lower(),
            account_type=acct.get("accountType", "CFD").lower(),
            currency=acct.get("currency", "EUR"),
            cash_in_hand=float(bal.get("deposit", 0)),
            current_value=float(bal.get("balance", 0)),
            margin_used=float(bal.get("balance", 0)) - float(bal.get("available", 0)),
            margin_available=float(bal.get("available", 0)),
            leverage=self._infer_leverage(acct),
            broker_specific_data=acct,
        )
        self._account_cache    = info
        self._account_cache_ts = now
        await self._emit(BrokerEvent.ACCOUNT_UPDATE, data=info)
        await self.check_risk_limits(info)
        return info

    def _infer_leverage(self, acct: dict) -> float:
        bal       = acct.get("balance", {})
        available = float(bal.get("available", 0))
        deposit   = float(bal.get("deposit", 1))
        if deposit > 0 and available > deposit:
            return round(available / deposit, 1)
        return 1.0

    # ── Positions ──────────────────────────────────────────────────────────────

    async def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        self._require_connected()
        data      = await self._get("/api/v1/positions")
        positions = []
        for raw in data.get("positions", []):
            pos = self._map_position(raw)
            if symbol is None or pos.symbol == symbol:
                positions.append(pos)
        return positions

    async def get_position(self, symbol: str) -> Optional[Position]:
        positions = await self.get_positions(symbol)
        return positions[0] if positions else None

    def _map_position(self, raw: dict) -> Position:
        pos    = raw.get("position", {})
        mkt    = raw.get("market", {})
        size   = float(pos.get("size", 0))
        level  = float(pos.get("level", 0))
        profit = float(pos.get("profit", 0))
        return Position(
            id=str(pos.get("dealId", "")),
            symbol=mkt.get("epic", mkt.get("instrumentName", "")),
            quantity=size,
            average_price=level,
            market_value=size * float(mkt.get("bid", level)),
            unrealized_pnl=profit,
            currency=pos.get("currency", "EUR"),
        )

    async def close_position(self, position_id: str, quantity: Optional[float] = None) -> bool:
        self._require_connected()
        try:
            if quantity is not None:
                # Partial close: reverse market order
                positions = await self.get_positions()
                pos = next((p for p in positions if p.id == position_id), None)
                if pos is None:
                    raise RuntimeError(f"Position {position_id} not found")
                reverse = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
                await self.place_order(pos.symbol, quantity, reverse, OrderType.MARKET)
                await self._emit(BrokerEvent.POSITION_UPDATED)
                return True

            resp     = await self._delete(f"/api/v1/positions/{position_id}")
            deal_ref = resp.get("dealReference")
            if deal_ref:
                for _ in range(_CONFIRM_RETRIES):
                    await asyncio.sleep(_CONFIRM_SLEEP)
                    try:
                        data   = await self._get(f"/api/v1/confirms/{deal_ref}")
                        status = data.get("status", "").upper()
                        if status in ("ACCEPTED", "CLOSED"):
                            break
                        if status == "REJECTED":
                            raise RuntimeError(f"Close rejected: {data.get('reason')}")
                    except RuntimeError:
                        raise
                    except Exception:
                        pass
            await self._emit(BrokerEvent.POSITION_CLOSED)
            logger.info("[%s] Closed position %s", self.broker_id, position_id)
            return True
        except Exception as e:
            logger.warning("[%s] close_position %s failed: %s", self.broker_id, position_id, e)
            return False

    # ── Orders ─────────────────────────────────────────────────────────────────

    async def preview_order(
        self,
        symbol:     str,
        quantity:   float,
        side:       OrderSide = OrderSide.BUY,
        order_type: OrderType = OrderType.MARKET,
        price:      Optional[float] = None,
    ) -> tuple:
        """
        Fetch a live quote and return an estimated cost summary + confirm_id.
        Caches order params so submit_order can place without re-fetching.
        """
        self._require_connected()
        quote  = await self.get_quote(symbol)
        action = "BUY" if side == OrderSide.BUY else "SELL"

        est_price = float(quote.ask if side == OrderSide.BUY else quote.bid) or float(quote.last or 0)
        if price:
            est_price = price
        est_total = est_price * quantity

        confirm_id = str(uuid.uuid4())
        self._pending_orders[confirm_id] = {
            "symbol": symbol, "quantity": quantity,
            "side": side, "order_type": order_type, "price": price,
        }
        preview = {
            "symbol":          symbol,
            "side":            action,
            "quantity":        quantity,
            "estimated_price": est_price,
            "estimated_total": est_total,
            "currency":        "USD",
            "bid":             float(quote.bid or 0),
            "ask":             float(quote.ask or 0),
        }
        return preview, confirm_id

    async def submit_order(
        self,
        symbol:     str,
        quantity:   float,
        side:       OrderSide,
        confirm_id: str,
        preview:    dict,   # noqa: ARG002 — kept for CLI interface parity
        order_type: OrderType = OrderType.MARKET,
        price:      Optional[float] = None,
    ) -> Order:
        pending = self._pending_orders.pop(confirm_id, None)
        if pending:
            order_type = pending.get("order_type", order_type)
            price      = pending.get("price", price)
        return await self.place_order(symbol, quantity, side, order_type, price)

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
        notes:         Optional[str]  = None,  # noqa: ARG002
    ) -> Order:
        self._require_connected()
        direction = "BUY" if side == OrderSide.BUY else "SELL"

        if order_type == OrderType.MARKET:
            body: dict = {
                "epic":          symbol,
                "direction":     direction,
                "size":          quantity,
                "guaranteedStop": False,
            }
            if stop_loss:
                body["stopLevel"] = stop_loss
            if take_profit:
                body["profitLevel"] = take_profit

            resp     = await self._post("/api/v1/positions", body)
            deal_ref = resp.get("dealReference")
            return await self._confirm_deal(symbol, quantity, side, order_type, deal_ref)

        else:
            order_t = "LIMIT" if order_type == OrderType.LIMIT else "STOP"
            body = {
                "epic":          symbol,
                "direction":     direction,
                "size":          quantity,
                "level":         price,
                "type":          order_t,
                "guaranteedStop": False,
                "goodTillDate":  None,  # None = GTC
            }
            if stop_loss:
                body["stopLevel"] = stop_loss
            if take_profit:
                body["profitLevel"] = take_profit

            resp     = await self._post("/api/v1/workingorders", body)
            deal_ref = resp.get("dealReference")
            return await self._confirm_deal(symbol, quantity, side, order_type, deal_ref, is_working=True)

    async def cancel_order(self, order_id: str) -> bool:
        self._require_connected()
        try:
            await self._delete(f"/api/v1/workingorders/{order_id}")
            order = Order(
                id=order_id, symbol="", order_type=OrderType.LIMIT, side=OrderSide.BUY,
                quantity=0, price=0, status=OrderStatus.CANCELLED,
                placed_timestamp=None, filled_timestamp=None,
                cancelled_timestamp=dt.datetime.now(dt.timezone.utc),
                average_fill_price=0.0, fees=0.0, leverage=1.0,
                broker_order_id=order_id,
            )
            await self._emit(BrokerEvent.ORDER_CANCELLED, data=order)
            logger.info("[%s] Cancelled order %s", self.broker_id, order_id)
            return True
        except Exception as e:
            logger.warning("[%s] cancel_order %s failed: %s", self.broker_id, order_id, e)
            return False

    async def get_order(self, order_id: str) -> Optional[Order]:
        orders = await self.get_orders()
        return next((o for o in orders if o.id == order_id), None)

    async def get_orders(
        self,
        symbol: Optional[str] = None,
        status: Optional[OrderStatus] = None,
    ) -> List[Order]:
        self._require_connected()
        data   = await self._get("/api/v1/workingorders")
        orders = []
        for raw in data.get("workingOrders", []):
            o = self._map_working_order(raw)
            if symbol and o.symbol != symbol:
                continue
            if status and o.status != status:
                continue
            orders.append(o)
        return orders

    def _map_working_order(self, raw: dict) -> Order:
        wo  = raw.get("workingOrderData", raw)
        mkt = raw.get("marketData", {})
        t   = wo.get("orderType", "LIMIT").upper()
        d   = wo.get("direction", "BUY").upper()
        return Order(
            id=str(wo.get("dealId", "")),
            symbol=wo.get("epic", mkt.get("epic", "")),
            order_type=OrderType.LIMIT if t == "LIMIT" else OrderType.STOP,
            side=OrderSide.BUY if d == "BUY" else OrderSide.SELL,
            quantity=float(wo.get("size", 0)),
            price=float(wo.get("level", 0) or 0),
            status=OrderStatus.SUBMITTED,
            placed_timestamp=None,
            filled_timestamp=None,
            cancelled_timestamp=None,
            average_fill_price=0.0,
            fees=0.0,
            leverage=1.0,
            broker_order_id=str(wo.get("dealId", "")),
            broker_specific_data=raw,
        )

    # ── Quotes ─────────────────────────────────────────────────────────────────

    async def get_quote(self, symbol: str) -> Quote:
        data = await self._get(f"/api/v1/markets/{symbol}")
        snap = data.get("snapshot", {})
        bid  = float(snap.get("bid",   0) or 0)
        ask  = float(snap.get("offer", 0) or 0)  # Capital uses "offer", not "ask"
        mid  = (bid + ask) / 2 if bid and ask else bid or ask
        return Quote(symbol=symbol, bid=bid, ask=ask, last=mid)

    async def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        results = {}
        for sym in symbols:
            try:
                results[sym] = await self.get_quote(sym)
            except Exception:
                pass
        return results

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _confirm_deal(
        self,
        symbol:     str,
        quantity:   float,
        side:       OrderSide,
        order_type: OrderType,
        deal_ref:   Optional[str],
        is_working: bool = False,
    ) -> Order:
        if not deal_ref:
            raise RuntimeError(f"[{self.broker_id}] No dealReference returned for {symbol}")

        now = dt.datetime.now(dt.timezone.utc)
        for _ in range(_CONFIRM_RETRIES):
            await asyncio.sleep(_CONFIRM_SLEEP)
            try:
                data   = await self._get(f"/api/v1/confirms/{deal_ref}")
                status = data.get("status", "").upper()
                reason = data.get("reason", "")

                if status == "REJECTED":
                    raise RuntimeError(f"Order rejected: {reason}")

                if status in ("ACCEPTED", "OPEN", "WORKING"):
                    deal_id    = str(data.get("dealId", deal_ref))
                    fill_price = float(data.get("level", 0) or 0)
                    order_status = OrderStatus.SUBMITTED if is_working else OrderStatus.FILLED

                    order = Order(
                        id=deal_id,
                        symbol=symbol,
                        order_type=order_type,
                        side=side,
                        quantity=quantity,
                        price=fill_price,
                        status=order_status,
                        placed_timestamp=now,
                        filled_timestamp=now if not is_working else None,
                        cancelled_timestamp=None,
                        average_fill_price=fill_price,
                        fees=0.0,
                        leverage=1.0,
                        deal_reference=deal_ref,
                        broker_order_id=deal_id,
                        broker_specific_data=data,
                    )
                    event = BrokerEvent.ORDER_SUBMITTED if is_working else BrokerEvent.ORDER_FILLED
                    await self._emit(event, data=order)
                    logger.info(
                        "[%s] Deal confirmed — ref=%s id=%s status=%s price=%.4f",
                        self.broker_id, deal_ref, deal_id, status, fill_price,
                    )
                    return order

            except RuntimeError:
                raise
            except Exception:
                pass  # not yet confirmed, retry

        raise RuntimeError(
            f"[{self.broker_id}] Deal {deal_ref} not confirmed after {_CONFIRM_RETRIES} retries"
        )

    # ── Guard ──────────────────────────────────────────────────────────────────

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError(f"[{self.broker_id}] Not connected — call connect() first")


# ── Cross-broker symbol search ────────────────────────────────────────────────

async def search_symbol(sym: str):
    """Returns (found, epic, name, bid, ask, flags) for cross-broker /search."""
    import httpx
    import os
    from infrastructure.cache.capital_session import get_capital_session, clear_capital_session

    api_key  = os.environ.get("CAPITAL_COM_LIVE_API_KEY",  os.environ.get("CAPITAL_API_KEY", ""))
    username = os.environ.get("CAPITAL_COM_USERNAME", os.environ.get("CAPITAL_USERNAME", ""))
    password = os.environ.get("CAPITAL_COM_PASSWORD", os.environ.get("CAPITAL_PASSWORD", ""))
    if not api_key or not username or not password:
        return False, "—", "no credentials", 0.0, 0.0, ["no credentials"]

    base = "https://api-capital.backend-capital.com"
    async with httpx.AsyncClient(timeout=10.0) as http:
        try:
            cst, token = await get_capital_session(api_key, username, password, http, base)
        except RuntimeError as e:
            return False, "—", str(e)[:30], 0.0, 0.0, [str(e)[:30]]

        auth = {"X-CAP-API-KEY": api_key, "CST": cst, "X-SECURITY-TOKEN": token}
        mr   = await http.get(f"{base}/api/v1/markets",
                              headers=auth, params={"searchTerm": sym, "limit": 10})

        if mr.status_code == 401:
            await clear_capital_session()
            try:
                cst, token = await get_capital_session(api_key, username, password, http, base)
            except RuntimeError as e:
                return False, "—", str(e)[:30], 0.0, 0.0, [str(e)[:30]]
            auth = {"X-CAP-API-KEY": api_key, "CST": cst, "X-SECURITY-TOKEN": token}
            mr   = await http.get(f"{base}/api/v1/markets",
                                  headers=auth, params={"searchTerm": sym, "limit": 10})

        if mr.is_error:
            return False, "—", f"search {mr.status_code}", 0.0, 0.0, [f"search {mr.status_code}"]

        markets = mr.json().get("markets", [])
        if not markets:
            return False, "—", "not found", 0.0, 0.0, ["not found"]

        mkt    = next((m for m in markets if m.get("epic", "").upper() == sym), markets[0])
        epic   = mkt.get("epic", "")
        name   = mkt.get("instrumentName", "")
        bid    = float(mkt.get("bid",   0) or 0)
        ask    = float(mkt.get("offer", mkt.get("ask", 0)) or 0)
        modes  = [m.upper() for m in mkt.get("marketModes", [])]
        status = mkt.get("marketStatus", "").upper()

        if "CLOSE_ONLY" in modes:
            flags = ["close only"]
        elif "LONG_ONLY" in modes:
            flags = ["long only", "no short"]
        elif "REGULAR" in modes:
            flags = ["long", "short"]
        elif status == "CLOSED":
            flags = ["market closed"]
        else:
            flags = ["/".join(modes) if modes else status.lower() or "unknown"]

        return True, epic, name, bid, ask, flags

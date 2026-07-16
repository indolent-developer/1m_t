"""
adapters.brokers.etoro_broker

eToro broker adapter — public REST API v1/v2.

Auth (3 headers on every request, no signing required):
  x-api-key:     Public API Key  (ETORO_LIVE_PUBLIC_KEY)  — identifies the app
  x-user-key:    User Key        (ETORO_LIVE_PRIVATE_KEY) — identifies the account
  x-request-id:  UUID generated per request

Keys are created via: eToro → Settings → Trading → API Key Management → Create New Key
Each key is environment-specific (Demo or Real).

Endpoints used:
  GET  /api/v1/money/balances                              — aggregated balance
  GET  /api/v1/portfolios/{env}/positions                  — open positions
  GET  /api/v1/market-data/search?internalSymbolFull=AAPL  — ticker → instrumentId
  GET  /api/v1/market-data/instruments/rates               — live bid/ask
  POST /api/v2/trading/execution/demo/orders               — place order (demo)
  POST /api/v1/trading/real/orders                         — place order (live)

Instrument IDs:
  IDs are numeric and immutable. Resolved once via search and cached to disk at
  data/etoro_instrument_cache.json so the search endpoint is only called once
  per symbol ever.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from adapters.brokers.base_broker import BaseBroker
from adapters.brokers.entities.broker_event import BrokerEvent
from core.config.config_models import eToroBrokerConfig
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
from core.utils.log_helper import getLogger

logger = getLogger(__name__)

_BASE_URL        = "https://public-api.etoro.com"
_TICKER_ISIN_FILE = Path(__file__).resolve().parents[3] / "data" / "ticker_isin.json"

# Fields to request from /market-data/search — keeps response small and debugger-friendly.
# Full field list: https://public-api.etoro.com/index.html (Market Data → Search)
_SEARCH_FIELDS = ",".join([
    "internalInstrumentId",
    "internalSymbolFull",
    "internalInstrumentDisplayName",
    "internalAssetClassName",
    "isCurrentlyTradable",   # single source of truth for tradability
    "isActiveInPlatform",
    "isBuyEnabled",
    "cvtBid",                # converted bid price
    "cvtAsk",
    "displayname" 
                                  # converted ask price
])


class eToroBroker(BaseBroker):
    """
    eToro broker adapter.

    Usage:
        config = eToroBrokerConfig(
            public_key="<ETORO_LIVE_PUBLIC_KEY>",
            private_key="<ETORO_LIVE_PRIVATE_KEY>",
            is_demo=False,
        )
        broker = eToroBroker(config)
        await broker.connect()
        info  = await broker.get_account_info()
        quote = await broker.get_quote("AAPL")
    """

    def __init__(self, config: eToroBrokerConfig) -> None:
        super().__init__(config)
        self.config: eToroBrokerConfig = config
        self._connected = False
        self._http: Optional[httpx.AsyncClient] = None
        self._env = "demo" if config.is_demo else "real"
        self.connect_error: Optional[str] = None

        # symbol (upper) → numeric instrumentId — persisted to disk
        self._instrument_cache: Dict[str, int] = self._load_instrument_cache()
        # reverse: instrumentId → symbol (built lazily from /market-data/instruments)
        self._id_to_symbol: Dict[int, str] = {v: k for k, v in self._instrument_cache.items()}

    # ── Capabilities ──────────────────────────────────────────────────────────

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            stock_trading=True,
            etf_trading=True,
            knock_out_trading=False,
            fractional_shares=True,
            bracket_orders=False,
            short_selling=False,
            real_time_quotes=True,
            historical_data=True,
        )

    @property
    def supports_fractional_shares(self) -> bool:
        return True

    # ── Instrument ID cache ───────────────────────────────────────────────────

    def _load_instrument_cache(self) -> Dict[str, int]:
        try:
            if _TICKER_ISIN_FILE.exists():
                raw = json.loads(_TICKER_ISIN_FILE.read_text())
                return {k: int(v) for k, v in raw.get("etoro", {}).items()}
        except Exception:
            pass
        return {}

    def _save_instrument_cache(self) -> None:
        try:
            _TICKER_ISIN_FILE.parent.mkdir(parents=True, exist_ok=True)
            raw = {}
            if _TICKER_ISIN_FILE.exists():
                raw = json.loads(_TICKER_ISIN_FILE.read_text())
            raw["etoro"] = self._instrument_cache
            _TICKER_ISIN_FILE.write_text(json.dumps(raw, indent=2))
        except Exception as e:
            logger.warning("[%s] Could not save instrument cache: %s", self.broker_id, e)

    async def resolve_instrument_id(self, symbol: str) -> int:
        """
        Resolve a ticker symbol to eToro's numeric instrumentId.
        Checks the disk cache first; calls the search API only on a miss.
        Raises ValueError if the symbol cannot be found.
        """
        key = symbol.upper()
        if key in self._instrument_cache:
            return self._instrument_cache[key]

        data = await self._get(
            "/api/v1/market-data/search",
            params={"internalSymbolFull": key},
        )
        # Actual response: {"page":1,"pageSize":20,"totalItems":N,"items":[{...}]}
        instruments = data.get("items", data.get("instruments", data.get("data", [])))

        # Verify exact match on internalSymbolFull (API may return partial matches)
        match = next(
            (i for i in instruments
             if i.get("internalSymbolFull", "").upper() == key),
            None,
        )
        if match is None:
            raise ValueError(f"[{self.broker_id}] Symbol '{symbol}' not found on eToro")

        # Search response uses "internalInstrumentId"
        instrument_id = int(
            match.get("internalInstrumentId")
            or match.get("instrumentID")
            or match.get("instrumentId")
            or 0
        )
        self._instrument_cache[key] = instrument_id
        self._id_to_symbol[instrument_id] = key
        self._save_instrument_cache()
        logger.debug("[%s] Resolved %s → instrumentId=%d", self.broker_id, key, instrument_id)
        return instrument_id

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        if self.config.is_demo:
            pub  = self.config.public_key_demo  or self.config.public_key  or self.config.api_key
            priv = self.config.private_key_demo or self.config.private_key
        else:
            pub  = self.config.public_key_live  or self.config.public_key  or self.config.api_key
            priv = self.config.private_key_live or self.config.private_key
        return {
            "x-api-key":    pub,
            "x-user-key":   priv,
            "x-request-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
            "Accept":       "application/json",
        }

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.is_error:
            raise RuntimeError(
                f"HTTP {resp.status_code} {resp.request.method} {resp.request.url}  —  {resp.text[:400]}"
            )

    async def _get(self, path: str, params: Optional[dict] = None) -> Any:
        resp = await self._http.get(
            f"{_BASE_URL}{path}",
            headers=self._headers(),
            params=params,
            timeout=self.config.request_timeout_seconds,
        )
        self._raise_for_status(resp)
        return resp.json()

    async def _post(self, path: str, body: dict) -> Any:
        resp = await self._http.post(
            f"{_BASE_URL}{path}",
            headers=self._headers(),
            json=body,
            timeout=self.config.request_timeout_seconds,
        )
        self._raise_for_status(resp)
        return resp.json()

    async def _delete(self, path: str) -> Any:
        resp = await self._http.delete(
            f"{_BASE_URL}{path}",
            headers=self._headers(),
            timeout=self.config.request_timeout_seconds,
        )
        self._raise_for_status(resp)
        return resp.json()

    # ── Connection lifecycle ───────────────────────────────────────────────────

    async def connect(self) -> bool:
        if self.config.is_demo:
            pub  = self.config.public_key_demo  or self.config.public_key  or self.config.api_key
            priv = self.config.private_key_demo or self.config.private_key
        else:
            pub  = self.config.public_key_live  or self.config.public_key  or self.config.api_key
            priv = self.config.private_key_live or self.config.private_key
        if not pub or not priv:
            self.connect_error = "Missing credentials — set ETORO_LIVE_PUBLIC_KEY and ETORO_LIVE_PRIVATE_KEY"
            logger.error("[%s] %s", self.broker_id, self.connect_error)
            return False
        try:
            self._http = httpx.AsyncClient()
            # Validate credentials against a universally-accessible endpoint.
            # Portfolio/balance endpoints (/api/v1/money/balances, /api/v1/portfolios/*)
            # require an upgraded API key tier — they return 404 for UnregisteredApplication keys.
            await self._get("/api/v1/watchlists")
            self._connected = True
            await self._emit(BrokerEvent.CONNECTED)
            logger.info("[%s] Connected (env=%s)", self.broker_id, self._env)
            return True
        except Exception as e:
            self.connect_error = str(e)
            logger.error("[%s] connect() failed: %s", self.broker_id, e)
            await self._emit(BrokerEvent.CONNECTION_LOST, error=str(e))
            return False

    async def disconnect(self) -> bool:
        self._connected = False
        await self._cancel_background_tasks()
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

        # /api/v1/money/balances and /api/v1/portfolios/* require an upgraded API key.
        # Current key type (UnregisteredApplication) only has access to watchlists,
        # market data, and agent-portfolio endpoints.
        # To unlock: eToro → Settings → Trading → API Key Management → Create New Key
        #            and request portfolio/trading permissions.
        raise NotImplementedError(
            "get_account_info requires an upgraded eToro API key with portfolio permissions. "
            "Current key (UnregisteredApplication) does not have access to /api/v1/money/balances. "
            "Go to eToro → Settings → Trading → API Key Management and create a key with Read permissions."
        )

    # ── Positions ──────────────────────────────────────────────────────────────

    async def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        self._require_connected()
        data     = await self._get("/api/v1/trading/info/portfolio")
        raw_list = (
            data.get("positions")
            or data.get("clientPortfolio", {}).get("positions")
            or data.get("data", {}).get("positions")
            or data.get("portfolio", {}).get("positions")
            or []
        )
        logger.info("[%s] get_positions: %d raw positions", self.broker_id, len(raw_list))
        # Collect instrumentIDs that aren't in the cache yet and resolve them
        unknown_ids = [
            r.get("instrumentID") or r.get("instrumentId")
            for r in raw_list
            if (r.get("instrumentID") or r.get("instrumentId")) not in self._id_to_symbol
        ]
        if unknown_ids:
            await self._load_instrument_map()
        positions = []
        for r in raw_list:
            try:
                positions.append(self._map_position(r))
            except Exception as e:
                logger.warning("[%s] _map_position failed for %s: %s", self.broker_id, r, e)
        if symbol:
            positions = [p for p in positions if p.symbol == symbol.upper()]

        # Enrich with live prices — the portfolio endpoint has no current price
        await self._enrich_positions_with_live_prices(positions)
        return positions

    async def _enrich_positions_with_live_prices(self, positions: List[Position]) -> None:
        unique_symbols = list({p.symbol for p in positions if p.symbol})
        if not unique_symbols:
            return
        quotes = await self.get_quotes(unique_symbols)
        for p in positions:
            q = quotes.get(p.symbol)
            if not q:
                continue
            mid = float((q.bid + q.ask) / 2) if q.bid and q.ask else float(q.ask or q.bid or 0)
            if not mid:
                continue
            sign           = 1 if p.side == TradeSide.LONG else -1
            pnl            = sign * (mid - p.average_price) * p.quantity
            collateral     = float((p.additional_info or {}).get("amount") or (p.average_price * p.quantity / max(p.leverage, 1)))
            pnl_pct        = (pnl / collateral * 100) if collateral else 0.0
            p.market_value              = mid * p.quantity
            p.unrealized_pnl            = pnl
            p.unrealized_pnl_percentage = pnl_pct

    async def _load_instrument_map(self) -> None:
        """Bulk-load instrumentID → ticker from /api/v1/market-data/instruments (symbolFull field)."""
        try:
            data  = await self._get(
                "/api/v1/market-data/instruments",
                params={"fields": "instrumentID,symbolFull"},
            )
            items = data.get("instrumentDisplayDatas", data.get("items", data.get("data", [])))
            for item in items:
                iid    = item.get("instrumentID")
                ticker = item.get("symbolFull")
                if iid and ticker:
                    self._id_to_symbol[int(iid)]       = ticker.upper()
                    self._instrument_cache[ticker.upper()] = int(iid)
            self._save_instrument_cache()
            logger.info("[%s] Loaded %d instruments from eToro", self.broker_id, len(self._id_to_symbol))
        except Exception as e:
            logger.warning("[%s] Could not load instrument map: %s", self.broker_id, e)

    async def get_position(self, symbol: str) -> Optional[Position]:
        positions = await self.get_positions(symbol)
        return positions[0] if positions else None

    def _map_position(self, raw: dict) -> Position:
        direction = raw.get("direction", raw.get("isBuy", True))
        side      = TradeSide.LONG if direction in (True, "buy", "BUY", 1) else TradeSide.SHORT
        # Resolve instrumentID → ticker; field name varies across endpoints
        iid = (
            raw.get("instrumentID")
            or raw.get("instrumentId")
            or raw.get("internalInstrumentId")
        )
        symbol = (
            (self._id_to_symbol.get(int(iid)) if iid else None)
            or raw.get("internalSymbolFull")
            or raw.get("symbolFull")
            or str(iid or "")
        )
        return Position(
            id=str(raw.get("positionId", raw.get("id", ""))),
            symbol=symbol,
            side=side,
            open_date=dt.datetime.now(),
            close_date=None,
            quantity=float(raw.get("units",         raw.get("quantity",     0))),
            average_price=float(raw.get("openRate", raw.get("averagePrice", 0))),
            leverage=float(raw.get("leverage", 1.0)),
            market_value=float(raw.get("currentValue",      raw.get("marketValue",    0))),
            unrealized_pnl=float(raw.get("profit",          raw.get("unrealizedPnl",  0))),
            unrealized_pnl_percentage=float(raw.get("profitPercentage", 0)),
            realized_pnl=0.0,
            realized_pnl_percentage=0.0,
            stop_loss_price=float(raw.get("stopLossRate",   0) or 0),
            take_profit_price=float(raw.get("takeProfitRate", 0) or 0),
            additional_info=raw,
        )

    async def close_position(self, position_id: str, quantity: Optional[float] = None) -> bool:
        path = (
            f"/api/v2/trading/execution/demo/orders/close"
            if self.config.is_demo else
            f"/api/v1/trading/real/orders/close"
        )
        await self._post(path, {"positionId": position_id})
        await self._emit(BrokerEvent.POSITION_CLOSED, data={"position_id": position_id})
        return True

    # ── Orders ─────────────────────────────────────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        quantity: float,
        side: OrderSide,
        order_type: OrderType,
        price: Optional[float] = None,
        *,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        time_in_force: str = "GTC",
        notes: Optional[str] = None,
    ) -> Order:
        self._require_connected()

        instrument_id = await self.resolve_instrument_id(symbol)

        # Check live rate before submitting (eToro recommendation)
        rates = await self._get(
            "/api/v1/market-data/instruments/rates",
            params={"instrumentIds": instrument_id},
        )
        rate_data = (rates.get("rates") or rates.get("data") or [{}])[0]
        logger.info(
            "[%s] Pre-order rate check %s: bid=%s ask=%s",
            self.broker_id, symbol,
            rate_data.get("bid"), rate_data.get("ask"),
        )

        body: Dict[str, Any] = {
            "instrumentId": instrument_id,
            "isBuy":        side == OrderSide.BUY,
            "leverage":     1,
            "units":        quantity,   # exactly one of: amount | units | contracts
        }
        if stop_loss:
            body["stopLossRate"] = stop_loss
        if take_profit:
            body["takeProfitRate"] = take_profit

        path = (
            "/api/v2/trading/execution/demo/orders"
            if self.config.is_demo else
            "/api/v1/trading/real/orders"
        )
        resp = await self._post(path, body)

        order = self._map_order(resp, symbol, side, order_type, quantity)
        await self._emit(BrokerEvent.ORDER_SUBMITTED, data=order)
        return order

    def _map_order(
        self,
        raw: dict,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: float,
    ) -> Order:
        return Order(
            id=str(raw.get("orderId", raw.get("positionId", raw.get("id", "")))),
            symbol=symbol,
            order_type=order_type,
            side=side,
            quantity=quantity,
            price=float(raw.get("openRate", raw.get("rate", 0)) or 0),
            status=OrderStatus.SUBMITTED,
            placed_timestamp=dt.datetime.now(),
            filled_timestamp=None,
            cancelled_timestamp=None,
            average_fill_price=float(raw.get("openRate", raw.get("rate", 0)) or 0),
            fees=float(raw.get("fee", 0) or 0),
            leverage=float(raw.get("leverage", 1)),
            broker_specific_data=raw,
        )

    async def cancel_order(self, order_id: str) -> bool:
        path = (
            f"/api/v2/trading/execution/demo/orders/{order_id}"
            if self.config.is_demo else
            f"/api/v1/trading/real/orders/{order_id}"
        )
        await self._delete(path)
        await self._emit(BrokerEvent.ORDER_CANCELLED, data={"order_id": order_id})
        return True

    async def get_order(self, order_id: str) -> Optional[Order]:
        raise NotImplementedError("get_order not yet implemented for eToroBroker")

    async def get_orders(
        self,
        symbol: Optional[str] = None,
        status: Optional[OrderStatus] = None,
    ) -> List[Order]:
        raise NotImplementedError("get_orders not yet implemented for eToroBroker")

    # ── Quotes ─────────────────────────────────────────────────────────────────

    def _map_rate(self, symbol: str, r: dict) -> Quote:
        ts_raw = r.get("date", "")
        try:
            ts = dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) if ts_raw else dt.datetime.now()
        except ValueError:
            ts = dt.datetime.now()
        from decimal import Decimal
        return Quote(
            symbol=symbol,
            instrument_type=InstrumentType.STOCK,
            bid=Decimal(str(r.get("bid",  0))),
            ask=Decimal(str(r.get("ask",  0))),
            last=Decimal(str(r.get("lastExecution", r.get("last", 0)))),
            bid_size=0,
            ask_size=0,
            volume=0,
            timestamp=ts,
        )

    async def get_quote(self, symbol: str) -> Quote:
        self._require_connected()
        instrument_id = await self.resolve_instrument_id(symbol)
        data = await self._get(
            "/api/v1/market-data/instruments/rates",
            params={"instrumentIds": instrument_id},
        )
        rates = data.get("rates", data.get("data", [{}]))
        r = rates[0] if rates else {}
        # Actual response fields: instrumentID, ask, bid, lastExecution, date
        return self._map_rate(symbol, r)

    async def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        self._require_connected()
        # The rates endpoint only processes the last instrumentIds value regardless
        # of how params are encoded — fetch each symbol individually and gather.
        async def _one(sym: str):
            try:
                return sym, await self.get_quote(sym)
            except Exception as e:
                logger.warning("[%s] get_quote(%s) failed: %s", self.broker_id, sym, e)
                return sym, None

        pairs = await asyncio.gather(*(_one(s) for s in symbols))
        return {sym: q for sym, q in pairs if q is not None}

    # ── Guard ──────────────────────────────────────────────────────────────────

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError(f"[{self.broker_id}] Not connected — call connect() first")


# ── Cross-broker symbol search ────────────────────────────────────────────────

async def search_symbol(sym: str):
    """Returns (found, instrumentId, name, bid, ask, flags) for cross-broker /search."""
    import httpx
    import os
    import uuid as _uuid

    pub  = os.environ.get("ETORO_LIVE_PUBLIC_KEY",  "")
    priv = os.environ.get("ETORO_LIVE_PRIVATE_KEY", "")
    if not pub or not priv:
        return False, "—", "no credentials", 0.0, 0.0, ["no credentials"]

    base    = "https://public-api.etoro.com"
    headers = {
        "x-api-key":    pub,
        "x-user-key":   priv,
        "x-request-id": str(_uuid.uuid4()),
        "Accept":       "application/json",
    }
    async with httpx.AsyncClient(timeout=10.0) as http:
        
        url=f"{base}/api/v1/market-data/search?internalSymbolFull={sym.upper()}&fields={_SEARCH_FIELDS}"
        r = await http.get(
            url,
            headers=headers,
        )
        if r.is_error:
            return False, "—", f"HTTP {r.status_code}", 0.0, 0.0, [f"HTTP {r.status_code}"]
        items = r.json().get("items", r.json().get("instruments", []))
        match = next((i for i in items
                      if i.get("internalSymbolFull", "").upper() == sym), None)
        if not match:
            return False, "—", "not found", 0.0, 0.0, ["not found"]

        iid  = int(match.get("internalInstrumentId", 0))
        name = match.get("internalInstrumentDisplayName", sym)
        bid  = float(match.get("cvtBid", 0) or 0)
        ask  = float(match.get("cvtAsk", 0) or 0)

        tradable = match.get("isCurrentlyTradable")
        active   = match.get("isActiveInPlatform")
        buyable  = match.get("isBuyEnabled")

        if tradable is None and active is None and buyable is None:
            logger.debug("[eToro] No tradability fields for %s", sym)
            flags = ["tradability unknown"]
        else:
            flags = []
            if tradable is False or active is False:
                flags.append("not tradable")
            elif buyable is False:
                flags.append("buy disabled")
            else:
                flags.append("long")

        return True, f"ID {iid}", name, bid, ask, flags

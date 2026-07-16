"""
adapters.brokers.scalable_broker

Scalable Capital broker adapter — CLI subprocess wrapper.

Order flow:
  1. place_order() calls: sc orders buy SYMBOL --quantity N --yes
  2. Returns Order with status=SUBMITTED immediately
  3. Background asyncio.Task polls: sc orders get ORDER_ID
     until the order appears as filled or failed
  4. Emits ORDER_FILLED or ORDER_REJECTED

Position ID convention:
  "{isin}_{orderId}" — Scalable identifies instruments by ISIN.

Important constraints:
  - No demo/paper account — all orders are live
  - readonly=True in config blocks all order placement (development safety)
  - CLI must be installed and authenticated before use: sc login
  - No bracket orders (no native stop/take-profit support)
  - No short selling (long-only broker)
  - No streaming quotes (polling only)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from core.utils.log_helper import getLogger
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

_ISIN_RE = re.compile(r'^[A-Z]{2}[A-Z0-9]{9}[0-9]$')

from adapters.brokers.base_broker import BaseBroker
from adapters.brokers.entities.broker_event import BrokerEvent
from core.config.config_models import ScalableBrokerConfig
from core.entities.broker_capabilities import BrokerCapabilities
from core.entities.broker_entities import (
    AccountInfo,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TradeSide,
)
from core.entities.market_quotes import Quote
from core.entities.position_types import Position
from core.entities.instrument_type import InstrumentType
from decimal import Decimal

logger = getLogger(__name__)


def _trade_args(
    action:     str,
    isin:       str,
    shares:     float,
    amount_eur: float,
    order_type: "OrderType",
    stop_price: Optional[float] = None,
    limit_price: Optional[float] = None,
) -> list:
    """Build the sc broker trade CLI args for phase 1."""
    from core.entities.broker_entities import OrderType
    args = ["broker", "trade", action, "--isin", isin]
    if action == "buy" and limit_price is not None:
        # --amount + --limit-price causes CONFIRMATION_FIELDS_MISMATCH (Scalable
        # validates that limit_price == their internal calculation, which we can't
        # predict).  --shares + --order-type limit + --limit-price is unambiguous
        # and passes validation cleanly.
        args += ["--shares", str(int(shares))]
    elif action == "buy":
        args += ["--amount", str(amount_eur)]
    else:
        args += ["--shares", str(int(shares))]
    if order_type != OrderType.MARKET:
        args += ["--order-type", order_type.value]
    if stop_price is not None:
        args += ["--stop-price", f"{stop_price:.4f}"]
    if limit_price is not None:
        args += ["--limit-price", f"{limit_price:.4f}"]
    return args


def _parse_phase1(p1d: dict) -> dict:
    """Extract human-readable summary fields from a phase-1 response data dict."""
    result = p1d.get("result", {}) or {}
    conf   = p1d.get("confirmation", {}) or {}
    calc   = result.get("calculation", {}) or {}
    quote  = result.get("market_quote", {}) or {}
    costs  = result.get("ex_ante_costs", {}) or {}
    trade  = result.get("tradability", {}) or {}
    intent = result.get("intent", {}) or {}
    return {
        "isin":       intent.get("isin", ""),
        "amount":     intent.get("amount", ""),
        "order_type": intent.get("order_type", "market"),
        "shares":     calc.get("shares", ""),
        "est_volume": calc.get("estimated_order_volume", ""),
        "ask":        quote.get("ask_price", ""),
        "bid":        quote.get("bid_price", ""),
        "mid":        quote.get("mid_price", ""),
        "currency":   quote.get("currency", "EUR"),
        "venue":      trade.get("selected_venue_label", ""),
        "fee_entry":  (costs.get("entryCosts") or {}).get("total", {}).get("amount", ""),
        "confirm_id": conf.get("id", ""),
        "expires_at": conf.get("expires_at_epoch", 0),
    }


class ScalableBroker(BaseBroker):
    """
    Scalable Capital broker adapter.

    All trading is executed via the official sc CLI binary.
    No public API — CLI is the only supported integration path.

    Usage:
        config = ScalableBrokerConfig(cli_path="sc", readonly=True)
        broker = ScalableBroker(config)
        broker.events.subscribe(BrokerEvent.ORDER_FILLED, on_fill)
        await broker.connect()

        # Live order (ensure readonly=False first)
        order = await broker.place_order("AAPL", 5, OrderSide.BUY, OrderType.MARKET)
    """

    def __init__(self, config: ScalableBrokerConfig) -> None:
        super().__init__(config)
        self.config: ScalableBrokerConfig = config
        self._connected = False

        # order_id → background poll Task
        self._order_poll_tasks: Dict[str, asyncio.Task] = {}
        self._isin_cache:      Dict[str, str] = {}   # symbol/name → ISIN (session cache)
        self._isin_name_cache: Dict[str, str] = {}   # ISIN → human-readable name
        self._ticker_overrides: Dict[str, str] = {}   # populated lazily per resolve
        self._isin_ticker_cache: Dict[str, str] = self._build_isin_ticker_cache()
        self._cli_lock = asyncio.Lock()              # sc binary is not concurrency-safe
        self._fundamentals      = self._init_fundamentals()

    # ── Capabilities ─────────────────────────────────────────────────────────

    native_currency: str = "EUR"

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            stock_trading=True,
            etf_trading=True,
            knock_out_trading=True,
            fractional_shares=False,
            bracket_orders=False,
            short_selling=False,
            real_time_quotes=False,       # polling only
            historical_data=True,
        )

    @property
    def supports_fractional_shares(self) -> bool:
        return False

    # ── CLI runner ────────────────────────────────────────────────────────────

    async def _cli_result(self, args: List[str], *, quiet: bool = False) -> Optional[dict | list]:
        """Run CLI and unwrap the standard {ok, data: {result}} envelope."""
        raw = await self._run_cli(args, quiet=quiet)
        if raw is None:
            return None
        if isinstance(raw, dict):
            data = raw.get("data", raw)
            if isinstance(data, dict) and "result" in data:
                return data["result"]
            return data
        return raw

    def _load_ticker_overrides(self) -> Dict[str, str]:
        """Load broker-specific ticker→ISIN overrides from data/ticker_isin.json."""
        path = Path(__file__).resolve().parents[3] / "data" / "ticker_isin.json"
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
            section = data.get(self.broker_id, {})
            # skip _isin_override — that key is for ISIN→ticker resolution, not execution
            return {k.upper(): v for k, v in section.items() if not k.startswith("_")}
        except Exception:
            return {}

    def _build_isin_ticker_cache(self) -> Dict[str, str]:
        """Build ISIN→ticker reverse map from data/ticker_isin.json."""
        path = Path(__file__).resolve().parents[3] / "data" / "ticker_isin.json"
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
            section = data.get(self.broker_id, {})
            return {isin.upper(): ticker.upper()
                    for ticker, isin in section.items()
                    if not ticker.startswith("_")}
        except Exception:
            return {}

    @staticmethod
    def _init_fundamentals():
        try:
            from services.fundamentals_service import FundamentalsService
            return FundamentalsService()
        except Exception:
            return None

    async def _resolve_isin(self, symbol: str) -> str:
        """
        Resolve ticker → ISIN via (in order):
          1. Already an ISIN — return as-is
          2. ticker_isin.json override
          3. Session cache
          4. Holdings (symbols already owned)
          5. FMP company name → holdings name match (preferred over search for held stocks;
             avoids Scalable search returning a different exchange listing)
          6. Scalable broker search (works for new buys / names like "NVIDIA")
          7. FMP ISIN fallback (last resort — may return a different exchange listing)
        """
        key = symbol.upper()
        if _ISIN_RE.match(key):
            self._isin_cache[key] = key   # so name-lookup chain can find it
            return key
        # ticker_isin.json overrides take priority over session cache (file edits during session)
        self._ticker_overrides = self._load_ticker_overrides()
        if key in self._ticker_overrides:
            isin = self._ticker_overrides[key]
            self._isin_cache[key] = isin
            logger.debug("[ScalableBroker] Resolved %s → %s via ticker_isin.json", symbol, isin)
            return isin
        if key in self._isin_cache:
            return self._isin_cache[key]
        # Holdings
        holdings = await self._cli_result(["broker", "holdings"])
        if holdings:
            for item in holdings.get("items", []):
                name = item.get("name", "")
                isin = item.get("isin", "")
                if isin:
                    self._isin_cache[name.upper()] = isin
                    self._isin_cache[isin.upper()]  = isin
                    if name:
                        self._isin_name_cache[isin.upper()] = name
        if key in self._isin_cache:
            return self._isin_cache[key]
        # FMP company name → holdings name match (BEFORE Scalable search).
        # Handles cross-listed stocks where Scalable's search returns a different exchange
        # listing than the one actually held (e.g. UEC: FMP search might return US OTC ISIN
        # while Scalable holds the TSX listing; or NFLX EU vs US ISINs).
        # The holdings cache (step 4) already has "URANIUM ENERGY CO"→ISIN_scalable etc.
        # We ask FMP for the canonical company name first word, then match against that cache.
        if self._fundamentals is not None:
            try:
                profile = await self._fundamentals.get_profile(symbol)
                if profile and profile.company_name:
                    first_word = profile.company_name.split(",")[0].strip().split()[0].upper()
                    for k, v in self._isin_cache.items():
                        if k.startswith(first_word) and not _ISIN_RE.match(k):
                            self._isin_cache[key] = v
                            logger.info(
                                "[ScalableBroker] Resolved %s → %s via name match '%s'",
                                symbol, v, k,
                            )
                            return v
            except Exception:
                pass
        if key in self._isin_cache:
            return self._isin_cache[key]
        # Scalable search (company name or ticker)
        # NOTE: Scalable's search also matches on ISIN prefix, so e.g. "PLTR" returns Polish
        # stocks (ISINs PLTRFM00018, PLTRNSP00013) ahead of Palantir (US69608A1088) because
        # "PL" is Poland's country code and the ISINs sort alphabetically before "US...".
        # We score results: exact ticker field match > ISIN doesn't start with the query
        # (genuine name/company hit) > ISIN-prefix false positive.
        result = await self._cli_result(["broker", "search", symbol])
        if result:
            items = result.get("items", []) or []
            for item in items:
                isin = item.get("isin", "")
                name = item.get("name", "")
                if isin:
                    self._isin_cache[name.upper()] = isin
                    self._isin_cache[isin.upper()]  = isin
                    if name:
                        self._isin_name_cache[isin.upper()] = name

            if key not in self._isin_cache:
                def _search_score(item: dict) -> int:
                    ticker_field = (item.get("ticker") or item.get("symbol") or "").upper()
                    isin_val     = (item.get("isin")   or "").upper()
                    if ticker_field == key:
                        return 0          # exact ticker match — highest confidence
                    if isin_val and not isin_val.startswith(key):
                        return 1          # ISIN doesn't share prefix with query → real name hit
                    return 2              # ISIN-prefix false positive (e.g. PL... for "PLTR")

                best = min(items, key=_search_score, default=None)
                if best and best.get("isin"):
                    self._isin_cache[key] = best["isin"]
                    logger.debug(
                        "[ScalableBroker] Search picked '%s' (score=%d) for %s",
                        best.get("name", ""), _search_score(best), symbol,
                    )
        # FMP profile ISIN fallback (last resort — may return a different exchange listing)
        if self._fundamentals is not None:
            isin = await self._fundamentals.get_isin(symbol)
            if isin:
                self._isin_cache[key]         = isin
                self._isin_cache[isin.upper()] = isin
                logger.info("[ScalableBroker] Resolved %s → %s via FMP", symbol, isin)
                return isin
        raise RuntimeError(f"Cannot resolve ISIN for '{symbol}'")


    async def _run_cli(self, args: List[str], *, quiet: bool = False) -> Optional[dict | list]:
        """
        Run a sc CLI command and return parsed JSON output.
        Returns None if the command fails or produces no output.
        quiet=True suppresses WARNING logs for non-zero exit codes (use in polling loops).
        """
        cmd = [self.config.cli_path] + args + ["--json"]
        logger.debug("[%s] CLI: %s", self.broker_id, " ".join(cmd))
        # sc binary shares session state — serialize all calls to avoid rc=10 conflicts
        async with self._cli_lock:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=30.0
                )
                raw = stdout.decode().strip()

                # sc puts error JSON in stdout regardless of exit code —
                # parse first, then decide what to do.
                if not raw:
                    err = stderr.decode().strip() or f"no output (rc={proc.returncode})"
                    logger.warning("[%s] CLI empty response: %s", self.broker_id, err)
                    return None

                parsed = json.loads(raw)   # raises json.JSONDecodeError if not JSON

                if isinstance(parsed, dict) and not parsed.get("ok", True):
                    err_obj = parsed.get("error", {})
                    code    = err_obj.get("code", "")    if isinstance(err_obj, dict) else ""
                    msg     = err_obj.get("message", raw) if isinstance(err_obj, dict) else str(err_obj)
                    hints   = parsed.get("hints", [])
                    full    = f"{msg} — {hints[0]}" if hints else msg

                    if code == "no_session":
                        raise RuntimeError(
                            f"[{self.broker_id}] Not authenticated — run 'sc login' to start a session"
                        )
                    if quiet:
                        logger.debug("[%s] CLI error (rc=%d): %s", self.broker_id, proc.returncode, full)
                    else:
                        logger.warning("[%s] CLI error (rc=%d): %s", self.broker_id, proc.returncode, full)
                    return None

                return parsed
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"[{self.broker_id}] CLI timeout after 30s: {' '.join(cmd)}"
                )
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"[{self.broker_id}] CLI returned non-JSON for '{' '.join(cmd)}': {e} | raw={raw!r}"
                )
            except FileNotFoundError:
                raise RuntimeError(
                    f"[{self.broker_id}] sc binary not found at '{self.config.cli_path}' — run: sc login"
                )

    # ── Connection ────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        """Verify sc CLI is installed and authenticated."""
        try:
            data = await self._cli_result(["broker", "overview"])
            if data is None:
                raise RuntimeError("sc broker overview returned no data — check sc CLI is installed and run 'sc login'")

            # Seed the account cache so the immediately-following get_account_info()
            # call in the CLI startup does not fire a second subprocess round-trip.
            self._account_cache    = self._parse_account_info(data)
            self._account_cache_ts = time.time()

            self._connected = True
            logger.info(
                "[%s] Scalable Capital broker ready (readonly=%s)",
                self.broker_id, self.config.readonly,
            )
            await self._emit(BrokerEvent.CONNECTED)
            return True

        except Exception as e:
            logger.error("[%s] Connection failed: %s", self.broker_id, e)
            await self._emit(BrokerEvent.CONNECTION_LOST, error=str(e))
            return False

    async def disconnect(self) -> bool:
        """Cancel all polling tasks and disconnect."""
        for task in list(self._order_poll_tasks.values()):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._order_poll_tasks.clear()
        await self._cancel_background_tasks()

        self._connected = False
        logger.info("[%s] Disconnected", self.broker_id)
        await self._emit(BrokerEvent.DISCONNECTED)
        return True

    # ── Account ───────────────────────────────────────────────────────────────

    def _parse_account_info(self, data: dict) -> AccountInfo:
        valuation  = data.get("valuation", {})
        total      = float(valuation.get("total", 0))
        securities = float(valuation.get("securities", 0))
        cash       = total - securities
        return AccountInfo(
            account_id=str(data.get("account_id", "")),
            account_name="Scalable Capital",
            status="active",
            account_type="broker",
            current_value=total,
            cash_in_hand=cash,
            margin_used=0.0,
            margin_available=cash,
            leverage=1.0,
            currency="EUR",
            broker_specific_data=data,
        )

    async def get_account_info(self) -> AccountInfo:
        self._require_connected()
        now = time.time()
        if self._account_cache and (now - self._account_cache_ts) < self.config.account_cache_ttl_seconds:
            return self._account_cache

        data = await self._cli_result(["broker", "overview"])
        if not data:
            raise RuntimeError("sc broker overview returned no data")

        info = self._parse_account_info(data)
        self._account_cache    = info
        self._account_cache_ts = now

        await self._emit(BrokerEvent.ACCOUNT_UPDATE, data=info)
        await self.check_risk_limits(info)
        return info

    # ── Positions ─────────────────────────────────────────────────────────────

    async def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        self._require_connected()
        now = time.time()

        if (
            self._position_cache is not None
            and (now - self._position_cache_ts) < self.config.position_cache_ttl_seconds
        ):
            positions = self._position_cache
        else:
            data = await self._cli_result(["broker", "holdings"])
            if data is None:
                return []
            raw = data.get("items", [])
            positions = [self._map_position(p) for p in raw]
            self._position_cache    = positions
            self._position_cache_ts = now

        if symbol:
            positions = [p for p in positions if p.symbol == symbol]
        return positions

    async def get_position(self, symbol: str) -> Optional[Position]:
        positions = await self.get_positions(symbol)
        return positions[0] if positions else None

    async def get_position_by_id(self, position_id: str) -> Optional[Position]:
        all_pos = await self.get_positions()
        return next((p for p in all_pos if p.id == position_id), None)

    async def close_position(self, position_id: str, quantity: Optional[float] = None) -> bool:
        self._require_connected()
        position = await self.get_position_by_id(position_id)
        if not position:
            logger.warning("[%s] Position %s not found", self.broker_id, position_id)
            return False
        qty = quantity or position.quantity
        await self.place_order(position.symbol, qty, OrderSide.SELL, OrderType.MARKET)
        return True

    # ── Orders ────────────────────────────────────────────────────────────────

    # ── Order helpers ─────────────────────────────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        quantity: float,
        side: OrderSide = OrderSide.BUY,
        order_type: OrderType = OrderType.MARKET,
        price: Optional[float] = None,
        *,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        time_in_force: str = "GTC",
        notes: Optional[str] = None,
    ) -> Order:
        self._require_connected()

        if self.config.readonly:
            raise RuntimeError(
                f"[{self.broker_id}] readonly=True — order placement blocked. "
                f"Set ScalableBrokerConfig(readonly=False) to enable live trading."
            )

        action = "buy" if side == OrderSide.BUY else "sell"
        isin   = await self._resolve_isin(symbol)

        if side == OrderSide.SELL:
            all_pos   = await self.get_positions()
            isin_up   = isin.upper()
            inst_name = self._isin_name_cache.get(isin_up, "").upper()
            name_word = inst_name.split()[0] if inst_name else ""
            if not any(
                (p.id or "").upper() == isin_up
                or (name_word and (p.symbol or "").upper().startswith(name_word))
                for p in all_pos
            ):
                raise RuntimeError(
                    f"[{self.broker_id}] Cannot short-sell {symbol} — "
                    f"Scalable Capital is long-only. No existing position to close."
                )

        # BUY uses --amount (EUR); SELL uses --shares (qty)
        quote_data   = await self._cli_result(["broker", "quote", "--isin", isin])
        q            = quote_data or {}
        mid_price    = float(q.get("quote_mid_price") or price or 0)
        sizing_price = float(
            q.get("quote_ask_price" if side == OrderSide.BUY else "quote_bid_price") or mid_price
        )
        if not sizing_price:
            raise RuntimeError(f"Cannot get quote for {symbol} (ISIN {isin})")

        # Scalable EIX: --amount buys always produce a GTC limit at a backend-derived
        # price (~64% below market) regardless of --order-type. Work around by computing
        # the limit at ask + 10% (fills immediately) and setting amount = qty × limit so
        # that Scalable's validation (amount / limit_price == shares) always holds and
        # doesn't raise CONFIRMATION_FIELDS_MISMATCH.
        _market_buy  = action == "buy" and order_type == OrderType.MARKET
        _limit_price = round(sizing_price * 1.10, 4) if _market_buy else (price if order_type == OrderType.LIMIT else None)
        amount       = round(quantity * (_limit_price if _market_buy else sizing_price), 2)

        base_args = _trade_args(
            action, isin, quantity, amount,
            OrderType.LIMIT if _market_buy else order_type,
            stop_price=price if order_type == OrderType.STOP else None,
            limit_price=_limit_price,
        )
        logger.info("[%s] %s %s isin=%s qty=%.2f args=%s",
                    self.broker_id, action.upper(), symbol, isin, quantity, base_args)

        phase1_raw = await self._run_cli(base_args)
        if not phase1_raw:
            raise RuntimeError(f"sc broker trade {action} phase-1 returned no output — check CLI auth")

        p1d        = phase1_raw.get("data", {}) if isinstance(phase1_raw, dict) else {}
        confirm_id = (p1d.get("confirmation") or {}).get("id", "")
        if not confirm_id:
            raise RuntimeError(f"No confirmation id in phase-1 response: {phase1_raw}")

        preview = _parse_phase1(p1d)
        preview.update({"_isin": isin, "_action": action, "_base_args": base_args})
        return await self._submit_order(symbol, quantity, side, order_type, price, confirm_id, preview)

    async def _refresh_confirm_id(self, base_args: list, preview: dict) -> str:
        """Re-run phase-1 to obtain a fresh confirmation ID after expiry."""
        phase1_raw = await self._run_cli(base_args)
        if not phase1_raw:
            raise RuntimeError(
                "Could not refresh order confirmation — check sc CLI auth ('sc login')"
            )
        p1d        = phase1_raw.get("data", {}) if isinstance(phase1_raw, dict) else {}
        confirm_id = (p1d.get("confirmation") or {}).get("id", "")
        if not confirm_id:
            raise RuntimeError(f"No confirmation id in refresh response: {phase1_raw}")
        conf = p1d.get("confirmation", {}) or {}
        preview["expires_at"] = conf.get("expires_at_epoch", 0)
        logger.info(
            "[%s] Refreshed confirmation id (expires epoch=%.0f)",
            self.broker_id, preview["expires_at"],
        )
        return confirm_id

    async def _submit_order(
        self,
        symbol:     str,
        quantity:   float,
        side:       OrderSide,
        order_type: OrderType,
        price:      Optional[float],
        confirm_id: str,
        preview:    dict,
    ) -> Order:
        """Phase 2: execute after caller has shown pre-trade summary and confirmed."""
        base_args  = preview.get("_base_args", [])
        expires_at = preview.get("expires_at", 0)

        # Pre-flight: refresh if we already know the confirmation has expired.
        if expires_at and time.time() > expires_at:
            logger.info("[%s] Confirmation expired before phase-2 — refreshing", self.broker_id)
            confirm_id = await self._refresh_confirm_id(base_args, preview)

        data = await self._run_cli(base_args + ["--confirm", confirm_id])

        # rc=10 with no output is Scalable's "confirmation expired" signal — retry once.
        if not data:
            logger.info("[%s] Phase-2 rc=10 — refreshing confirmation and retrying", self.broker_id)
            confirm_id = await self._refresh_confirm_id(base_args, preview)
            data = await self._run_cli(base_args + ["--confirm", confirm_id])
            if not data:
                raise RuntimeError(
                    "sc broker trade phase-2 failed after confirmation refresh — "
                    "check sc CLI authentication ('sc login')"
                )

        result   = (data.get("data") or {}).get("result") or data
        order_id = str(
            (result.get("order_submission") or {}).get("order_id")
            or result.get("id") or result.get("order_id") or result.get("orderId") or ""
        )
        if not order_id:
            raise RuntimeError(f"No order id in phase-2 response: {data}")

        order = Order(
            id=order_id,
            symbol=symbol,
            order_type=order_type,
            side=side,
            quantity=quantity,
            price=price or 0.0,
            status=OrderStatus.SUBMITTED,
            placed_timestamp=dt.datetime.now(dt.timezone.utc),
            filled_timestamp=None,
            cancelled_timestamp=None,
            average_fill_price=0.0,
            fees=0.0,
            leverage=1.0,
            broker_order_id=order_id,
            broker_specific_data=data,
        )

        await self._emit(BrokerEvent.ORDER_SUBMITTED, data=order)
        self._position_cache = None

        if order_type == OrderType.MARKET:
            # Extract ISIN from phase-1 args so the poller can scan transactions by ISIN.
            isin = ""
            if "--isin" in base_args:
                idx = base_args.index("--isin")
                if idx + 1 < len(base_args):
                    isin = base_args[idx + 1]
            return await self._poll_order_status_sync(order_id, order, isin=isin)
        else:
            # Limit / stop orders may take hours — poll in background.
            task = asyncio.create_task(self._poll_order_status(order_id, order))
            self._order_poll_tasks[order_id] = task
            return order

    async def preview_order(
        self,
        symbol:     str,
        quantity:   float,
        side:       OrderSide = OrderSide.BUY,
        order_type: OrderType = OrderType.MARKET,
        price:      Optional[float] = None,
    ) -> tuple:
        """
        Phase 1 only — returns (preview_dict, confirm_id).
        Call submit_order() after showing the summary and getting user confirmation.
        """
        self._require_connected()
        if self.config.readonly:
            raise RuntimeError(f"[{self.broker_id}] readonly=True — trading blocked.")
        action       = "buy" if side == OrderSide.BUY else "sell"
        isin         = await self._resolve_isin(symbol)
        if side == OrderSide.SELL:
            all_pos   = await self.get_positions()
            isin_up   = isin.upper()
            inst_name = self._isin_name_cache.get(isin_up, "").upper()
            name_word = inst_name.split()[0] if inst_name else ""
            if not any(
                (p.id or "").upper() == isin_up
                or (name_word and (p.symbol or "").upper().startswith(name_word))
                for p in all_pos
            ):
                raise RuntimeError(f"Cannot short-sell {symbol} — no existing position.")
        quote_data   = await self._cli_result(["broker", "quote", "--isin", isin])
        q            = quote_data or {}
        mid_price    = float(q.get("quote_mid_price") or price or 0)
        sizing_price = float(
            q.get("quote_ask_price" if side == OrderSide.BUY else "quote_bid_price") or mid_price
        )
        if not sizing_price:
            raise RuntimeError(f"Cannot get quote for {symbol}")

        # Scalable EIX: --amount buys always produce a GTC limit at a backend-derived
        # price (~64% below market) regardless of --order-type. Work around by computing
        # the limit at ask + 10% (fills immediately) and setting amount = qty × limit so
        # that Scalable's validation (amount / limit_price == shares) always holds and
        # doesn't raise CONFIRMATION_FIELDS_MISMATCH.
        _market_buy     = action == "buy" and order_type == OrderType.MARKET
        stop_price_eur  = price if order_type == OrderType.STOP  else None
        limit_price_eur = (round(sizing_price * 1.10, 4) if _market_buy else
                           (price if order_type == OrderType.LIMIT else None))
        amount          = round(quantity * (limit_price_eur if _market_buy else sizing_price), 2)
        base_args  = _trade_args(
            action, isin, quantity, amount,
            OrderType.LIMIT if _market_buy else order_type,
            stop_price=stop_price_eur, limit_price=limit_price_eur,
        )
        phase1_raw = await self._run_cli(base_args)
        if not phase1_raw:
            raise RuntimeError("Phase-1 returned no output")

        p1d        = phase1_raw.get("data", {}) if isinstance(phase1_raw, dict) else {}
        confirm_id = (p1d.get("confirmation") or {}).get("id", "")
        if not confirm_id:
            raise RuntimeError(f"No confirmation id: {phase1_raw}")

        preview = _parse_phase1(p1d)
        preview["_isin"]            = isin
        preview["_action"]          = action
        preview["_base_args"]       = base_args
        preview["_instrument_name"] = (
            self._isin_name_cache.get(isin.upper(), "")
            or q.get("name", "")
        )
        return preview, confirm_id

    async def submit_order(
        self,
        symbol:     str,
        quantity:   float,
        side:       OrderSide,
        confirm_id: str,
        preview:    dict,
        order_type: OrderType = OrderType.MARKET,
        price:      Optional[float] = None,
    ) -> Order:
        """Phase 2 — call after user has confirmed the preview."""
        self._require_connected()
        return await self._submit_order(symbol, quantity, side, order_type, price, confirm_id, preview)

    async def _poll_order_status_sync(
        self, order_id: str, submitted_order: Order, *, isin: str = ""
    ) -> Order:
        """
        Synchronous fill-wait for market orders.

        Polls `broker transactions` (the recent-transactions list) and matches by
        ISIN + side + timestamp, which is reliable because Scalable's transaction
        list uses a different ID scheme (SCALxxx) from the order-submission ID.
        Falls back to `broker transaction details --transaction-id` in case the
        list is empty or ISIN is unknown.

        Returns a filled Order so callers get price/qty directly.
        """
        symbol       = submitted_order.symbol
        side_str     = "BUY" if submitted_order.side == OrderSide.BUY else "SELL"
        submitted_at = submitted_order.placed_timestamp
        t0           = time.time()
        deadline     = t0 + self.config.poll_timeout_seconds
        attempts     = 0

        while time.time() < deadline:
            await asyncio.sleep(self.config.poll_interval_seconds)
            attempts += 1
            elapsed = int(time.time() - t0)
            print(f"\r⏳ Waiting for fill… {elapsed}s", end="", flush=True)

            # Primary: scan the transactions list for a matching ISIN entry that
            # appeared after we submitted.  This works even when the per-transaction
            # details endpoint returns rc=30 (it uses a different ID namespace).
            txn_data = await self._cli_result(["broker", "transactions"], quiet=True)
            if txn_data:
                items = txn_data.get("items", []) if isinstance(txn_data, dict) else []
                for item in items:
                    item_isin = str(item.get("isin", "")).upper()
                    if isin and item_isin != isin.upper():
                        continue
                    # Filter to transactions at or after our submission time.
                    ts_raw = item.get("last_event_datetime") or item.get("created_at") or ""
                    try:
                        ts = dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) if ts_raw else None
                    except ValueError:
                        ts = None
                    if ts and ts < submitted_at:
                        continue  # older transaction for same ISIN, skip
                    raw_status = str(item.get("status", "")).upper()
                    item_side  = str(item.get("side", item.get("type", ""))).upper()
                    if raw_status in ("SETTLED", "FILLED") and side_str in item_side:
                        print()
                        qty        = float(item.get("quantity") or submitted_order.quantity)
                        amount     = float(item.get("amount") or 0)
                        fill_price = abs(amount) / qty if qty else 0.0
                        filled = Order(
                            id=item_isin or order_id,
                            symbol=symbol,
                            order_type=submitted_order.order_type,
                            side=submitted_order.side,
                            quantity=qty,
                            price=fill_price,
                            status=OrderStatus.FILLED,
                            placed_timestamp=submitted_order.placed_timestamp,
                            filled_timestamp=ts or dt.datetime.now(dt.timezone.utc),
                            cancelled_timestamp=None,
                            average_fill_price=fill_price,
                            fees=0.0,
                            leverage=1.0,
                            broker_order_id=order_id,
                            broker_specific_data=item,
                        )
                        self._position_cache = None
                        await self._emit(BrokerEvent.ORDER_FILLED, data=filled)
                        await self._emit(BrokerEvent.POSITION_OPENED, data=filled)
                        logger.info("[%s] %s FILLED @ %.4f (%.0f shares, via transactions list)", self.broker_id, order_id, fill_price, qty)
                        return filled
                    if raw_status in ("CANCELLED", "CANCEL_REQUESTED", "REJECTED", "EXPIRED") and side_str in item_side:
                        print()
                        raise RuntimeError(f"Order {order_id} {raw_status}")

            logger.debug("[%s] Poll %d — %s not settled yet", self.broker_id, attempts, order_id)

        print()
        raise RuntimeError(
            f"Order {order_id} timed out after {self.config.poll_timeout_seconds:.0f}s — "
            "check /orders in the app"
        )

    async def _poll_order_status(self, order_id: str, submitted_order: Order) -> None:
        """
        Background task: polls until a terminal state is reached.

        Strategy: orders live in `broker orders` while pending and move to
        `broker transaction details` once settled/filled.  We check both:
          1. transaction details → filled/cancelled/rejected → done
          2. transaction details empty → check broker orders list:
               - found (still pending) → keep waiting, log DEBUG not WARNING
               - not found → transaction details may lag; keep waiting quietly
        """
        symbol   = submitted_order.symbol
        attempts = 0
        deadline = time.time() + self.config.poll_timeout_seconds

        while time.time() < deadline and attempts < self.config.poll_max_attempts:
            await asyncio.sleep(self.config.poll_interval_seconds)
            attempts += 1

            # quiet=True: rc=30 ("not found yet") is expected while order is pending
            data = await self._cli_result(
                ["broker", "transaction", "details", "--transaction-id", order_id],
                quiet=True,
            )
            if not data:
                # Transaction not visible yet — order is likely still pending.
                logger.debug("[%s] Poll %d — order %s not settled yet", self.broker_id, attempts, order_id)
                continue

            raw_status = str(data.get("status", "")).upper()
            logger.debug("[%s] Order %s status=%s (attempt %d)", self.broker_id, order_id, raw_status, attempts)

            if raw_status in ("SETTLED", "FILLED"):
                qty        = float(data.get("quantity", submitted_order.quantity))
                amount     = float(data.get("amount", 0))
                fill_price = (amount / qty) if qty else 0.0
                isin       = data.get("isin", "")
                position_id = isin if isin else order_id

                filled = Order(
                    id=position_id,
                    symbol=symbol,
                    order_type=submitted_order.order_type,
                    side=submitted_order.side,
                    quantity=submitted_order.quantity,
                    price=fill_price,
                    status=OrderStatus.FILLED,
                    placed_timestamp=submitted_order.placed_timestamp,
                    filled_timestamp=dt.datetime.now(dt.timezone.utc),
                    cancelled_timestamp=None,
                    average_fill_price=fill_price,
                    fees=0.0,
                    leverage=1.0,
                    broker_order_id=order_id,
                    broker_specific_data=data,
                )
                self._position_cache = None
                await self._emit(BrokerEvent.ORDER_FILLED, data=filled)
                await self._emit(BrokerEvent.POSITION_OPENED, data=filled)
                logger.info("[%s] Order %s FILLED @ %.4f", self.broker_id, order_id, fill_price)
                break

            elif raw_status in ("CANCELLED", "CANCEL_REQUESTED", "REJECTED", "EXPIRED"):
                rejected = Order(
                    id=order_id,
                    symbol=symbol,
                    order_type=submitted_order.order_type,
                    side=submitted_order.side,
                    quantity=submitted_order.quantity,
                    price=submitted_order.price,
                    status=OrderStatus.REJECTED,
                    placed_timestamp=submitted_order.placed_timestamp,
                    filled_timestamp=None,
                    cancelled_timestamp=dt.datetime.now(dt.timezone.utc),
                    average_fill_price=0.0,
                    fees=0.0,
                    leverage=1.0,
                    broker_order_id=order_id,
                    reject_reason=data.get("reason", raw_status),
                )
                await self._emit(BrokerEvent.ORDER_REJECTED, data=rejected)
                logger.warning("[%s] Order %s %s: %s", self.broker_id, order_id, raw_status, data.get("reason", ""))
                break
        else:
            logger.error("[%s] Order %s timed out after %.0fs", self.broker_id, order_id, self.config.poll_timeout_seconds)
            timeout_order = Order(
                id=order_id, symbol=symbol,
                order_type=submitted_order.order_type, side=submitted_order.side,
                quantity=submitted_order.quantity, price=submitted_order.price,
                status=OrderStatus.REJECTED,
                placed_timestamp=submitted_order.placed_timestamp,
                filled_timestamp=None, cancelled_timestamp=dt.datetime.now(dt.timezone.utc),
                average_fill_price=0.0, fees=0.0, leverage=1.0,
                reject_reason="POLL_TIMEOUT",
            )
            await self._emit(BrokerEvent.ORDER_REJECTED, data=timeout_order)

        self._order_poll_tasks.pop(order_id, None)

    async def cancel_order(self, order_id: str) -> bool:
        self._require_connected()
        data = await self._run_cli(["broker", "trade", "cancel", "--order-id", order_id])
        if data is None:
            logger.warning("[%s] Cancel %s — CLI returned no output", self.broker_id, order_id)
            return False

        task = self._order_poll_tasks.pop(order_id, None)
        if task:
            task.cancel()

        await self._emit(BrokerEvent.ORDER_CANCELLED, data={"order_id": order_id})
        logger.info("[%s] Order %s cancelled", self.broker_id, order_id)
        return True

    async def get_order(self, order_id: str) -> Optional[Order]:
        self._require_connected()
        data = await self._cli_result(["broker", "transaction", "details", "--transaction-id", order_id])
        return self._map_order_data(data) if data else None

    async def get_orders(
        self,
        symbol: Optional[str] = None,
        status: Optional[OrderStatus] = None,
    ) -> List[Order]:
        self._require_connected()

        cmd = [
            "broker", "transactions",
            "--page-size", "100",
            "--type-filter", "BUY",
            "--type-filter", "SELL",
        ]
        if symbol:
            isin = await self._resolve_isin(symbol)
            cmd += ["--isin", isin]

        data = await self._cli_result(cmd)
        if not data:
            return []

        raw    = data.get("items", [])
        orders = [self._map_order_data(o) for o in raw if o]
        if status:
            orders = [o for o in orders if o.status == status]
        return orders

    async def get_pending_orders(self) -> List[Order]:
        """
        Return resting orders (stop / limit) that have not yet filled.
        Uses sc broker transactions filtered to pre-fill statuses.
        """
        self._require_connected()
        data = await self._cli_result([
            "broker", "transactions",
            "--status", "CREATED",
            "--status", "PENDING",
            "--status", "PARTIAL_FILLED",
            "--type-filter", "BUY",
            "--type-filter", "SELL",
            "--page-size", "100",
        ])
        if not data:
            return []
        raw = data.get("items", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        return [self._map_pending_order(o) for o in raw if o]

    def _map_pending_order(self, data: dict) -> Order:
        """Map a transactions item (pre-fill status) to an Order entity."""
        order = self._map_order_data(data)
        # Preserve limit/stop price if set (map_order_data derives price from amount/qty)
        limit_price = float(data.get("limit_price") or 0)
        stop_price  = float(data.get("stop_price")  or 0)
        if stop_price:
            order.order_type = OrderType.STOP
            order.price      = stop_price
        elif limit_price:
            order.order_type = OrderType.LIMIT
            order.price      = limit_price
        # Resolve ISIN → ticker so symbol comparisons work
        isin = str(data.get("isin") or "").upper()
        if isin and isin in self._isin_ticker_cache:
            order.symbol = self._isin_ticker_cache[isin]
        return order

    # ── Quotes ────────────────────────────────────────────────────────────────

    async def get_quote(self, symbol: str) -> Quote:
        self._require_connected()
        isin = await self._resolve_isin(symbol)
        data = await self._cli_result(["broker", "quote", "--isin", isin])
        if not data:
            raise RuntimeError(f"No quote data for {symbol} (ISIN {isin})")
        name = data.get("name", "")
        if name and isin:
            self._isin_name_cache[isin.upper()] = name
        return self._map_quote(symbol, data)

    async def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        results = {}
        for symbol in symbols:
            try:
                results[symbol] = await self.get_quote(symbol)
            except Exception:
                pass
        return results

    # ── Mapping helpers ───────────────────────────────────────────────────────

    def _map_position(self, data: dict) -> Position:
        # holdings item: {isin, name, quantity, fifo_price, quote_mid_price, valuation}
        isin      = data.get("isin") or ""
        name      = data.get("name") or isin
        quantity  = float(data.get("quantity")  or 0)
        avg_price = float(data.get("fifo_price") or 0)
        cur_price = float(data.get("quote_mid_price") or avg_price or 0)
        value     = float(data.get("valuation") or (quantity * cur_price))
        cost      = quantity * avg_price
        upl       = value - cost
        upl_pct   = (upl / cost * 100) if cost else 0.0

        # Populate ISIN cache while we have the data
        if isin and name:
            self._isin_cache[name.upper()] = isin
            self._isin_cache[isin.upper()]  = isin
            self._isin_name_cache[isin.upper()] = name

        # Prefer ticker from ticker_isin.json over the human-readable name
        symbol = self._isin_ticker_cache.get(isin.upper(), name)

        return Position(
            id=isin, symbol=symbol, side=TradeSide.LONG,
            open_date=dt.datetime.now(dt.timezone.utc), close_date=None,
            quantity=quantity, average_price=avg_price, leverage=1.0,
            market_value=value,
            unrealized_pnl=upl, unrealized_pnl_percentage=upl_pct,
            realized_pnl=0.0, realized_pnl_percentage=0.0,
            stop_loss_price=0.0, take_profit_price=0.0,
        )

    def _map_order_data(self, data: dict) -> Order:
        # transactions item: {id, description, isin, side, quantity, amount, status, last_event_datetime}
        order_id   = str(data.get("id", ""))
        symbol     = str(data.get("description", data.get("isin", "")))
        raw_side   = str(data.get("side", data.get("type", "BUY"))).upper()
        side       = OrderSide.BUY if "BUY" in raw_side else OrderSide.SELL
        quantity   = float(data.get("quantity", 0))
        amount     = float(data.get("amount", 0))
        price      = abs(amount / quantity) if quantity else 0.0
        raw_status = str(data.get("status", "")).upper()

        status_map = {
            "SETTLED":        OrderStatus.FILLED,
            "FILLED":         OrderStatus.FILLED,
            "PARTIAL_FILLED": OrderStatus.SUBMITTED,
            "PENDING":        OrderStatus.SUBMITTED,
            "REQUESTED":      OrderStatus.SUBMITTED,
            "CREATED":        OrderStatus.SUBMITTED,
            "CANCELLED":      OrderStatus.CANCELLED,
            "CANCEL_REQUESTED": OrderStatus.CANCELLED,
            "REJECTED":       OrderStatus.REJECTED,
            "EXPIRED":        OrderStatus.REJECTED,
        }
        status = status_map.get(raw_status, OrderStatus.SUBMITTED)

        ts_str = data.get("last_event_datetime", "")
        try:
            ts = dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if ts_str else dt.datetime.now(dt.timezone.utc)
        except ValueError:
            ts = dt.datetime.now(dt.timezone.utc)

        return Order(
            id=order_id, symbol=symbol,
            order_type=OrderType.MARKET, side=side,
            quantity=quantity, price=price, status=status,
            placed_timestamp=ts,
            filled_timestamp=ts if status == OrderStatus.FILLED else None,
            cancelled_timestamp=ts if status == OrderStatus.CANCELLED else None,
            average_fill_price=price if status == OrderStatus.FILLED else 0.0,
            fees=0.0, leverage=1.0, broker_order_id=order_id, broker_specific_data=data,
        )

    def _map_quote(self, symbol: str, data: dict) -> Quote:
        # broker quote result: {quote_mid_price, quote_bid_price, quote_ask_price, quote_currency}
        mid_val = data.get("quote_mid_price") or data.get("price") or 0
        bid_val = data.get("quote_bid_price") or mid_val
        ask_val = data.get("quote_ask_price") or mid_val
        if not mid_val and not bid_val and not ask_val:
            raise RuntimeError(
                f"No price data in quote response for {symbol} — "
                f"instrument may not be tradable or ISIN may be wrong (check ticker_isin.json)"
            )
        mid  = Decimal(str(mid_val))
        bid  = Decimal(str(bid_val))
        ask  = Decimal(str(ask_val))
        name = data.get("name", symbol)
        return Quote(
            symbol=name,
            instrument_type=InstrumentType.STOCK,
            bid=bid, ask=ask, last=mid,
            bid_size=0, ask_size=0, volume=0,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )

    # ── Internal guard ────────────────────────────────────────────────────────

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError(f"[{self.broker_id}] Not connected — call connect() first")


# ── Cross-broker symbol search ────────────────────────────────────────────────

async def search_symbol(broker: "ScalableBroker", sym: str):
    """Returns (found, isin, name, bid, ask, flags) for cross-broker /search."""
    try:
        isin = await broker._resolve_isin(sym)
        data = await broker._cli_result(["broker", "quote", "--isin", isin])
        d    = data or {}
        name = d.get("name", "") or broker._isin_name_cache.get(isin.upper(), "")
        bid  = float(d.get("quote_bid_price") or d.get("bid_price") or d.get("bid") or 0)
        ask  = float(d.get("quote_ask_price") or d.get("ask_price") or d.get("ask") or 0)
        mid  = float(d.get("quote_mid_price") or d.get("mid_price") or d.get("mid") or 0)
        if not bid and mid:
            bid = ask = mid
        return True, isin, name, bid, ask, ["long only", "no leverage"]
    except Exception as e:
        logger.warning("[ScalableBroker] search_symbol(%s) failed: %s", sym, e)
        return False, "—", "not tradeable", 0.0, 0.0, ["not tradeable"]

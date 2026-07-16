"""
services.command_service

Unified command backend shared by CLI, Telegram, and (future) API interfaces.

Each interface is responsible only for:
  - parsing user input → typed args
  - calling CommandService methods
  - formatting the returned structured data for its output channel

Raises CommandError for user-facing validation / not-found errors.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ── User-facing error ─────────────────────────────────────────────────────────

class CommandError(Exception):
    """Raised for user-facing errors. Show the message directly, no traceback."""


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class IndicatorResult:
    symbol:     str
    tf:         str
    ts:         str
    close:      float
    atr:        Optional[float] = None
    atr_pct:    Optional[float] = None
    rsi:        Optional[float] = None
    ema8:       Optional[float] = None
    ema20:      Optional[float] = None
    st_value:   Optional[float] = None
    st_dir:     Optional[int]   = None
    st_flipped: bool            = False
    adx:        Optional[float] = None


@dataclass
class PortfolioIndicatorRow:
    ticker:     str
    pos_name:   str
    indicators: IndicatorResult


@dataclass
class PortfolioIndicatorResult:
    rows:    list[PortfolioIndicatorRow]  = field(default_factory=list)
    skipped: list[tuple[str, str]]        = field(default_factory=list)


@dataclass
class FillSummary:
    symbol:        str
    fills:         list          # list[Order]
    position_qty:  float
    broker_avg:    float
    current_price: Optional[float]
    buy_qty:       float
    avg_buy:       float
    sell_qty:      float
    avg_sell:      float
    realized:      float
    unrealized:    float


@dataclass
class PnlSummary:
    positions:        list   # list[Position]
    total_unrealized: float
    total_value:      float


@dataclass
class RiskMetrics:
    own_equity:      float
    drawdown:        float
    dd_pct:          float
    total_value:     float
    loan:            float
    equity_floor:    float
    hard_max_loss:   float
    starting_equity: float


@dataclass
class CloseResult:
    symbol:      str
    position_id: str
    success:     bool
    error:       str = ""


# ── Ignore-list helpers (for portfolio indicators) ────────────────────────────

_IGNORE_FILE = Path(__file__).resolve().parents[2] / "data" / "indp_ignore.json"


def _load_ignore() -> set[str]:
    try:
        if _IGNORE_FILE.exists():
            return {s.lower() for s in json.loads(_IGNORE_FILE.read_text())}
    except Exception:
        pass
    return set()


def _save_ignore(ig: set[str]) -> None:
    _IGNORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _IGNORE_FILE.write_text(json.dumps(sorted(ig), indent=2))


def _is_ignored(pos: Any, ignore: set[str], ticker: Optional[str] = None) -> bool:
    checks = []
    if pos.symbol:
        checks.append(pos.symbol.lower())
    if ticker:
        checks.append(ticker.lower())
    if pos.id:
        checks.append((pos.id or "").lower())
    return any(c in ignore for c in checks)


# ── Scanner loader ────────────────────────────────────────────────────────────

_SCANNERS_DIR = Path(__file__).resolve().parents[1] / "scripts" / "scanners"

_SCANNER_MAP: dict[str, tuple[str, dict]] = {
    "pm":        ("run_post_market_scanner",  {}),
    "pre":       ("run_pre_market_scanner",   {}),
    "vol":       ("run_daily_high_volumes",   {"min_relvol": 3.0, "mode_label": "FIXED"}),
    "spikes":    ("run_spikes_scanner",       {}),
    "parabolic": ("run_parabolic_scanner",    {}),
}


def _load_scanner(name: str):
    path = _SCANNERS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── CommandService ─────────────────────────────────────────────────────────────

class CommandService:
    """
    Unified command backend. All business logic lives here.

    Interfaces (CLI / Telegram / API) are responsible only for:
      - parsing user input → typed args
      - instantiating CommandService(broker)
      - calling methods and formatting the returned data
    """

    def __init__(self, broker: Any) -> None:
        self._broker = broker

    # ── Account reads ─────────────────────────────────────────────────────────

    async def account(self):
        """Return AccountInfo."""
        return await self._broker.get_account_info()

    async def positions(self):
        """Return list[Position]."""
        return await self._broker.get_positions()

    async def orders(self, symbol: Optional[str] = None):
        """Return list[Order]."""
        return await self._broker.get_orders(symbol=symbol)

    async def pnl(self) -> PnlSummary:
        """Return open P&L summary across all positions."""
        pos_list  = await self._broker.get_positions()
        total_upl = sum(p.unrealized_pnl or 0 for p in pos_list)
        total_val = sum(p.market_value   or 0 for p in pos_list)
        return PnlSummary(positions=pos_list, total_unrealized=total_upl, total_value=total_val)

    async def quote(self, symbol: str):
        """Return Quote. Raises CommandError if not available."""
        q = await self._broker.get_quote(symbol.upper())
        if not q:
            raise CommandError(f"No quote available for {symbol}")
        return q

    async def fills(self, symbol: str, n: Optional[int] = None) -> FillSummary:
        """Return fill history + P&L for symbol."""
        from services.pnl_service      import get_fills, calc_pnl
        from services.position_service import get_position_for_ticker

        fills_list = await get_fills(self._broker, symbol)
        if n is not None:
            fills_list = fills_list[-n:]

        pos     = await get_position_for_ticker(self._broker, symbol)
        pos_qty = float(pos.quantity)      if pos else 0.0
        pos_avg = float(pos.average_price or 0) if pos else 0.0

        current_price: Optional[float] = None
        try:
            q = await self._broker.get_quote(symbol.upper())
            if q:
                current_price = float(q.bid or q.mid or q.last or 0) or None
        except Exception:
            pass

        pnl = calc_pnl(fills_list, pos_qty, current_bid=current_price or 0.0)

        # Prefer broker's own avg over calc'd avg when position is open
        unrealized = pnl.get("unrealized", 0.0)
        if pos_qty > 0 and pos_avg > 0 and current_price:
            unrealized = (current_price - pos_avg) * pos_qty

        return FillSummary(
            symbol=symbol.upper(),
            fills=fills_list,
            position_qty=pos_qty,
            broker_avg=pos_avg,
            current_price=current_price,
            buy_qty=pnl.get("buy_qty",  0.0),
            avg_buy=pnl.get("avg_buy",  0.0),
            sell_qty=pnl.get("sell_qty", 0.0),
            avg_sell=pnl.get("avg_sell", 0.0),
            realized=pnl.get("realized", 0.0),
            unrealized=unrealized,
        )

    # ── Risk ──────────────────────────────────────────────────────────────────

    async def risk(self) -> RiskMetrics:
        """Return risk metrics vs configured limits."""
        acc = await self._broker.get_account_info()
        cfg = getattr(self._broker, "config", None)
        loan   = getattr(cfg, "loan_amount",     0)
        floor  = getattr(cfg, "equity_floor",    0)
        max_dd = getattr(cfg, "hard_max_loss",   0)
        start  = getattr(cfg, "starting_equity", 0)

        own_equity = acc.current_value - loan
        drawdown   = (start - acc.current_value) if start else 0.0
        dd_pct     = (drawdown / start * 100)    if start else 0.0

        return RiskMetrics(
            own_equity=own_equity,
            drawdown=drawdown,
            dd_pct=dd_pct,
            total_value=acc.current_value,
            loan=loan,
            equity_floor=floor,
            hard_max_loss=max_dd,
            starting_equity=start,
        )

    # ── Indicators ────────────────────────────────────────────────────────────

    async def indicators(
        self,
        symbol:   str,
        tf:       str  = "1d",
        extended: bool = False,
    ) -> IndicatorResult:
        """Return technical indicators for a symbol."""
        from services.fundamentals_service import FundamentalsService
        from services.indicators_service   import atr, rsi, ema, supertrend, adx

        svc = FundamentalsService(api_key=os.environ.get("FMP_API_KEY", ""))
        df  = await svc.get_ohlcv(symbol.upper(), timeframe=tf, limit=60, extended=extended)

        atr_s   = atr(df, length=14)
        rsi_s   = rsi(df, length=14)
        ema8_s  = ema(df, length=8)
        ema20_s = ema(df, length=20)
        st_df   = supertrend(df, length=14, multiplier=2.5)
        adx_s   = adx(df, length=20)

        last_close = float(df["c"].iloc[-1])
        last_atr   = atr_s.iloc[-1]
        last_ts    = str(df["t"].iloc[-1])[:16]

        return IndicatorResult(
            symbol=symbol.upper(),
            tf=tf,
            ts=last_ts,
            close=round(last_close, 4),
            atr=round(float(last_atr), 4)             if last_atr is not None else None,
            atr_pct=round(last_atr / last_close * 100, 2) if last_atr           else None,
            rsi=round(float(rsi_s.iloc[-1]), 2)       if rsi_s.iloc[-1] is not None else None,
            ema8=round(float(ema8_s.iloc[-1]), 4)     if ema8_s.iloc[-1] is not None else None,
            ema20=round(float(ema20_s.iloc[-1]), 4)   if ema20_s.iloc[-1] is not None else None,
            st_value=round(float(st_df["value"].iloc[-1]), 4)
                if not st_df["value"].isna().iloc[-1] else None,
            st_dir=int(st_df["direction"].iloc[-1])
                if not st_df["direction"].isna().iloc[-1] else None,
            st_flipped=bool(st_df["flipped"].iloc[-1]),
            adx=round(float(adx_s.iloc[-1]), 1) if not adx_s.isna().iloc[-1] else None,
        )

    async def portfolio_indicators(
        self,
        tf:       str  = "1m",
        extended: bool = True,
    ) -> PortfolioIndicatorResult:
        """Run indicators for all non-ignored portfolio positions concurrently."""
        from services.fundamentals_service import FundamentalsService

        positions = await self._broker.get_positions()
        if not positions:
            return PortfolioIndicatorResult()

        ignore    = _load_ignore()
        active    = [p for p in positions if not _is_ignored(p, ignore)]
        broker_id = getattr(self._broker, "broker_id", "")
        svc       = FundamentalsService(api_key=os.environ.get("FMP_API_KEY", ""))

        skipped: list[tuple[str, str]] = []
        to_resolve = []
        for pos in active:
            isin = pos.id or ""
            if not isin or not isin[:2].isalpha() or len(isin) < 12:
                skipped.append((pos.symbol, "no valid ISIN"))
            else:
                to_resolve.append(pos)

        async def _resolve(pos):
            t = await svc.get_ticker_from_isin(pos.id, name_hint=pos.symbol, broker_id=broker_id)
            return pos, t

        resolved   = await asyncio.gather(*[_resolve(p) for p in to_resolve])
        ticker_map = []
        for pos, ticker in resolved:
            if not ticker:
                skipped.append((pos.symbol, "ticker not found"))
            elif _is_ignored(pos, ignore, ticker=ticker):
                pass
            else:
                ticker_map.append((pos, ticker))

        async def _fetch_one(pos, ticker):
            try:
                ind = await self.indicators(ticker, tf, extended=extended)
                return PortfolioIndicatorRow(ticker=ticker, pos_name=pos.symbol, indicators=ind), None
            except Exception as e:
                return None, (pos.symbol, f"error: {str(e)[:60]}")

        raw  = await asyncio.gather(*[_fetch_one(pos, ticker) for pos, ticker in ticker_map])
        rows = []
        for row, err in raw:
            if err:
                skipped.append(err)
            else:
                rows.append(row)

        return PortfolioIndicatorResult(rows=rows, skipped=skipped)

    async def portfolio_correlations(
        self,
        rows: list[PortfolioIndicatorRow],
    ) -> dict[str, dict[str, float]]:
        """60d daily-return correlation between every pair of tickers in rows.

        Independent of the timeframe used for portfolio_indicators — correlation
        is always computed on daily closes for stability, matching the convention
        used elsewhere (see trade_helper_service._compute_portfolio_corrs).
        Returns a symmetric {ticker: {other_ticker: corr}} map; pairs where either
        side lacks enough history are omitted.
        """
        from services.fundamentals_service import FundamentalsService

        svc = FundamentalsService(api_key=os.environ.get("FMP_API_KEY", ""))

        async def _returns(ticker: str):
            try:
                df = await svc.get_ohlcv(ticker, timeframe="1d", limit=70)
                ret = df["c"].pct_change().dropna().tail(60)
                return ticker, ret if len(ret) >= 20 else None
            except Exception:
                return ticker, None

        tickers = sorted({row.ticker for row in rows})
        results = await asyncio.gather(*[_returns(t) for t in tickers])
        rets = {t: r for t, r in results if r is not None}

        matrix: dict[str, dict[str, float]] = {}
        keys = list(rets.keys())
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                n = min(len(rets[a]), len(rets[b]))
                if n < 20:
                    continue
                corr = float(rets[a].iloc[-n:].corr(rets[b].iloc[-n:]))
                matrix.setdefault(a, {})[b] = corr
                matrix.setdefault(b, {})[a] = corr
        return matrix

    # ── Ignore list ───────────────────────────────────────────────────────────

    def ignore_list_add(self, name: str) -> None:
        ig = _load_ignore(); ig.add(name.lower()); _save_ignore(ig)

    def ignore_list_remove(self, name: str) -> None:
        ig = _load_ignore(); ig.discard(name.lower()); _save_ignore(ig)

    def ignore_list_get(self) -> set[str]:
        return _load_ignore()

    # ── Trading ───────────────────────────────────────────────────────────────

    async def buy(
        self,
        symbol:        str,
        size_token:    str,
        trigger_token: Optional[str] = None,
        forced_type:   Optional[str] = None,
    ):
        """Place a buy order. Returns placed Order."""
        return await self._place_order("buy", symbol, size_token, trigger_token, forced_type)

    async def sell(
        self,
        symbol:        str,
        size_token:    str = "all",
        trigger_token: Optional[str] = None,
        forced_type:   Optional[str] = None,
    ):
        """Place a sell order. Returns placed Order."""
        return await self._place_order("sell", symbol, size_token, trigger_token, forced_type)

    async def _place_order(
        self,
        side_str:      str,
        symbol:        str,
        size_token:    str,
        trigger_token: Optional[str],
        forced_type:   Optional[str],
    ):
        from core.entities.broker_entities import OrderSide, OrderType
        from services.fundamentals_service import FundamentalsService
        from services.position_service     import find_position
        from services.trade_service        import parse_trigger, calc_quantity, infer_order_type

        SIDE       = OrderSide.BUY if side_str == "buy" else OrderSide.SELL
        native_eur = getattr(self._broker, "native_currency", "USD").upper() == "EUR"
        svc        = FundamentalsService()

        # EUR-native brokers: bare number → treat as EUR amount
        if native_eur and size_token:
            st_lower = size_token.lower()
            if (not st_lower.startswith(("e", "$"))
                    and st_lower != "all"
                    and not st_lower.endswith("%")):
                try:
                    float(size_token)
                    size_token = f"e{size_token}"
                except ValueError:
                    pass

        st             = size_token.lower()
        trigger_in_eur = native_eur or st.startswith("e")

        pos = None
        if st.endswith("%") or st == "all" or SIDE == OrderSide.SELL:
            pos = await find_position(self._broker, symbol)
            if not pos:
                raise CommandError(f"No open position for {symbol}")

        pos_qty = float(pos.quantity) if pos else 0.0

        trigger_is_usd = trigger_token is not None and trigger_token.startswith("@$")
        usd_rate: Optional[float] = None
        if not trigger_in_eur or trigger_is_usd:
            try:
                usd_rate = await svc.get_fx_rate("USD", "EUR")
            except Exception:
                pass

        quote = await self._broker.get_quote(symbol.upper())
        if not quote:
            raise CommandError(f"No quote for {symbol}")
        pricing_price = (quote.ask if SIDE == OrderSide.BUY else quote.bid) or quote.last or quote.mid
        if not pricing_price:
            raise CommandError(f"Cannot determine price for {symbol}")

        try:
            qty, _ = calc_quantity(size_token, SIDE, pos_qty, pricing_price, usd_rate)
        except ValueError as e:
            raise CommandError(str(e))

        if qty <= 0:
            raise CommandError(f"Quantity resolved to 0 for size '{size_token}'")

        order_type    = OrderType.MARKET
        trigger_price: Optional[float] = None
        if trigger_token:
            trig = parse_trigger(trigger_token)
            if trig["kind"] == "price":
                trigger_price = trig["usd"]
                if trig.get("force_usd") and usd_rate:
                    trigger_price = round(trigger_price * usd_rate, 4)
                order_type    = infer_order_type(SIDE, pricing_price, trigger_price)
            elif trig["kind"] == "atr":
                try:
                    ind = await self.indicators(symbol, trig.get("tf", "1m"))
                    if ind.atr:
                        trigger_price = (
                            pricing_price + trig["mult"] * ind.atr
                            if SIDE == OrderSide.BUY
                            else pricing_price - trig["mult"] * ind.atr
                        )
                        order_type = infer_order_type(SIDE, pricing_price, trigger_price)
                except Exception:
                    pass

        if forced_type == "stop":
            order_type = OrderType.STOP
        elif forced_type == "limit":
            order_type = OrderType.LIMIT

        return await self._broker.place_order(symbol, qty, SIDE, order_type, price=trigger_price)

    async def close(self, symbol: str) -> CloseResult:
        """Close position for symbol."""
        from services.position_service import find_position
        pos = await find_position(self._broker, symbol)
        if not pos:
            raise CommandError(f"No open position for {symbol}")
        try:
            ok = await self._broker.close_position(pos.id)
            return CloseResult(symbol=symbol, position_id=pos.id or "", success=bool(ok))
        except Exception as e:
            return CloseResult(symbol=symbol, position_id=pos.id or "", success=False, error=str(e))

    async def closeall(self) -> list[CloseResult]:
        """Close all open positions."""
        positions = await self._broker.get_positions()
        if not positions:
            return []
        raw = await asyncio.gather(
            *[self.close(p.symbol) for p in positions],
            return_exceptions=True,
        )
        results = []
        for pos, res in zip(positions, raw):
            if isinstance(res, Exception):
                results.append(CloseResult(
                    symbol=pos.symbol, position_id=pos.id or "",
                    success=False, error=str(res),
                ))
            else:
                results.append(res)
        return results

    async def stop_loss(self, symbol: str, price: float) -> bool:
        """Update stop-loss. Returns True on success, raises CommandError otherwise."""
        from services.position_service import find_position
        pos = await find_position(self._broker, symbol)
        if not pos:
            raise CommandError(f"No open position for {symbol}")
        ok = await self._broker.update_position_stops(pos.id, stop_loss_price=price)
        if not ok:
            raise CommandError("Stop-loss not supported by this broker")
        return True

    # ── Scanners ──────────────────────────────────────────────────────────────

    async def scan(self, scan_type: str) -> str:
        """Run a market scanner. Returns the full text output."""
        key = scan_type.lower()
        if key not in _SCANNER_MAP:
            valid = " | ".join(_SCANNER_MAP)
            raise CommandError(f"Unknown scanner '{scan_type}'. Use: {valid}")

        mod_name, kwargs = _SCANNER_MAP[key]
        mod = _load_scanner(mod_name)

        buf = io.StringIO()
        with redirect_stdout(buf):
            mod.run_scanner(**kwargs)
        return buf.getvalue().strip()

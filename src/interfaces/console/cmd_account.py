"""
interfaces.console.cmd_account

Handlers for account-info commands: /a /p /o /pnl /q /fills
"""
from __future__ import annotations

import json as _json
import re as _re
from pathlib import Path as _Path

from services.position_service import find_position as _find_position, get_position_for_ticker as _get_position_for_ticker
from services.pnl_service      import get_fills as _get_fills, calc_pnl as _calc_pnl
from interfaces.console.formatters import tty_colors

_LIST_TICKER_RE   = _re.compile(r"^[A-Z0-9]{1,10}(\.[A-Z]{1,3})?$")
_LIST_IGNORE_FILE = _Path(__file__).parents[3] / "data" / "indp_ignore.json"


def _list_ignored() -> set[str]:
    try:
        if _LIST_IGNORE_FILE.exists():
            return {s.upper() for s in _json.loads(_LIST_IGNORE_FILE.read_text())}
    except Exception:
        pass
    return set()


def _is_listable(symbol: str) -> bool:
    return bool(_LIST_TICKER_RE.match(symbol.upper())) and symbol.upper() not in _list_ignored()


async def cmd_account(broker, args: list) -> None:
    try:
        acc = await broker.get_account_info()
        print(
            f"\n💼 Account\n"
            f"  Value:    {acc.current_value:>12,.2f}\n"
            f"  Cash:     {acc.cash_in_hand:>12,.2f}\n"
            f"  Margin:   {acc.margin_used:>12,.2f} used / {acc.margin_available:>10,.2f} free\n"
            f"  Leverage: {acc.leverage}x\n"
            f"  Currency: {acc.currency}\n"
        )
    except Exception as e:
        print(f"❌ {e}")


async def cmd_positions(broker, args: list) -> None:
    if args and args[0].lower() == "list":
        try:
            positions = await broker.get_positions()
            if not positions:
                print("(no open positions)")
            else:
                syms = [p.symbol for p in positions if p.symbol and _is_listable(p.symbol)]
                print(", ".join(syms) if syms else "(no valid symbols)")
        except Exception as e:
            print(f"❌ {e}")
        return

    symbol = args[0].upper() if args else None
    try:
        if symbol:
            pos = await _find_position(broker, symbol)
            positions = [pos] if pos else []
        else:
            positions = await broker.get_positions()
        if not positions:
            print("📭 No open positions.")
            return

        GREEN, RED, RESET = tty_colors()

        if symbol and len(positions) == 1:
            # Single position — fetch live bid and compute PnL from it
            p = positions[0]
            try:
                q   = await broker.get_quote(p.id or p.symbol)
                bid = float(q.bid or q.mid or q.last or 0)
                upl = (bid - (p.average_price or 0)) * (p.quantity or 0)
                bid_str = f"{bid:,.4f}"
                src_tag = "  [live bid]"
            except Exception:
                bid     = None
                upl     = p.unrealized_pnl or 0
                bid_str = "—"
                src_tag = "  [broker]"
            sign    = "+" if upl >= 0 else ""
            upl_col = GREEN if upl >= 0 else RED
            print(f"\n📋 {p.symbol}  qty={p.quantity}  avg={p.average_price:,.4f}  bid={bid_str}{src_tag}")
            print(f"   P&L  {upl_col}{sign}{upl:,.2f}{RESET}")
            print()
        else:
            # All positions — use broker's cached values (no per-position quote calls)
            total_upl = sum(p.unrealized_pnl or 0 for p in positions)
            total_val = sum(p.market_value   or 0 for p in positions)
            sign      = "+" if total_upl >= 0 else ""
            print(f"\n📋 Positions ({len(positions)}) — {sign}{total_upl:,.2f} unreal | val {total_val:,.2f}")
            print("  " + "─" * 60)
            for p in positions:
                upl     = p.unrealized_pnl or 0
                sign    = "+" if upl >= 0 else ""
                upl_col = GREEN if upl >= 0 else RED
                mkt_val = p.market_value or ((p.quantity or 0) * (p.average_price or 0) + upl)
                cost    = (p.quantity or 0) * (p.average_price or 0)
                pct     = p.unrealized_pnl_percentage or (upl / cost * 100 if cost else 0)
                sign_p  = "+" if pct >= 0 else ""
                pct_col = GREEN if pct >= 0 else RED
                print(
                    f"  {p.symbol:<8}  qty={p.quantity:>8}  avg={p.average_price:>10,.4f}"
                    f"  val={mkt_val:>10,.2f}"
                    f"  pnl={upl_col}{sign}{upl:>10,.2f}{RESET}"
                    f"  ({pct_col}{sign_p}{pct:.1f}%{RESET})"
                )
            print()
    except Exception as e:
        print(f"❌ {e}")


async def cmd_orders(broker, args: list) -> None:
    symbol = args[0].upper() if args else None
    try:
        orders = await broker.get_orders(symbol=symbol)
        if not orders:
            msg = f"📭 No recent orders for {symbol}." if symbol else "📭 No recent orders."
            print(msg)
            return
        orders = sorted(
            orders,
            key=lambda o: o.filled_timestamp or o.cancelled_timestamp or o.placed_timestamp or 0,
            reverse=True,
        )
        header = f"📋 Orders — {symbol} ({min(len(orders), 10)})" if symbol else f"📋 Orders ({min(len(orders), 10)})"
        print(f"\n{header}")
        print("  " + "─" * 60)
        for o in orders[:10]:
            price  = f"{o.price:,.4f}" if getattr(o, "price", None) else "MKT"
            ts     = o.filled_timestamp or o.cancelled_timestamp or o.placed_timestamp
            ts_str = ts.strftime("%H:%M:%S") if ts else "—"
            print(f"  {ts_str}  {o.symbol:<8}  {str(o.side):<5}  qty={o.quantity:>8}  @ {price:>10}  [{o.status}]")
        print()
    except Exception as e:
        print(f"❌ {e}")


async def cmd_pnl(broker, args: list) -> None:
    try:
        positions = await broker.get_positions()
        if not positions:
            print("📭 No open positions.")
            return
        total_upl = sum(p.unrealized_pnl or 0 for p in positions)
        total_val = sum(p.market_value   or 0 for p in positions)
        sign      = "+" if total_upl >= 0 else ""
        emoji     = "🟢" if total_upl >= 0 else "🔴"
        print(
            f"\n{emoji} Open P&L\n"
            f"  Unrealised: {sign}{total_upl:,.2f}\n"
            f"  Market val: {total_val:,.2f}\n"
            f"  Positions:  {len(positions)}\n"
        )
    except Exception as e:
        print(f"❌ {e}")


async def cmd_quote(broker, args: list) -> None:
    if not args:
        print("Usage: /q SYMBOL")
        return
    symbol = args[0].upper()
    try:
        q          = await broker.get_quote(symbol)
        spread_pct = float(q.spread / q.ask * 100) if q.ask else 0.0
        ts         = q.timestamp.strftime("%H:%M:%S") if q.timestamp else "—"
        print(
            f"\n📈 Quote — {symbol}\n"
            f"  Bid:    {float(q.bid):>10,.4f}  ({q.bid_size})\n"
            f"  Ask:    {float(q.ask):>10,.4f}  ({q.ask_size})\n"
            f"  Last:   {float(q.last):>10,.4f}\n"
            f"  Mid:    {float(q.mid):>10,.4f}\n"
            f"  Spread: {float(q.spread):>10,.4f}  ({spread_pct:.3f}%)\n"
            f"  Time:   {ts}\n"
        )
    except Exception as e:
        print(f"❌ {e}")


async def cmd_fills(broker, args: list) -> None:
    if not args:
        print("Usage: /fills SYMBOL [N]  — fill history + P&L (last N trades)")
        return
    symbol = args[0].upper()
    limit  = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    print(f"⏳ Fetching fills for {symbol}…")
    try:
        fills = await _get_fills(broker, symbol)
        if not fills:
            print(f"No filled orders found for {symbol}.")
            return
        if limit:
            fills = fills[-limit:]

        open_pos      = await _get_position_for_ticker(broker, symbol)
        position_size = float(open_pos.quantity)      if open_pos else 0.0
        broker_avg    = float(open_pos.average_price) if open_pos else 0.0
        current_bid   = 0.0
        try:
            q           = await broker.get_quote(symbol)
            current_bid = float(q.bid or q.mid or q.last or 0)
        except Exception:
            pass

        result = _calc_pnl(fills, position_size, current_bid)

        # Broker's own FIFO avg is more accurate than fill history for unrealized P&L
        if position_size > 0 and broker_avg > 0 and current_bid > 0:
            unrealized = (current_bid - broker_avg) * position_size
        else:
            unrealized = result["unrealized"]
            broker_avg = result["avg_buy"]

        GREEN, RED, RESET = tty_colors()

        def _pnl_color(v: float) -> str:
            return f"{'%s' % GREEN if v >= 0 else RED}{v:+.2f}{RESET}"

        print(f"\n  Fills — {symbol}  ({len(result['rows'])} in batch)")
        print(f"  {'─'*52}")
        print(f"  {'Date':<12}  {'Side':<5}  {'Qty':>8}  {'Price €':>10}  {'Notional €':>11}")
        print(f"  {'─'*52}")
        for row in result["rows"]:
            ts       = row["ts"].strftime("%Y-%m-%d") if row["ts"] else "—"
            notional = row["qty"] * row["price"]
            sign     = -1 if row["side"] == "BUY" else 1
            print(
                f"  {ts:<12}  {row['side']:<5}  {row['qty']:>8.0f}  "
                f"{row['price']:>10.4f}  {sign * notional:>+11.2f}"
            )
        print(f"  {'─'*52}")
        if result["buy_qty"] > 0:
            print(f"  Avg buy:       €{result['avg_buy']:.4f}  ({result['buy_qty']:.0f} shares)")
        if result["sell_qty"] > 0:
            print(f"  Avg sell:      €{result['avg_sell']:.4f}  ({result['sell_qty']:.0f} shares)")
        if position_size > 0:
            bid_s = f"  bid €{current_bid:.4f}" if current_bid else ""
            print(f"  Open:          {position_size:.0f} shares @ avg €{broker_avg:.4f}{bid_s}")
            print(f"  Unrealized:    {_pnl_color(unrealized)}")
        else:
            print(f"  Position:      closed")
        print(f"  Realized P&L:  {_pnl_color(result['realized'])}")
        print(f"  Total P&L:     {_pnl_color(result['realized'] + unrealized)}")
        print()
    except Exception as e:
        print(f"❌ {e}")

"""
interfaces.console.cmd_trading

Handlers for trading commands: /buy /sell /move /close /closeall /pending /cancel /size
"""
from __future__ import annotations

from services.position_service      import find_position as _find_position
from services.trade_service         import (
    parse_trigger, calc_quantity, infer_order_type,
    calc_position_size_by_risk, TIMEFRAMES,
)
from interfaces.console.formatters  import print_order_preview, print_triggered_preview


async def cmd_trade(broker, side_str: str, args: list) -> None:
    """Unified handler for /buy and /sell."""
    from core.entities.broker_entities import OrderSide, OrderType
    from services.fundamentals_service import FundamentalsService

    SIDE       = OrderSide.BUY if side_str in ("buy", "b") else OrderSide.SELL
    SIDE_LABEL = "BUY" if SIDE == OrderSide.BUY else "SELL"

    if not args:
        print(f"Usage: /{side_str} SYMBOL [SIZE] [@TRIGGER] [stop|limit]")
        print("  SIZE: N shares | N% | all | eN euros | $N USD")
        print(f"  e.g. /{side_str} WOLF 50%  |  /{side_str} PLTR e3000 @118")
        return

    symbol = args[0].upper()

    size_token    = None
    trigger_token = None
    forced_type   = None
    for tok in args[1:]:
        if tok.startswith("@"):
            trigger_token = tok
        elif tok.lower() in ("stop", "limit"):
            forced_type = tok.lower()
        elif size_token is None:
            size_token = tok

    if size_token is None:
        if SIDE == OrderSide.SELL:
            size_token = "all"
        else:
            print(f"Usage: /{side_str} SYMBOL SIZE [@TRIGGER] [stop|limit]")
            print("  SIZE: N shares | N% | all | eN euros | $N USD")
            return

    svc = FundamentalsService()
    st  = size_token.lower()

    # ── Position (needed for % / all sizes and sell guard) ────────────────────
    pos = None
    if st.endswith("%") or st == "all" or SIDE == OrderSide.SELL:
        pos = await _find_position(broker, symbol)
        if not pos:
            print(f"❌ No open position for {symbol}")
            return

    # ── Pending order guard (sell only) ──────────────────────────────────────
    # Pending stop/limit orders lock up shares, so a % or all sell will exceed
    # the broker's sellable quantity and be rejected.
    if SIDE == OrderSide.SELL:
        try:
            pending = await broker.get_pending_orders()
            sym_pending = [
                o for o in pending
                if o.symbol.upper() == symbol.upper()
                and o.status.value not in ("filled", "cancelled", "rejected")
            ]
            if sym_pending:
                print(f"\n  ⚠️  {len(sym_pending)} pending order(s) on {symbol}:")
                for o in sym_pending:
                    type_s  = o.order_type.value.upper() if o.order_type else "MKT"
                    side_s  = "BUY" if "BUY" in str(o.side).upper() else "SELL"
                    price_s = f"€{o.price:,.4f}" if o.price else "MKT"
                    oid     = (o.broker_order_id or o.id or "")[:14]
                    print(f"     {oid}  {side_s} {int(o.quantity)} {type_s} @ {price_s}")
                print()
                choice = input(
                    "  [c] cancel pending orders then proceed\n"
                    "  [k] keep pending orders and proceed anyway\n"
                    "  [q] quit\n"
                    "  › "
                ).strip().lower()
                if choice == "q":
                    print("❌ Order cancelled.")
                    return
                if choice == "c":
                    for o in sym_pending:
                        oid = o.broker_order_id or o.id
                        try:
                            ok = await broker.cancel_order(oid)
                            print(f"  {'✅' if ok else '❌'} Cancelled {oid}")
                        except Exception as ce:
                            print(f"  ❌ Could not cancel {oid}: {ce}")
                # "k" falls through and proceeds
        except Exception:
            pass  # non-fatal — broker may not support get_pending_orders

    # ── Quote (needed for notional sizing or trigger inference) ───────────────
    quote = None
    if st.startswith("e") or st.startswith("$") or trigger_token is not None:
        try:
            quote = await broker.get_quote(symbol)
        except Exception as e:
            print(f"❌ Could not get quote for {symbol}: {e}")
            return
        _i_cache = getattr(broker, "_isin_cache", {})
        _n_cache = getattr(broker, "_isin_name_cache", {})
        _r_isin  = _i_cache.get(symbol.upper(), "")
        _r_name  = _n_cache.get(_r_isin.upper(), "") if _r_isin else ""
        if _r_name:
            print(f"  Instrument:  {_r_name}  [{_r_isin}]")

    # ── FX rate ───────────────────────────────────────────────────────────────
    rate = None
    if st.startswith("$") or trigger_token is not None:
        rate = await svc.get_fx_rate("USD", "EUR")
        if not rate:
            print("❌ Could not fetch USD/EUR rate")
            return

    # ── Quantity ──────────────────────────────────────────────────────────────
    sizing_price_eur = float(
        (quote.ask if SIDE == OrderSide.BUY else quote.bid) if quote else 0
    )
    try:
        qty, qty_desc = calc_quantity(
            size_token, SIDE, float(pos.quantity) if pos else 0,
            sizing_price_eur, rate,
        )
        if qty_desc:
            print(f"  {qty_desc}")
    except ValueError as e:
        print(f"❌ {e}")
        return

    # ── Trigger ───────────────────────────────────────────────────────────────
    order_type  = OrderType.MARKET
    trigger_usd = None
    trigger_eur = None

    if trigger_token:
        try:
            trig = parse_trigger(trigger_token)
        except (ValueError, IndexError):
            print(f"❌ Invalid trigger: {trigger_token}")
            return

        if trig["kind"] == "atr":
            tf   = trig["tf"]
            mult = trig["mult"]
            if tf not in TIMEFRAMES:
                print(f"❌ Unknown timeframe: {tf}")
                return
            print(f"⏳ Fetching {symbol} {tf} data for ATR…")
            from services.indicators_service import atr as calc_atr
            df          = await svc.get_ohlcv(symbol, timeframe=tf, limit=60, extended=True)
            atr_s       = calc_atr(df, length=14)
            last_close  = float(df["c"].iloc[-1])
            last_atr    = float(atr_s.iloc[-1])
            offset      = mult * last_atr
            trigger_usd = round(
                last_close - offset if SIDE == OrderSide.SELL else last_close + offset, 4
            )
            if trigger_usd <= 0:
                print(f"❌ Calculated trigger ${trigger_usd:.4f} ≤ 0")
                return
            trigger_eur = round(trigger_usd * rate, 4)
            print(
                f"  last_close=${last_close:.4f}  ATR={last_atr:.4f}  "
                f"offset={mult}×ATR=${offset:.4f}\n"
                f"  Trigger: ${trigger_usd:.4f} → €{trigger_eur:.4f}  (USD/EUR {rate:.4f})"
            )
        else:
            trigger_usd = trig["usd"]
            trigger_eur = round(trigger_usd * rate, 4)
            print(f"  Trigger: ${trigger_usd:.4f} → €{trigger_eur:.4f}  (USD/EUR {rate:.4f})")

        order_type = infer_order_type(SIDE, trigger_eur, sizing_price_eur, forced_type)
        type_label = order_type.value.upper()
        cmp_sym    = (
            "≥" if order_type == OrderType.STOP  and SIDE == OrderSide.BUY  else
            "≤" if order_type == OrderType.STOP                              else
            "≥" if SIDE == OrderSide.SELL                                    else "≤"
        )
        print(f"  Order type:  {type_label}-{SIDE_LABEL}  (trigger {cmp_sym} €{trigger_eur:.4f})")

        # Warn if BUY STOP is already above market
        if SIDE == OrderSide.BUY and order_type == OrderType.STOP:
            current_ask_eur = float(quote.ask)
            if trigger_eur <= current_ask_eur:
                current_ask_usd = round(current_ask_eur / rate, 4)
                print(
                    f"\n  ⚠️  Current ask ${current_ask_usd:.4f} (€{current_ask_eur:.4f}) is already "
                    f"ABOVE your trigger ${trigger_usd:.4f}.\n"
                    f"     A buy stop fires on breakout — trigger should be above current price.\n"
                    f"     Drop @TRIGGER to buy at market, or append 'limit' to place a limit buy."
                )
                if input("  Proceed anyway? [y/N] › ").strip().lower() != "y":
                    print("❌ Order cancelled.")
                    return

        # Warn if trigger is very far from current price
        if sizing_price_eur:
            dist_pct = abs(trigger_eur - sizing_price_eur) / sizing_price_eur * 100
            if dist_pct > 10:
                price_label = "ask" if SIDE == OrderSide.BUY else "bid"
                print(
                    f"\n  ⚠️  Trigger €{trigger_eur:.4f} is {dist_pct:.0f}% away from "
                    f"current {price_label} €{sizing_price_eur:.4f} — possible wrong ticker or stale price."
                )
                if input("  Proceed anyway? [y/N] › ").strip().lower() != "y":
                    print("❌ Order cancelled.")
                    return

    # ── Preview ───────────────────────────────────────────────────────────────
    type_label     = order_type.value.upper() if order_type != OrderType.MARKET else "MARKET"
    display_symbol = (
        f"{symbol} ({pos.symbol})"
        if pos and pos.symbol.upper() != symbol.upper()
        else symbol
    )
    print(f"⏳ Previewing {SIDE_LABEL} {qty} {display_symbol} [{type_label}]…")
    preview, confirm_id = await broker.preview_order(
        symbol, qty, SIDE, order_type, price=trigger_eur
    )

    if order_type == OrderType.MARKET:
        print_order_preview(SIDE_LABEL, display_symbol, qty, preview)
    else:
        print_triggered_preview(
            type_label, SIDE_LABEL, display_symbol, qty,
            trigger_usd, trigger_eur, rate, preview,
        )

    inst_name = preview.get("_instrument_name", "")
    inst_tag  = f" · {inst_name}" if inst_name else ""
    if input(f"  Confirm {SIDE_LABEL} {qty} × {symbol}{inst_tag}? [y/N] › ").strip().lower() == "y":
        order = await broker.submit_order(
            symbol, qty, SIDE, confirm_id, preview,
            order_type=order_type, price=trigger_eur,
        )
        print(f"✅ {SIDE_LABEL} {type_label} submitted — ID: {order.broker_order_id}")
    else:
        print("❌ Order cancelled.")


async def cmd_move(broker, args: list) -> None:
    """
    /move SYMBOL1 SIZE SYMBOL2
    Sell SIZE of SYMBOL1 at market, wait for fill, then buy SYMBOL2 with proceeds.
    """
    from core.entities.broker_entities import OrderSide, OrderType
    from services.fundamentals_service import FundamentalsService

    if len(args) < 3:
        print(
            "Usage: /move SYMBOL1 SIZE SYMBOL2\n"
            "  SIZE (shares):  /move UEC 100 PLTR\n"
            "  SIZE (amount):  /move UEC e2000 PLTR  |  /move UEC $1500 PLTR\n"
            "                  /move UEC 50% PLTR    |  /move UEC all PLTR"
        )
        return

    symbol1  = args[0].upper()
    size_tok = args[1]
    symbol2  = args[2].upper()
    st       = size_tok.lower()
    svc      = FundamentalsService()

    pos = await _find_position(broker, symbol1)
    if not pos:
        print(f"❌ No open position for {symbol1}")
        return

    rate       = None
    sizing_bid = 0.0
    if st.startswith("$"):
        rate = await svc.get_fx_rate("USD", "EUR")
        if not rate:
            print("❌ Could not fetch USD/EUR rate")
            return
    if st.startswith("e") or st.startswith("$"):
        try:
            q1         = await broker.get_quote(symbol1)
            sizing_bid = float(q1.bid)
        except Exception as e:
            print(f"❌ Quote for {symbol1}: {e}")
            return

    try:
        qty, qty_desc = calc_quantity(
            size_tok, OrderSide.SELL, float(pos.quantity), sizing_bid, rate
        )
        if qty_desc:
            print(f"  {qty_desc}")
    except ValueError as e:
        print(f"❌ {e}")
        return

    display1 = (
        f"{symbol1} ({pos.symbol})" if pos.symbol.upper() != symbol1.upper() else symbol1
    )
    print(f"⏳ Previewing SELL {qty} {display1}…")
    try:
        preview1, confirm_id1 = await broker.preview_order(
            symbol1, qty, OrderSide.SELL, OrderType.MARKET
        )
    except Exception as e:
        print(f"❌ {e}")
        return

    print_order_preview("SELL", display1, qty, preview1)

    est_proceeds = float(preview1.get("est_volume") or 0)
    if not est_proceeds:
        bid_val      = float(preview1.get("bid") or pos.average_price or 0)
        est_proceeds = qty * bid_val

    print(f"\n  ➜  Will BUY {symbol2} with ≈€{est_proceeds:.2f} proceeds\n")

    if input(f"  Confirm: SELL {qty} × {symbol1}  then BUY {symbol2}? [y/N] › ").strip().lower() != "y":
        print("❌ Move cancelled.")
        return

    try:
        sell_order = await broker.submit_order(symbol1, qty, OrderSide.SELL, confirm_id1, preview1)
    except Exception as e:
        print(f"❌ SELL submit failed: {e}")
        return

    fill_qty     = float(sell_order.quantity)
    fill_price   = float(sell_order.average_fill_price or 0)
    proceeds_eur = fill_qty * fill_price
    print(
        f"✅ SELL filled — {fill_qty:.0f} × {symbol1} @ €{fill_price:.4f}  "
        f"proceeds = €{proceeds_eur:.2f}"
    )

    print(f"⏳ Previewing BUY of {symbol2} with €{proceeds_eur:.2f}…")
    try:
        quote2  = await broker.get_quote(symbol2)
        ask2    = float(quote2.ask)
        if not ask2:
            raise RuntimeError(f"No ask price for {symbol2}")
        buy_qty  = max(1, int(proceeds_eur / ask2))
        preview2, confirm_id2 = await broker.preview_order(
            symbol2, buy_qty, OrderSide.BUY, OrderType.MARKET
        )
    except Exception as e:
        print(f"❌ BUY preview failed: {e}")
        print(f"  ⚠️  Proceeds €{proceeds_eur:.2f} are in cash — deploy manually with /buy {symbol2}")
        return

    print_order_preview("BUY", symbol2, buy_qty, preview2)

    try:
        buy_order  = await broker.submit_order(symbol2, buy_qty, OrderSide.BUY, confirm_id2, preview2)
        buy_price  = float(buy_order.average_fill_price or 0)
        print(f"✅ BUY filled  — {buy_qty} × {symbol2} @ €{buy_price:.4f}")
        print(f"   Move complete:  sold {fill_qty:.0f} {symbol1} → bought {buy_qty} {symbol2}")
    except Exception as e:
        print(f"❌ BUY failed: {e}")
        print(f"  ⚠️  Proceeds €{proceeds_eur:.2f} are in cash — deploy manually with /buy {symbol2}")


async def cmd_close(broker, args: list) -> None:
    if not args:
        print("Usage: /close SYMBOL")
        return
    symbol = args[0].upper()
    try:
        pos = await _find_position(broker, symbol)
        if not pos:
            print(f"❌ No open position for {symbol}")
            return

        # Cancel pending orders first so the broker doesn't reject the close
        try:
            pending = await broker.get_pending_orders()
            sym_pending = [
                o for o in pending
                if o.symbol.upper() == symbol.upper()
                and o.status.value not in ("filled", "cancelled", "rejected")
            ]
            if sym_pending:
                print(f"\n  ⚠️  {len(sym_pending)} pending order(s) on {symbol} — cancelling first…")
                for o in sym_pending:
                    oid = o.broker_order_id or o.id
                    try:
                        ok = await broker.cancel_order(oid)
                        print(f"  {'✅' if ok else '❌'} Cancelled {oid}")
                    except Exception as ce:
                        print(f"  ❌ Could not cancel {oid}: {ce}")
        except Exception:
            pass

        ok = await broker.close_position(pos.id)
        print(f"{'✅' if ok else '❌'} Close {'sent' if ok else 'failed'} for {symbol}")
    except Exception as e:
        print(f"❌ {e}")


async def cmd_closeall(broker, args: list) -> None:
    pct = None
    if args:
        try:
            pct = float(args[0].rstrip("%"))
            if not 1 <= pct <= 100:
                raise ValueError
        except ValueError:
            print("❌ Invalid percentage. Usage: /closeall [PCT%]")
            return
    try:
        from core.entities.broker_entities import OrderSide, OrderType
        positions = await broker.get_positions()
        if not positions:
            print("📭 No open positions.")
            return
        print(f"\n{len(positions)} positions — y=sell  n/Enter=skip  q=quit\n")
        for pos in positions:
            qty     = (
                max(1, round(float(pos.quantity) * pct / 100))
                if pct is not None and pct < 100
                else int(pos.quantity)
            )
            val_str = f"€{pos.market_value:,.2f}" if pos.market_value else ""
            ans     = input(f"  Sell {pos.symbol}  {qty} shares {val_str}? [y/N/q] › ").strip().lower()
            if ans == "q":
                print("↩ Stopped.")
                break
            if ans != "y":
                print(f"  ↩ Skipped {pos.symbol}")
                continue
            try:
                if pct is None or pct >= 100:
                    await broker.close_position(pos.id)
                    print(f"  ✅ {pos.symbol} closed")
                else:
                    preview, confirm_id = await broker.preview_order(
                        pos.id, qty, OrderSide.SELL, OrderType.MARKET
                    )
                    order      = await broker.submit_order(pos.id, qty, OrderSide.SELL, confirm_id, preview)
                    fill_price = float(order.average_fill_price or 0)
                    print(f"  ✅ {pos.symbol}  {qty} shares @ €{fill_price:.4f}")
            except Exception as e:
                print(f"  ❌ {pos.symbol}: {e}")
    except Exception as e:
        print(f"❌ {e}")


async def cmd_pending(broker, args: list) -> None:
    try:
        orders = await broker.get_pending_orders()
        if not orders:
            print("📭 No pending orders.")
            return
        open_orders = [
            o for o in orders
            if str(o.status).upper() not in ("ORDERST.FILLED", "FILLED", "CANCELLED", "REJECTED")
               and o.status.value not in ("filled", "cancelled", "rejected")
        ]
        if not open_orders:
            print("📭 No pending orders.")
            return
        print(f"\n📋 Pending Orders ({len(open_orders)})")
        print("  " + "─" * 66)
        print(f"  {'ID':<14}  {'Symbol':<8}  {'Side':<5}  {'Type':<6}  {'Qty':>6}  {'@ Price':>10}")
        print("  " + "─" * 66)
        for o in open_orders:
            type_s  = o.order_type.value.upper() if o.order_type else "MKT"
            side_s  = "BUY" if "BUY" in str(o.side).upper() else "SELL"
            price_s = f"€{o.price:,.4f}" if o.price else "—"
            oid     = (o.broker_order_id or o.id or "")[:14]
            print(f"  {oid:<14}  {o.symbol:<8}  {side_s:<5}  {type_s:<6}  {int(o.quantity):>6}  {price_s:>10}")
        print()
    except Exception as e:
        print(f"❌ {e}")


async def cmd_cancel(broker, args: list) -> None:
    if not args:
        print("Usage: /cancel SYMBOL|ID|all")
        return
    target = args[0]
    try:
        if target.lower() == "all":
            orders      = await broker.get_pending_orders()
            open_orders = [o for o in orders if o.status.value not in ("filled", "cancelled", "rejected")]
            if not open_orders:
                print("📭 No open orders to cancel.")
                return
            for o in open_orders:
                try:
                    ok = await broker.cancel_order(o.broker_order_id or o.id)
                    print(f"{'✅' if ok else '❌'} {o.symbol} {o.broker_order_id}")
                except Exception as e:
                    print(f"❌ {o.broker_order_id}: {e}")
        else:
            ok = await broker.cancel_order(target)
            print(f"{'✅' if ok else '❌'} Cancel sent for {target}")
    except Exception as e:
        print(f"❌ {e}")


async def cmd_size(broker, args: list) -> None:
    if len(args) < 3:
        print("Usage: /size SYMBOL STOP_USD RISK_EUR")
        print("  e.g. /size WOLF 2.30 200   — risk €200 with a $2.30 stop")
        return
    symbol = args[0].upper()
    try:
        stop_usd = float(args[1].lstrip("$"))
        risk_eur = float(args[2].lstrip("€"))
    except ValueError:
        print("❌ Invalid stop price or risk amount.")
        return
    try:
        from services.fundamentals_service import FundamentalsService
        svc  = FundamentalsService()
        rate = await svc.get_fx_rate("USD", "EUR")
        if not rate:
            print("❌ Could not fetch USD/EUR rate")
            return
        q         = await broker.get_quote(symbol)
        entry_eur = float(q.ask)
        try:
            r = calc_position_size_by_risk(entry_eur, stop_usd, risk_eur, rate)
        except ValueError as e:
            print(f"❌ {e}")
            return
        print(
            f"\n  Position Size — {symbol}\n"
            f"  {'─'*38}\n"
            f"  Entry:        ${r['entry_usd']:.4f}  (€{entry_eur:.4f})\n"
            f"  Stop:         ${stop_usd:.4f}  (€{r['stop_eur']:.4f})\n"
            f"  Risk/share:   ${r['risk_per_share_usd']:.4f}  (€{r['risk_per_share_eur']:.4f})\n"
            f"  Risk budget:  €{risk_eur:.2f}\n"
            f"  {'─'*38}\n"
            f"  Shares:       {r['qty']}\n"
            f"  Notional:     €{r['notional']:,.2f}\n"
        )
    except Exception as e:
        print(f"❌ {e}")

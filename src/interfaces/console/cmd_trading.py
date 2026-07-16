"""
interfaces.console.cmd_trading

Handlers for trading commands: /buy /sell /move /close /closeall /pending /cancel /size
                                /rotate /restore /rsuggest
"""
from __future__ import annotations

import datetime as _dt
import json as _json
from pathlib import Path as _Path

from services.position_service      import find_position as _find_position
from services.trade_service         import (
    parse_trigger, calc_quantity, infer_order_type,
    calc_position_size_by_risk, TIMEFRAMES,
)
from interfaces.console.formatters  import print_order_preview, print_triggered_preview

# ── Rotation state persistence ────────────────────────────────────────────────
_DATA_DIR       = _Path(__file__).resolve().parents[3] / "data"
_ROTATIONS_FILE = _DATA_DIR / "rotations.json"


def _load_rotations() -> dict:
    try:
        return _json.loads(_ROTATIONS_FILE.read_text())
    except (FileNotFoundError, _json.JSONDecodeError):
        return {}


def _save_rotations(state: dict) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    _ROTATIONS_FILE.write_text(_json.dumps(state, indent=2))


def _add_rotation(temp_symbol: str, restore_symbol: str, restore_qty: float,
                  sell_price: float, buy_qty: int) -> None:
    state = _load_rotations()
    state[temp_symbol] = {
        "restore_symbol": restore_symbol,
        "restore_qty":    restore_qty,
        "sell_price":     sell_price,
        "temp_qty":       buy_qty,
        "opened_at":      _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    _save_rotations(state)


def _remove_rotation(temp_symbol: str) -> None:
    state = _load_rotations()
    state.pop(temp_symbol, None)
    _save_rotations(state)


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

    svc        = FundamentalsService()
    native_eur = getattr(broker, "native_currency", "USD").upper() == "EUR"

    # For EUR-native brokers (Scalable), a plain number means EUR amount — no FX needed.
    if native_eur and size_token:
        _st = size_token.lower()
        if not _st.startswith(("e", "$")) and _st not in ("all",) and not _st.endswith("%"):
            try:
                float(size_token)
                size_token = f"e{size_token}"
            except ValueError:
                pass

    st  = size_token.lower()

    # Trigger is in EUR if the broker is EUR-native, or if the user explicitly
    # sized in EUR (eN prefix) — same currency context for both size and trigger.
    trigger_in_eur = native_eur or st.startswith("e")

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
    # EUR-native brokers (Scalable) quote in EUR already — only fetch rate for
    # explicit USD amounts ($N); trigger prices are treated as EUR directly
    # unless the trigger itself carries a $ prefix (force_usd).
    trigger_is_usd = trigger_token is not None and trigger_token.startswith("@$")
    rate = None
    if st.startswith("$") or (trigger_token is not None and not trigger_in_eur) or trigger_is_usd:
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
            if trigger_in_eur:
                trigger_eur = trigger_usd
                print(
                    f"  last_close=€{last_close:.4f}  ATR={last_atr:.4f}  "
                    f"offset={mult}×ATR=€{offset:.4f}\n"
                    f"  Trigger: €{trigger_eur:.4f}"
                )
            else:
                trigger_eur = round(trigger_usd * rate, 4)
                print(
                    f"  last_close=${last_close:.4f}  ATR={last_atr:.4f}  "
                    f"offset={mult}×ATR=${offset:.4f}\n"
                    f"  Trigger: ${trigger_usd:.4f} → €{trigger_eur:.4f}  (USD/EUR {rate:.4f})"
                )
        else:
            trigger_usd = trig["usd"]
            if trigger_in_eur and not trig.get("force_usd"):
                trigger_eur = trigger_usd
                print(f"  Trigger: €{trigger_eur:.4f}")
            else:
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
                if trigger_in_eur:
                    print(
                        f"\n  ⚠️  Current ask €{current_ask_eur:.4f} is already "
                        f"ABOVE your trigger €{trigger_eur:.4f}.\n"
                        f"     A buy stop fires on breakout — trigger should be above current price.\n"
                        f"     Drop @TRIGGER to buy at market, or append 'limit' to place a limit buy."
                    )
                else:
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


async def cmd_rotate(broker, args: list) -> None:
    """
    /rotate FROM SIZE TO [+X%]

    Sell SIZE of FROM, buy TO with proceeds.
    Optionally set a take-profit on TO at +X% above current ask.
    Prints the /restore command to complete the cycle when TO hits target.

    Examples:
      /rotate AAPL 50 PLTR
      /rotate AAPL e3000 PLTR +10
      /rotate AAPL all PLTR +8.5
    """
    from core.entities.broker_entities import OrderSide, OrderType

    if len(args) < 3:
        print(
            "Usage: /rotate FROM SIZE TO [+X%]\n"
            "  Sell SIZE of FROM, buy TO with proceeds.\n"
            "  +X%: optional take-profit on TO at X% above current ask.\n"
            "  After TO hits target run: /restore TO FROM ORIGINAL_QTY\n"
            "  Examples:\n"
            "    /rotate AAPL 50 PLTR\n"
            "    /rotate AAPL e3000 PLTR +10\n"
            "    /rotate AAPL all PLTR +8.5"
        )
        return

    symbol_from = args[0].upper()
    size_tok    = args[1]
    symbol_to   = args[2].upper()
    target_pct: float | None = None

    for tok in args[3:]:
        if tok.startswith("+"):
            try:
                target_pct = float(tok[1:].rstrip("%"))
            except ValueError:
                print(f"❌ Invalid target '{tok}'  (expected e.g. +10 or +8.5%)")
                return

    from services.fundamentals_service import FundamentalsService
    svc = FundamentalsService()

    pos_from = await _find_position(broker, symbol_from)
    if not pos_from:
        print(f"❌ No open position for {symbol_from}")
        return

    original_qty = float(pos_from.quantity)

    st = size_tok.lower()
    rate = None
    quote_from = None
    if st.startswith("e") or st.startswith("$"):
        try:
            quote_from = await broker.get_quote(symbol_from)
        except Exception as e:
            print(f"❌ Quote for {symbol_from}: {e}")
            return
    if st.startswith("$"):
        rate = await svc.get_fx_rate("USD", "EUR")
        if not rate:
            print("❌ Could not fetch USD/EUR rate")
            return

    sizing_bid = float(quote_from.bid) if quote_from else 0.0
    try:
        qty_sell, qty_desc = calc_quantity(
            size_tok, OrderSide.SELL, original_qty, sizing_bid, rate
        )
        if qty_desc:
            print(f"  {qty_desc}")
    except ValueError as e:
        print(f"❌ {e}")
        return

    print(f"⏳ Previewing SELL {qty_sell} {symbol_from}…")
    try:
        preview_sell, confirm_sell = await broker.preview_order(
            symbol_from, qty_sell, OrderSide.SELL, OrderType.MARKET
        )
    except Exception as e:
        print(f"❌ {e}")
        return

    print_order_preview("SELL", symbol_from, qty_sell, preview_sell)

    est_proceeds = float(preview_sell.get("est_volume") or 0)
    if not est_proceeds:
        bid_val      = float(preview_sell.get("bid") or pos_from.average_price or 0)
        est_proceeds = qty_sell * bid_val

    print(f"\n  ➜  Will BUY {symbol_to} with ≈€{est_proceeds:.2f} proceeds")
    print(f"  Restore plan:  /restore {symbol_to} {symbol_from} {qty_sell:.0f}")
    if target_pct is not None:
        print(f"  Take-profit:   will place +{target_pct:.1f}% limit on {symbol_to}")

    print()
    if input(
        f"  Confirm: SELL {qty_sell} × {symbol_from}  then BUY {symbol_to}? [y/N] › "
    ).strip().lower() != "y":
        print("❌ Rotate cancelled.")
        return

    try:
        sell_order = await broker.submit_order(
            symbol_from, qty_sell, OrderSide.SELL, confirm_sell, preview_sell
        )
    except Exception as e:
        print(f"❌ SELL failed: {e}")
        return

    fill_qty   = float(sell_order.quantity)
    fill_price = float(sell_order.average_fill_price or 0)
    proceeds   = fill_qty * fill_price
    print(f"✅ SELL filled — {fill_qty:.0f} × {symbol_from} @ €{fill_price:.4f}  proceeds ≈€{proceeds:.2f}")

    print(f"⏳ Previewing BUY {symbol_to} with €{proceeds:.2f}…")
    ask_to = 0.0
    try:
        quote_to = await broker.get_quote(symbol_to)
        ask_to   = float(quote_to.ask)
        if not ask_to:
            raise RuntimeError(f"No ask price for {symbol_to}")
        buy_qty  = max(1, int(proceeds / ask_to))
        preview_buy, confirm_buy = await broker.preview_order(
            symbol_to, buy_qty, OrderSide.BUY, OrderType.MARKET
        )
    except Exception as e:
        print(f"❌ BUY preview failed: {e}")
        print(f"  ⚠️  Proceeds €{proceeds:.2f} in cash — deploy manually with /buy {symbol_to}")
        return

    print_order_preview("BUY", symbol_to, buy_qty, preview_buy)

    if input(f"  Confirm BUY {buy_qty} × {symbol_to}? [y/N] › ").strip().lower() != "y":
        print(f"  ⚠️  BUY cancelled. Proceeds €{proceeds:.2f} in cash.")
        print(f"     To complete: /buy {symbol_to} e{proceeds:.0f}")
        return

    try:
        buy_order = await broker.submit_order(
            symbol_to, buy_qty, OrderSide.BUY, confirm_buy, preview_buy
        )
        buy_price = float(buy_order.average_fill_price or 0)
        print(f"✅ BUY filled — {buy_qty} × {symbol_to} @ €{buy_price:.4f}")
        _add_rotation(symbol_to, symbol_from, fill_qty, fill_price, buy_qty)
    except Exception as e:
        print(f"❌ BUY failed: {e}")
        print(f"  ⚠️  Proceeds €{proceeds:.2f} in cash — deploy manually with /buy {symbol_to}")
        return

    if target_pct is not None and ask_to:
        tp_price = round(ask_to * (1 + target_pct / 100), 4)
        print(f"\n⏳ Placing take-profit SELL {buy_qty} × {symbol_to} @ €{tp_price:.4f} (+{target_pct:.1f}%)…")
        try:
            tp_preview, tp_confirm = await broker.preview_order(
                symbol_to, buy_qty, OrderSide.SELL, OrderType.LIMIT, price=tp_price
            )
            print_triggered_preview(
                "LIMIT", "SELL", symbol_to, buy_qty,
                tp_price, tp_price, None, tp_preview,
            )
            if input(f"  Confirm TP? [y/N] › ").strip().lower() == "y":
                tp_order = await broker.submit_order(
                    symbol_to, buy_qty, OrderSide.SELL, tp_confirm, tp_preview,
                    order_type=OrderType.LIMIT, price=tp_price,
                )
                print(f"✅ Take-profit placed — ID: {tp_order.broker_order_id}")
            else:
                print(f"  TP skipped. Place manually: /sell {symbol_to} all @{tp_price:.4f} limit")
        except Exception as e:
            print(f"  ⚠️  Could not place TP ({e})")
            print(f"     Place manually: /sell {symbol_to} {buy_qty} @{tp_price:.4f} limit")

    print(f"\n{'─'*54}")
    print(f"  Rotation open:  {fill_qty:.0f} {symbol_from} → {buy_qty} {symbol_to}")
    print(f"  To restore:     /restore {symbol_to} {symbol_from} {fill_qty:.0f}")
    print(f"{'─'*54}")


async def cmd_restore(broker, args: list) -> None:
    """
    /restore FROM TO QTY

    Complete a rotation: sell all of FROM at market, buy back QTY of TO.
    Use after /rotate when FROM has reached its profit target.

    Example:
      /restore PLTR AAPL 50   — sell all PLTR, buy back 50 AAPL
    """
    from core.entities.broker_entities import OrderSide, OrderType

    if len(args) < 2:
        print(
            "Usage: /restore FROM TO [QTY]\n"
            "  Sell all of FROM, buy back QTY shares of TO.\n"
            "  QTY is optional if /rotate was used (saved automatically).\n"
            "  Example: /restore PLTR AAPL 50\n"
            "           /restore PLTR AAPL      ← uses saved qty"
        )
        return

    symbol_from = args[0].upper()
    symbol_to   = args[1].upper()

    saved = _load_rotations().get(symbol_from, {})

    if len(args) >= 3:
        try:
            restore_qty = float(args[2])
        except ValueError:
            print(f"❌ QTY must be a number, got: {args[2]}")
            return
    elif saved and saved.get("restore_symbol", "").upper() == symbol_to:
        restore_qty = float(saved["restore_qty"])
        opened = saved.get("opened_at", "?")
        print(f"  Rotation record found: restore {restore_qty:.0f} × {symbol_to} (opened {opened})")
    else:
        print(
            f"❌ No saved rotation for {symbol_from} → {symbol_to}.\n"
            f"   Run /restore {symbol_from} {symbol_to} QTY with an explicit qty."
        )
        return

    pos_from = await _find_position(broker, symbol_from)
    if not pos_from:
        print(f"❌ No open position for {symbol_from}")
        return

    sell_qty = float(pos_from.quantity)

    print(f"⏳ Previewing SELL {sell_qty:.0f} {symbol_from} (all)…")
    try:
        preview_sell, confirm_sell = await broker.preview_order(
            symbol_from, sell_qty, OrderSide.SELL, OrderType.MARKET
        )
    except Exception as e:
        print(f"❌ {e}")
        return

    print_order_preview("SELL", symbol_from, sell_qty, preview_sell)

    est_proceeds = float(preview_sell.get("est_volume") or 0)
    if not est_proceeds:
        bid_val      = float(preview_sell.get("bid") or pos_from.average_price or 0)
        est_proceeds = sell_qty * bid_val

    try:
        quote_to = await broker.get_quote(symbol_to)
        ask_to   = float(quote_to.ask)
        if not ask_to:
            raise RuntimeError(f"No ask for {symbol_to}")
    except Exception as e:
        print(f"❌ Could not get quote for {symbol_to}: {e}")
        return

    cost_to_restore = restore_qty * ask_to
    shortfall       = cost_to_restore - est_proceeds

    print(f"\n  Restore plan:  buy back {restore_qty:.0f} × {symbol_to} @ ≈€{ask_to:.4f}")
    print(f"  Estimated cost: €{cost_to_restore:.2f}  |  proceeds: ≈€{est_proceeds:.2f}")

    if shortfall <= 0:
        surplus = -shortfall
        surplus_qty = int(surplus / ask_to) if ask_to else 0
        print(f"  ✅ Proceeds cover restoration  (surplus ≈€{surplus:.2f}", end="")
        print(f" = {surplus_qty} extra {symbol_to} shares)" if surplus_qty else ")")
    else:
        affordable = max(1, int(est_proceeds / ask_to))
        print(f"  ⚠️  Shortfall ≈€{shortfall:.2f} — can only buy {affordable:.0f} of {restore_qty:.0f} {symbol_to}")
        if input(f"  Buy {affordable} instead of {restore_qty:.0f}? [y/N] › ").strip().lower() != "y":
            print(f"  Restore cancelled. Add ≈€{shortfall:.2f} cash or adjust qty.")
            return
        restore_qty = affordable

    print()
    if input(
        f"  Confirm: SELL {sell_qty:.0f} × {symbol_from}  then BUY {restore_qty:.0f} × {symbol_to}? [y/N] › "
    ).strip().lower() != "y":
        print("❌ Restore cancelled.")
        return

    try:
        sell_order = await broker.submit_order(
            symbol_from, sell_qty, OrderSide.SELL, confirm_sell, preview_sell
        )
    except Exception as e:
        print(f"❌ SELL failed: {e}")
        return

    fill_qty   = float(sell_order.quantity)
    fill_price = float(sell_order.average_fill_price or 0)
    proceeds   = fill_qty * fill_price
    print(f"✅ SELL filled — {fill_qty:.0f} × {symbol_from} @ €{fill_price:.4f}  proceeds €{proceeds:.2f}")

    actual_buy = min(int(restore_qty), max(1, int(proceeds / ask_to)))
    print(f"⏳ Previewing BUY {actual_buy} × {symbol_to}…")
    try:
        preview_buy, confirm_buy = await broker.preview_order(
            symbol_to, actual_buy, OrderSide.BUY, OrderType.MARKET
        )
    except Exception as e:
        print(f"❌ BUY preview failed: {e}")
        print(f"  ⚠️  Proceeds €{proceeds:.2f} in cash — deploy manually with /buy {symbol_to} e{proceeds:.0f}")
        return

    print_order_preview("BUY", symbol_to, actual_buy, preview_buy)

    if input(f"  Confirm BUY {actual_buy} × {symbol_to}? [y/N] › ").strip().lower() != "y":
        print(f"  ⚠️  BUY cancelled. Proceeds €{proceeds:.2f} in cash.")
        return

    try:
        buy_order = await broker.submit_order(
            symbol_to, actual_buy, OrderSide.BUY, confirm_buy, preview_buy
        )
        buy_price = float(buy_order.average_fill_price or 0)
        leftover  = proceeds - actual_buy * buy_price
        print(f"✅ BUY filled — {actual_buy} × {symbol_to} @ €{buy_price:.4f}")
        if leftover > 0.5:
            print(f"   Leftover cash: €{leftover:.2f}")
        short = int(restore_qty) - actual_buy
        print(f"\n  Rotation complete:  {symbol_from} → {symbol_to} ({actual_buy} shares restored)")
        if short > 0:
            print(f"  ⚠️  {short} share(s) short of original target ({int(restore_qty)} requested)")
        _remove_rotation(symbol_from)
    except Exception as e:
        print(f"❌ BUY failed: {e}")
        print(f"  ⚠️  Proceeds €{proceeds:.2f} in cash — deploy manually with /buy {symbol_to} e{proceeds:.0f}")


async def cmd_rotations(args: list) -> None:
    """
    /rotations   — list all open rotation records
    """
    state = _load_rotations()
    if not state:
        print("  No open rotations.")
        return
    bar = "─" * 52
    print(f"\n  {bar}")
    print(f"  {'Temp':>8}  {'Restore':>8}  {'Qty':>6}  {'Sell@':>8}  Opened")
    print(f"  {bar}")
    for temp_sym, rec in state.items():
        print(
            f"  {temp_sym:>8}  {rec['restore_symbol']:>8}  "
            f"{rec['restore_qty']:>6.0f}  "
            f"€{rec['sell_price']:>7.4f}  {rec.get('opened_at', '?')}"
        )
        print(f"     → /restore {temp_sym} {rec['restore_symbol']} {rec['restore_qty']:.0f}")
    print(f"  {bar}\n")


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


async def cmd_rotate_suggest(broker, args: list) -> None:
    """
    /rsuggest [TF]

    Applies the Momentum Rotation rules (MomentumRotation.md) to the current
    portfolio and recommends FROM → TO pairs with suggested /rotate commands.

    TF: 1m 5m 15m 1h 4h 1d (default 1d)
    """
    from services.command_service import CommandService
    from interfaces.console.formatters import tty_colors

    tf = args[0].lower() if args else "1d"
    print(f"⏳ Running momentum rotation scan ({tf} +ext)…")

    try:
        svc = CommandService(broker)
        pr  = await svc.portfolio_indicators(tf=tf, extended=True)
    except Exception as e:
        print(f"❌ {e}")
        return

    if not pr.rows:
        print("📭 No open positions with indicator data.")
        return

    # Portfolio median ATR% — threshold separating high/low volatility names
    atr_vals = sorted(
        r.indicators.atr_pct for r in pr.rows if r.indicators.atr_pct is not None
    )
    if not atr_vals:
        print("❌ No ATR% data available.")
        return
    n          = len(atr_vals)
    median_atr = (atr_vals[n // 2] + atr_vals[(n - 1) // 2]) / 2

    def _st_pct(ind):
        if ind.st_value and ind.close:
            return (ind.close - ind.st_value) / ind.st_value * 100
        return None

    def _ema_flag(ind):
        if ind.ema8 is not None and ind.ema20 is not None:
            return "T" if ind.ema8 > ind.ema20 else "↓"
        return "—"

    # ── FROM candidates (Section 3 of MomentumRotation.md) ───────────────────
    # ST=▼, or ST=▲ but barely holding (ST%<1%)
    # ATR% below median, ADX<25, RSI 35–55
    from_candidates = []
    for row in pr.rows:
        ind = row.indicators
        if any(v is None for v in (ind.adx, ind.rsi, ind.atr_pct, ind.st_dir)):
            continue
        sp    = _st_pct(ind)
        st_ok = ind.st_dir == -1 or (ind.st_dir == 1 and sp is not None and sp < 1.0)
        if not st_ok:
            continue
        if ind.atr_pct >= median_atr:
            continue
        if ind.adx >= 25:
            continue
        if not (35 <= ind.rsi <= 55):
            continue
        from_candidates.append(row)

    # Rank FROM: ADX ascending — weakest trend first, safest to vacate
    from_candidates.sort(key=lambda r: r.indicators.adx or 0)

    # ── TO candidates (Section 4 of MomentumRotation.md) ─────────────────────
    # ST=▲, EMA8>EMA20 (fresh cross T), ADX>20, RSI 45–65, ATR%>=median
    to_candidates = []
    for row in pr.rows:
        ind = row.indicators
        if any(v is None for v in (ind.adx, ind.rsi, ind.atr_pct, ind.st_dir, ind.ema8, ind.ema20)):
            continue
        if ind.st_dir != 1:
            continue
        if ind.ema8 <= ind.ema20:          # flag ↓ — EMA cross not confirmed
            continue
        if ind.adx <= 20:
            continue
        if not (45 <= ind.rsi <= 65):
            continue
        if ind.atr_pct < median_atr:
            continue
        to_candidates.append(row)

    # Rank TO: ADX descending then ST% ascending — strong trend, not overextended
    to_candidates.sort(key=lambda r: (-(r.indicators.adx or 0), _st_pct(r.indicators) or 999))

    # ── Correlation check — don't rotate into a name that just duplicates
    # exposure already sitting elsewhere in the book (60d daily-return corr).
    _CORR_FLAG = 0.7
    corr_matrix = await svc.portfolio_correlations(pr.rows)

    def _max_corr_vs_book(ticker: str) -> tuple[str, float] | None:
        others = {t: c for t, c in corr_matrix.get(ticker, {}).items() if t != ticker}
        return max(others.items(), key=lambda kv: abs(kv[1])) if others else None

    def _flagged(row) -> bool:
        worst = _max_corr_vs_book(row.ticker)
        return worst is not None and abs(worst[1]) > _CORR_FLAG

    # Keep ADX/ST% ranking within each bucket — just push duplicated exposure last.
    to_candidates = [r for r in to_candidates if not _flagged(r)] + [r for r in to_candidates if _flagged(r)]

    GREEN, RED, RESET = tty_colors()

    print(f"\n{'═'*62}")
    print(f"  Momentum Rotation Scan  [{tf}]   portfolio median ATR%: {median_atr:.2f}%")
    print(f"{'═'*62}")

    # ── FROM table ────────────────────────────────────────────────────────────
    print(f"\n  FROM candidates  (weak / stalled — safe to vacate capital):")
    print(f"  {'─'*58}")
    print(f"  {'Ticker':<7}  {'ST':>2}  {'ST%':>6}  {'ADX':>4}  {'RSI':>4}  {'ATR%':>5}  EMA")
    print(f"  {'─'*58}")
    if not from_candidates:
        print("  (none — no portfolio names currently meet all FROM criteria)")
    for row in from_candidates[:4]:
        ind    = row.indicators
        sp     = _st_pct(ind)
        col    = GREEN if ind.st_dir == 1 else RED
        st_lbl = "▲" if ind.st_dir == 1 else "▼"
        sp_str = f"{sp:+.1f}%" if sp is not None else "—"
        print(
            f"  {row.ticker:<7}  {col}{st_lbl}{RESET}   {sp_str:>6}"
            f"  {ind.adx or 0:>4.0f}  {ind.rsi or 0:>4.0f}  {ind.atr_pct or 0:>5.2f}%  {_ema_flag(ind)}"
        )

    # ── TO table ──────────────────────────────────────────────────────────────
    print(f"\n  TO candidates  (confirmed momentum — capital destination):")
    print(f"  {'─'*66}")
    print(f"  {'Ticker':<7}  {'ST':>2}  {'ST%':>6}  {'ADX':>4}  {'RSI':>4}  {'ATR%':>5}  EMA  {'Sug TP':>7}  {'SL':>6}")
    print(f"  {'─'*66}")
    if not to_candidates:
        print("  (none — no portfolio names currently meet all TO criteria)")
    to_shown = to_candidates[:4]
    for row in to_shown:
        ind    = row.indicators
        sp     = _st_pct(ind)
        sp_str = f"{sp:+.1f}%" if sp is not None else "—"
        # TP: ~8× daily ATR% (doc examples: ATR 0.93%→+8%, ATR 1.09%→+10%)
        tp_pct = max(2, round((ind.atr_pct or 2) * 8))
        sl_pct = round(ind.atr_pct or 1, 1)
        print(
            f"  {row.ticker:<7}  {GREEN}▲{RESET}   {sp_str:>6}"
            f"  {ind.adx or 0:>4.0f}  {ind.rsi or 0:>4.0f}  {ind.atr_pct or 0:>5.2f}%"
            f"  {_ema_flag(ind)}  +{tp_pct:>4}%   -{sl_pct:.1f}%"
        )
        worst = _max_corr_vs_book(row.ticker)
        if worst is not None and abs(worst[1]) > _CORR_FLAG:
            print(f"     ⚠️  {worst[1]:+.2f} corr vs {worst[0]} — largely duplicates existing exposure")

    # ── Suggested /rotate commands ────────────────────────────────────────────
    if from_candidates and to_shown:
        print(f"\n  {'─'*58}")
        print(f"  Suggested rotations  (25–50% of FROM; /restore after TP hits):")
        print(f"  {'─'*58}")
        for fr, to in zip(from_candidates[:2], to_shown[:2]):
            atr_to = to.indicators.atr_pct or 2.0
            tp_pct = max(2, round(atr_to * 8))
            sl_pct = round(atr_to, 1)
            tp_price = round(to.indicators.close * (1 + tp_pct / 100), 2) if to.indicators.close else None
            sl_price = round(to.indicators.close * (1 - sl_pct / 100), 2) if to.indicators.close else None
            print(f"  /rotate {fr.ticker} 50% {to.ticker} +{tp_pct}")
            print(
                f"     ({fr.ticker}: ADX {fr.indicators.adx:.0f}, "
                f"ATR {fr.indicators.atr_pct:.2f}%, RSI {fr.indicators.rsi:.0f}"
                f"  →  {to.ticker}: ADX {to.indicators.adx:.0f}, "
                f"ATR {atr_to:.2f}%, RSI {to.indicators.rsi:.0f}, EMA T)"
            )
            if tp_price and sl_price:
                print(
                    f"     Entry ≈{to.indicators.close:.2f}  "
                    f"TP ≈{tp_price:.2f} (+{tp_pct}%)  "
                    f"SL ≈{sl_price:.2f} (-{sl_pct:.1f}%)"
                )
            pair_corr = corr_matrix.get(fr.ticker, {}).get(to.ticker)
            if pair_corr is not None and abs(pair_corr) > _CORR_FLAG:
                print(f"     ⚠️  {pair_corr:+.2f} corr between {fr.ticker} and {to.ticker} — thin diversification benefit")
            print()

    # ── Guardrails ────────────────────────────────────────────────────────────
    print(f"  {'─'*58}")
    print(f"  Guardrails:")
    violations = []
    for row in from_candidates[:4]:
        ind = row.indicators
        if ind.adx and ind.adx > 30:
            violations.append(f"  ⚠️  {row.ticker}: ADX {ind.adx:.0f} > 30 — strong trend, unsafe to vacate")
        if ind.rsi and ind.rsi < 30:
            violations.append(f"  ⚠️  {row.ticker}: RSI {ind.rsi:.0f} < 30 — oversold, bounce risk on exit")
    for row in to_shown:
        ind = row.indicators
        if ind.rsi and ind.rsi > 70:
            violations.append(f"  ⚠️  {row.ticker}: RSI {ind.rsi:.0f} > 70 — overbought, do not use as TO")
    if violations:
        for v in violations:
            print(v)
    else:
        print(f"  ✅ No guardrail violations in top candidates")

    # ── Open rotations warning ────────────────────────────────────────────────
    open_rots = _load_rotations()
    if open_rots:
        print(f"\n  ⚠️  {len(open_rots)} open rotation(s) already active  (max 2–3 concurrent):")
        for sym, rec in open_rots.items():
            print(f"     {sym} → {rec['restore_symbol']}  opened {rec.get('opened_at', '?')}")
        if len(open_rots) >= 3:
            print(f"  🛑 At/above max — resolve existing rotations before opening new ones")

    print()

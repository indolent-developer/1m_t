"""
services.pnl_service

Shared fill-history and P&L helpers used by both the CLI and Telegram interfaces.
"""
from __future__ import annotations

import datetime as _dt


async def get_fills(broker, ticker: str) -> list:
    """Return all FILLED orders for ticker, sorted oldest-first."""
    from core.entities.broker_entities import OrderStatus

    isin = None
    _resolve = getattr(broker, "_resolve_isin", None)
    if _resolve:
        try:
            isin = (await _resolve(ticker)).upper()
        except Exception:
            pass

    all_orders = await broker.get_orders()
    fills = []
    for o in all_orders:
        if o.status != OrderStatus.FILLED:
            continue
        raw        = o.broker_specific_data or {}
        order_isin = raw.get("isin", "").upper()
        if isin and order_isin:
            if order_isin == isin:
                fills.append(o)
        elif ticker.upper() in (o.symbol or "").upper():
            fills.append(o)

    fills.sort(key=lambda o: o.filled_timestamp or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc))
    return fills


def calc_pnl(fills: list, position_size: float, current_bid: float = 0.0) -> dict:
    """
    Isolate the current round-trip batch and compute P&L.

    Walk fills newest→oldest, accumulating until (buys − sells) == position_size.
    That batch represents the current trade.  Then:

        avg_buy    = total_buy_notional  / total_buy_qty
        avg_sell   = total_sell_notional / total_sell_qty
        realized   = (avg_sell − avg_buy) × matched_sell_qty
        unrealized = (current_bid − avg_buy) × position_size
    """
    from core.entities.broker_entities import OrderSide

    target = float(position_size)
    net    = 0.0
    batch  = []

    for o in reversed(fills):          # newest → oldest
        qty = float(o.quantity)
        net += qty if o.side == OrderSide.BUY else -qty
        batch.insert(0, o)             # keep oldest-first display order
        if abs(net - target) < 0.001:  # exact match
            break
        if net >= target - 0.001 and target >= 0:
            break                      # overshot — include and stop

    buy_qty = buy_notional = 0.0
    sell_qty = sell_notional = 0.0
    rows = []

    for o in batch:
        qty   = float(o.quantity)
        price = float(o.average_fill_price)
        if o.side == OrderSide.BUY:
            buy_qty      += qty
            buy_notional += qty * price
            rows.append({"ts": o.filled_timestamp, "side": "BUY",  "qty": qty, "price": price})
        else:
            sell_qty      += qty
            sell_notional += qty * price
            rows.append({"ts": o.filled_timestamp, "side": "SELL", "qty": qty, "price": price})

    avg_buy  = buy_notional  / buy_qty  if buy_qty  > 0 else 0.0
    avg_sell = sell_notional / sell_qty if sell_qty > 0 else 0.0

    matched    = min(buy_qty, sell_qty)
    realized   = (avg_sell - avg_buy) * matched if matched > 0 and avg_buy > 0 else 0.0
    unrealized = (current_bid - avg_buy) * target if target > 0 and avg_buy > 0 and current_bid > 0 else 0.0

    return {
        "rows":          rows,
        "buy_qty":       buy_qty,      "avg_buy":  avg_buy,
        "sell_qty":      sell_qty,     "avg_sell": avg_sell,
        "realized":      realized,     "unrealized": unrealized,
        "total":         realized + unrealized,
        "position_size": target,       "current_bid": current_bid,
    }

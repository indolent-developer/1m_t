"""
services.trade_service

Pure trade-sizing and order-type logic — no I/O, no broker calls.
"""
from __future__ import annotations

TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}


def parse_trigger(token: str) -> dict:
    """
    Parse @TRIGGER token.
    Returns {"kind": "price", "usd": float}
          | {"kind": "atr",   "mult": float, "tf": str}
    """
    val = token[1:]  # strip leading @
    if val.lower().startswith("atr"):
        atr_part = val[3:]
        if ":" in atr_part:
            mult_s, tf = atr_part.split(":", 1)
        else:
            mult_s, tf = atr_part, "1m"
        return {"kind": "atr", "mult": float(mult_s), "tf": tf.lower()}
    return {"kind": "price", "usd": float(val)}


def calc_quantity(
    size_token: str,
    side,
    pos_qty: float,
    sizing_price_eur: float,
    usd_rate: float | None,
) -> tuple[int, str]:
    """
    Resolve order quantity from a SIZE token.

    Returns (qty, human-readable description).
    Raises ValueError with a user-readable message on bad input.
    SIZE formats: N shares | N% | all | eN euros | $N USD
    """
    st = size_token.lower()

    if st == "all":
        qty = int(pos_qty)
        return qty, f"all = {qty} shares"

    if st.endswith("%"):
        pct = float(st[:-1])
        qty = round(pos_qty * pct / 100)
        if qty < 1:
            raise ValueError(f"{pct}% of {pos_qty} = {qty} shares — too small")
        return qty, f"{pct}% of {pos_qty:.0f} shares = {qty} shares"

    if st.startswith("e"):
        eur_amount = float(st[1:])
        if not sizing_price_eur:
            raise ValueError("Cannot size: no bid/ask available")
        qty = max(1, int(eur_amount / sizing_price_eur))
        return qty, f"€{eur_amount:.2f} / €{sizing_price_eur:.4f} = {qty} shares"

    if st.startswith("$"):
        if not usd_rate:
            raise ValueError("Cannot size: no USD/EUR rate available")
        usd_amount = float(st[1:])
        eur_amount = usd_amount * usd_rate
        if not sizing_price_eur:
            raise ValueError("Cannot size: no bid/ask available")
        qty = max(1, int(eur_amount / sizing_price_eur))
        return qty, f"${usd_amount:.2f} → €{eur_amount:.2f} / €{sizing_price_eur:.4f} = {qty} shares"

    qty = int(float(size_token))
    if qty < 1:
        raise ValueError(f"Invalid size: {size_token}")
    return qty, ""


def infer_order_type(side, trigger_eur: float, current_eur: float, forced_type: str | None = None):
    """
    Infer STOP vs LIMIT from direction and trigger vs current price.
    forced_type: 'stop' | 'limit' | None
    Returns OrderType.
    """
    from core.entities.broker_entities import OrderSide, OrderType

    if forced_type == "stop":
        return OrderType.STOP
    if forced_type == "limit":
        return OrderType.LIMIT

    if side == OrderSide.BUY:
        return OrderType.STOP if trigger_eur >= current_eur else OrderType.LIMIT
    return OrderType.STOP if trigger_eur <= current_eur else OrderType.LIMIT


def calc_position_size_by_risk(
    entry_eur: float,
    stop_usd: float,
    risk_eur: float,
    usd_rate: float,
) -> dict:
    """
    Calculate shares to buy for a given risk budget.

    Returns: entry_usd, stop_eur, risk_per_share_usd, risk_per_share_eur, qty, notional
    Raises ValueError if stop is above entry.
    """
    entry_usd          = entry_eur / usd_rate
    risk_per_share_usd = entry_usd - stop_usd
    if risk_per_share_usd <= 0:
        raise ValueError(f"Stop ${stop_usd:.4f} is above entry ${entry_usd:.4f}")
    risk_per_share_eur = risk_per_share_usd * usd_rate
    qty      = int(risk_eur / risk_per_share_eur)
    return {
        "entry_usd":          entry_usd,
        "stop_eur":           round(stop_usd * usd_rate, 4),
        "risk_per_share_usd": risk_per_share_usd,
        "risk_per_share_eur": risk_per_share_eur,
        "qty":                qty,
        "notional":           qty * entry_eur,
    }

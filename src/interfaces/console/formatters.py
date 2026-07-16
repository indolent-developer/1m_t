"""
interfaces.console.formatters

Terminal display helpers for order previews and colour output.
"""
from __future__ import annotations

import sys


def tty_colors() -> tuple[str, str, str]:
    """Return (GREEN, RED, RESET); empty strings when stdout is not a tty."""
    _tty = sys.stdout.isatty()
    return (
        "\033[32m" if _tty else "",
        "\033[31m" if _tty else "",
        "\033[0m"  if _tty else "",
    )


def print_order_preview(side: str, symbol: str, qty: float, preview: dict) -> None:
    cur       = preview.get("currency", "EUR")
    inst_name = preview.get("_instrument_name", "")
    print(
        f"\n  {'─'*44}\n"
        f"  {'ORDER PREVIEW':^44}\n"
        f"  {'─'*44}\n"
        f"  Side:        {side} {qty} × {symbol}\n"
        f"  Instrument:  {inst_name or '—'}\n"
        f"  ISIN:        {preview.get('isin', '—')}\n"
        f"  Shares:      {preview.get('shares', '—')}\n"
        f"  Bid / Ask:   {preview.get('bid', '—')} / {preview.get('ask', '—')} {cur}\n"
        f"  Est. volume: {preview.get('est_volume', '—')} {cur}\n"
        f"  Entry fee:   {preview.get('fee_entry', '—')} {cur}\n"
        f"  Venue:       {preview.get('venue', '—')}\n"
        f"  {'─'*44}"
    )


def print_triggered_preview(
    type_label: str, side: str, symbol: str, qty: int,
    trigger_usd: float, trigger_eur: float, rate: float | None, preview: dict,
) -> None:
    cur       = preview.get("currency", "EUR")
    side_up   = side.upper()
    inst_name = preview.get("_instrument_name", "")
    price_key = "ask" if side_up == "BUY" else "bid"
    fee_key   = "entry" if side_up == "BUY" else "exit"
    fee_val   = (preview.get("ex_ante_costs") or {}).get(fee_key, preview.get("fee_entry", "—"))
    if rate is not None:
        trigger_line = f"  Trigger:     ${trigger_usd:.4f}  →  €{trigger_eur:.4f}  (rate {rate:.4f})"
    else:
        trigger_line = f"  Trigger:     €{trigger_eur:.4f}"

    current_price_raw = preview.get(price_key)
    try:
        cp = float(current_price_raw)
        diff = trigger_eur - cp
        diff_pct = diff / cp * 100
        arrow = "↑" if diff >= 0 else "↓"
        dist_tag = f"  {arrow} {diff:+.2f} {cur} ({diff_pct:+.1f}%)"
    except (TypeError, ValueError, ZeroDivisionError):
        dist_tag = ""

    print(
        f"\n  {'─'*44}\n"
        f"  {f'{type_label}-{side_up} ORDER PREVIEW':^44}\n"
        f"  {'─'*44}\n"
        f"  Side:        {type_label}-{side_up} {qty} × {symbol}\n"
        f"  Instrument:  {inst_name or '—'}\n"
        f"  ISIN:        {preview.get('isin', '—')}\n"
        f"{trigger_line}\n"
        f"  Current {price_key}:  {current_price_raw} {cur}{dist_tag}\n"
        f"  Venue:       {preview.get('venue', '—')}\n"
        f"  Fee:         {fee_val} {cur}\n"
        f"  {'─'*44}"
    )

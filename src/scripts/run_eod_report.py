"""
scripts.run_eod_report

End-of-day portfolio report across all configured brokers.

Connects to each broker concurrently, fetches account + positions,
prints a consolidated report to the console, and optionally sends
it to the Telegram chat configured in .env.

Usage:
    PYTHONPATH=src python src/scripts/run_eod_report.py
    PYTHONPATH=src python src/scripts/run_eod_report.py --telegram
    ./run_scripts/run_eod_report.sh
    ./run_scripts/run_eod_report.sh --telegram
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import random
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# ── Path bootstrap ────────────────────────────────────────────────────────────
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_ROOT = Path(__file__).resolve().parents[2]
_ENV  = _ROOT / ".env"
if _ENV.exists():
    try:
        from dotenv import dotenv_values
        for k, v in dotenv_values(_ENV).items():
            os.environ.setdefault(k, v or "")
    except ImportError:
        for line in _ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ── Broker result container ───────────────────────────────────────────────────

@dataclass
class BrokerSnapshot:
    name:       str
    account:    object          # AccountInfo | None
    positions:  list            # List[Position]
    error:      Optional[str]   = None

    @property
    def ok(self) -> bool:
        return self.account is not None and self.error is None


# ── Per-broker connect + fetch ────────────────────────────────────────────────

def _make_broker(name: str):
    from interfaces.console.local_cli import _make_broker as _cli_make_broker
    broker = _cli_make_broker(name, is_demo=False)
    if name == "ibkr" and hasattr(broker, "config"):
        broker.config.client_id_broker = random.randint(100, 9999)
    return broker


async def _fetch_broker(display_name: str, broker_name: str) -> BrokerSnapshot:
    try:
        broker = _make_broker(broker_name)
    except Exception as e:
        return BrokerSnapshot(display_name, None, [], str(e))
    try:
        if broker_name == "ibkr":
            ok = await asyncio.wait_for(broker.connect(), timeout=8.0)
        else:
            ok = await broker.connect()
        if not ok:
            err = getattr(broker, "connect_error", None) or "connect() returned False"
            return BrokerSnapshot(display_name, None, [], err)
        account   = await broker.get_account_info()
        positions = await broker.get_positions()
        return BrokerSnapshot(display_name, account, positions)
    except asyncio.TimeoutError:
        port = getattr(getattr(broker, "config", None), "port", 4001)
        return BrokerSnapshot(display_name, None, [], f"timeout — TWS/Gateway not reachable on port {port}")
    except Exception as e:
        return BrokerSnapshot(display_name, None, [], str(e))
    finally:
        try:
            await broker.disconnect()
        except Exception:
            pass


async def _fetch_capital()  -> BrokerSnapshot: return await _fetch_broker("Capital.com", "capital")
async def _fetch_scalable() -> BrokerSnapshot: return await _fetch_broker("Scalable",    "scalable")
async def _fetch_etoro()    -> BrokerSnapshot: return await _fetch_broker("eToro",        "etoro")
async def _fetch_ibkr()     -> BrokerSnapshot: return await _fetch_broker("IBKR",         "ibkr")


# ── Formatting ────────────────────────────────────────────────────────────────

_W = 64   # total line width

def _hr(char: str = "─") -> str:
    return "  " + char * (_W - 2)

def _pnl_str(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:,.2f}"

def _pct_str(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def format_report(snapshots: List[BrokerSnapshot], generated_at: dt.datetime) -> str:
    lines: List[str] = []
    ts = generated_at.strftime("%Y-%m-%d  %H:%M:%S")

    lines.append("")
    lines.append(_hr("═"))
    lines.append(f"  {'EOD PORTFOLIO REPORT':^{_W - 2}}")
    lines.append(f"  {ts:^{_W - 2}}")
    lines.append(_hr("═"))

    total_value   = 0.0
    total_cash    = 0.0
    total_pnl     = 0.0
    position_rows = []

    for snap in snapshots:
        lines.append("")
        lines.append(f"  {'▸ ' + snap.name}")
        lines.append(_hr())

        if not snap.ok:
            lines.append(f"  ✗  {snap.error or 'unavailable'}")
            continue

        acc = snap.account
        cur = acc.currency

        lines.append(f"  Account:   {acc.account_id}")
        lines.append(f"  Value:     {acc.current_value:>12,.2f} {cur}")
        lines.append(f"  Cash:      {acc.cash_in_hand:>12,.2f} {cur}")
        if acc.margin_used:
            lines.append(f"  Margin:    {acc.margin_used:>12,.2f} used  /  {acc.margin_available:,.2f} free")
        if acc.leverage and acc.leverage != 1.0:
            lines.append(f"  Leverage:  {acc.leverage}x")

        total_value += acc.current_value
        total_cash  += acc.cash_in_hand

        if snap.positions:
            lines.append(f"  Positions ({len(snap.positions)}):")
            for p in snap.positions:
                pnl     = getattr(p, "unrealized_pnl", 0) or 0
                pnl_pct = getattr(p, "unrealized_pnl_percentage", 0) or 0
                side    = getattr(p.side, "value", str(p.side)) if hasattr(p, "side") and p.side else "—"
                lines.append(
                    f"    {p.symbol:<10}  {side.upper():<4}  "
                    f"qty={p.quantity:>8.2f}  "
                    f"avg={p.average_price:>10.4f}  "
                    f"val={p.market_value:>10,.2f}  "
                    f"pnl={_pnl_str(pnl):>10}  {_pct_str(pnl_pct)}"
                )
                total_pnl += pnl
                position_rows.append((snap.name, p))
        else:
            lines.append("  Positions: none")

    # ── Totals ────────────────────────────────────────────────────────────────
    ok_snaps = [s for s in snapshots if s.ok]
    if ok_snaps:
        lines.append("")
        lines.append(_hr("═"))
        lines.append(f"  {'TOTAL ACROSS ALL BROKERS':^{_W - 2}}")
        lines.append(_hr("─"))
        lines.append(f"  Portfolio value:  {total_value:>12,.2f}")
        lines.append(f"  Cash:             {total_cash:>12,.2f}")
        lines.append(f"  Open P&L:         {_pnl_str(total_pnl):>12}")
        lines.append(f"  Open positions:   {len(position_rows):>12}")
        lines.append(_hr("═"))

    lines.append("")
    return "\n".join(lines)


def format_telegram(snapshots: List[BrokerSnapshot], generated_at: dt.datetime) -> str:
    ts = generated_at.strftime("%Y\\-%m\\-%d %H:%M:%S")
    lines = [f"*📊 EOD Report* — `{ts}`\n"]

    total_value = 0.0
    total_pnl   = 0.0
    n_positions = 0

    for snap in snapshots:
        if not snap.ok:
            lines.append(f"*{snap.name}* — ✗ _{snap.error}_\n")
            continue

        acc     = snap.account
        cur     = acc.currency
        n_pos   = len(snap.positions)
        pos_pnl = sum((getattr(p, "unrealized_pnl", 0) or 0) for p in snap.positions)
        sign    = "🟢" if pos_pnl >= 0 else "🔴"

        total_value += acc.current_value
        total_pnl   += pos_pnl
        n_positions += n_pos

        lines.append(
            f"*{snap.name}*\n"
            f"  Value: `{acc.current_value:,.2f} {cur}`\n"
            f"  Cash:  `{acc.cash_in_hand:,.2f} {cur}`\n"
            f"  Pos:   {n_pos}  {sign} P&L `{_pnl_str(pos_pnl)}`\n"
        )

    if any(s.ok for s in snapshots):
        total_sign = "🟢" if total_pnl >= 0 else "🔴"
        lines.append(
            f"*TOTAL*\n"
            f"  Portfolio: `{total_value:,.2f}`\n"
            f"  Open P&L:  {total_sign} `{_pnl_str(total_pnl)}`\n"
            f"  Positions: `{n_positions}`"
        )

    return "\n".join(lines)


async def _send_telegram(text: str) -> None:
    token   = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("  ⚠  TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set — skipping Telegram send")
        return

    import httpx
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    body = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=body, timeout=15.0)
        if resp.status_code == 200:
            print("  ✓  Sent to Telegram")
        else:
            print(f"  ✗  Telegram error {resp.status_code}: {resp.text[:200]}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    send_telegram = "--telegram" in sys.argv

    loop = asyncio.get_running_loop()

    def _handle_shutdown():
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_shutdown)

    print("Fetching data from all brokers…")

    snapshots = await asyncio.gather(
        _fetch_capital(),
        _fetch_scalable(),
        _fetch_etoro(),
        _fetch_ibkr(),
        return_exceptions=True,
    )

    now    = dt.datetime.now()
    report = format_report(
        [s for s in snapshots if isinstance(s, BrokerSnapshot)], now
    )
    print(report)

    if send_telegram:
        tg_text = format_telegram(
            [s for s in snapshots if isinstance(s, BrokerSnapshot)], now
        )
        print("Sending to Telegram…")
        await _send_telegram(tg_text)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

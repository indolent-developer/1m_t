"""
scripts.run_snapshot

Instant account snapshot across all configured brokers.
Fetches positions, qty, last price, unrealised P&L and saves a
timestamped plain-text file to data/snapshots/.

Usage:
    ./run_scripts/run_snapshot.sh
    PYTHONPATH=src python src/scripts/run_snapshot.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# ── Path bootstrap ────────────────────────────────────────────────────────────
_SRC  = Path(__file__).resolve().parents[2] / "src"
_ROOT = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_ENV = _ROOT / ".env"
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

_OUT_DIR = _ROOT / "data" / "snapshots"
_OUT_DIR.mkdir(parents=True, exist_ok=True)

_W = 68


@dataclass
class _Snap:
    name:         str
    account:      object           # AccountInfo or None
    positions:    list
    error:        Optional[str] = None
    acct_error:   Optional[str] = None   # set when account fetch failed but positions succeeded

    @property
    def ok(self) -> bool:
        return self.error is None and (self.account is not None or self.positions)


def _hr(char: str = "─") -> str:
    return "  " + char * (_W - 2)


def _pnl(v: float) -> str:
    return f"{'+'if v>=0 else ''}{v:,.2f}"


def _pct(v: float) -> str:
    return f"{'+'if v>=0 else ''}{v:.2f}%"


# ── Broker fetch ──────────────────────────────────────────────────────────────

def _make_broker(name: str):
    from interfaces.console.local_cli import _make_broker as _cli_make
    broker = _cli_make(name, is_demo=False)
    if name == "ibkr" and hasattr(broker, "config"):
        broker.config.client_id_broker = random.randint(100, 9999)
    return broker


async def _fetch(display: str, name: str) -> _Snap:
    try:
        broker = _make_broker(name)
    except Exception as e:
        return _Snap(display, None, [], str(e))
    try:
        if name == "ibkr":
            ok = await asyncio.wait_for(broker.connect(), timeout=8.0)
        else:
            ok = await broker.connect()
        if not ok:
            err = getattr(broker, "connect_error", None) or "connect() returned False"
            return _Snap(display, None, [], err)

        account    = None
        acct_error = None
        try:
            account = await broker.get_account_info()
        except Exception as e:
            acct_error = str(e)

        positions = await broker.get_positions()
        return _Snap(display, account, positions, acct_error=acct_error)
    except asyncio.TimeoutError:
        port = getattr(getattr(broker, "config", None), "port", 4001)
        return _Snap(display, None, [], f"timeout — TWS not reachable on port {port}")
    except Exception as e:
        return _Snap(display, None, [], str(e))
    finally:
        try:
            await broker.disconnect()
        except Exception:
            pass


# ── Format ────────────────────────────────────────────────────────────────────

def _format(snapshots: List[_Snap], ts: dt.datetime) -> str:
    lines: List[str] = []
    ts_str = ts.strftime("%Y-%m-%d  %H:%M:%S")

    lines.append("")
    lines.append(_hr("═"))
    lines.append(f"  {'ACCOUNT SNAPSHOT':^{_W-2}}")
    lines.append(f"  {ts_str:^{_W-2}}")
    lines.append(_hr("═"))

    total_value     = 0.0
    total_cash      = 0.0
    total_pnl       = 0.0
    total_positions = 0

    for snap in snapshots:
        lines.append("")
        lines.append(f"  ▸ {snap.name}")
        lines.append(_hr())

        if not snap.ok:
            lines.append(f"  ✗  {snap.error or 'unavailable'}")
            continue

        if snap.account is not None:
            acc = snap.account
            cur = acc.currency
            lines.append(f"  Account :  {acc.account_id}")
            lines.append(f"  Value   :  {acc.current_value:>12,.2f} {cur}")
            lines.append(f"  Cash    :  {acc.cash_in_hand:>12,.2f} {cur}")
            if getattr(acc, "margin_used", None):
                lines.append(f"  Margin  :  {acc.margin_used:>12,.2f} used  /  {acc.margin_available:,.2f} free")
            total_value += acc.current_value
            total_cash  += acc.cash_in_hand
        elif snap.acct_error:
            short = snap.acct_error.split(".")[0]   # first sentence only
            lines.append(f"  ⚠  account info unavailable — {short}")

        if snap.positions:
            lines.append(f"  Positions ({len(snap.positions)}):")
            lines.append(
                f"    {'Symbol':<10}  {'Side':<4}  {'Qty':>10}  "
                f"{'Avg':>10}  {'Last':>10}  {'Mkt Val':>12}  "
                f"{'Unreal PnL':>12}  {'%PnL':>8}"
            )
            lines.append(
                f"    {'─'*10}  {'─'*4}  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*12}  {'─'*12}  {'─'*8}"
            )
            for p in snap.positions:
                pnl     = getattr(p, "unrealized_pnl", 0) or 0
                pnl_pct = getattr(p, "unrealized_pnl_percentage", 0) or 0
                mkt_val = getattr(p, "market_value", 0) or 0
                last    = (getattr(p, "current_price", None)
                           or getattr(p, "last_price", None)
                           or (mkt_val / p.quantity if p.quantity and mkt_val else None))
                side    = getattr(p.side, "value", str(p.side)) if getattr(p, "side", None) else "—"
                last_s  = f"{last:.4f}" if last else "—"
                lines.append(
                    f"    {p.symbol:<10}  {side.upper():<4}  {p.quantity:>10.2f}  "
                    f"{p.average_price:>10.4f}  {last_s:>10}  {mkt_val:>12,.2f}  "
                    f"{_pnl(pnl):>12}  {_pct(pnl_pct):>8}"
                )
                total_pnl      += pnl
                total_positions += 1
                if snap.account is None:
                    total_value += mkt_val
        else:
            lines.append("  Positions: none")

    lines.append("")
    lines.append(_hr("═"))
    lines.append(f"  {'TOTALS':^{_W-2}}")
    lines.append(_hr("─"))
    lines.append(f"  Portfolio value :  {total_value:>12,.2f}")
    lines.append(f"  Cash            :  {total_cash:>12,.2f}")
    lines.append(f"  Open P&L        :  {_pnl(total_pnl):>12}")
    lines.append(f"  Open positions  :  {total_positions:>12}")
    lines.append(_hr("═"))
    lines.append("")
    return "\n".join(lines)


# ── Entry ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("Fetching positions from all brokers…")

    results = await asyncio.gather(
        _fetch("Capital.com", "capital"),
        _fetch("Scalable",    "scalable"),
        _fetch("eToro",       "etoro"),
        _fetch("IBKR",        "ibkr"),
        return_exceptions=True,
    )

    snapshots = [r for r in results if isinstance(r, _Snap)]

    now    = dt.datetime.now()
    report = _format(snapshots, now)
    print(report)

    fname   = now.strftime("%Y-%m-%d_%H-%M-%S") + "_snapshot.txt"
    outfile = _OUT_DIR / fname
    outfile.write_text(report, encoding="utf-8")
    print(f"  Saved → {outfile.relative_to(_ROOT)}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

"""
interfaces.console.cmd_sym

/sym — view and manage the symbol / ISIN resolution caches.

Two layers:
  Master cache      data/isin_ticker_cache.json   ISIN → ticker
                    Auto-populated; manual entries act as permanent overrides.

  Broker overrides  ticker_isin.json (project root)
                    [broker_id]._isin_override: {isin: ticker}
                    Takes priority over master cache for that broker only.
                    Same file also holds execution overrides [broker_id][ticker] = isin.

Commands
--------
  /sym                       scan open positions → show ISIN + resolved ticker
  /sym cache                 list master cache (isin_ticker_cache.json)
  /sym add  ISIN TICKER      pin to master cache
  /sym rm   ISIN             remove from master cache

  /sym broker                list broker-level overrides for current broker
  /sym broker add ISIN TICKER   add broker-level ISIN→ticker override
  /sym broker rm  ISIN          remove broker-level ISIN→ticker override
  /sym broker exec              list execution overrides (ticker→ISIN)
  /sym broker exec add TICKER ISIN
  /sym broker exec rm  TICKER
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_DATA_DIR         = Path(__file__).resolve().parents[3] / "data"
_MASTER_CACHE     = _DATA_DIR / "isin_ticker_cache.json"
_TICKER_ISIN_PATH = _DATA_DIR / "ticker_isin.json"


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_master() -> dict:
    if _MASTER_CACHE.exists():
        try:
            return json.loads(_MASTER_CACHE.read_text())
        except Exception:
            pass
    return {}


def _save_master(data: dict) -> None:
    _MASTER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _MASTER_CACHE.write_text(json.dumps(data, indent=2, sort_keys=True))


def _load_ticker_isin() -> dict:
    if _TICKER_ISIN_PATH.exists():
        try:
            return json.loads(_TICKER_ISIN_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_ticker_isin(data: dict) -> None:
    _TICKER_ISIN_PATH.write_text(json.dumps(data, indent=2))


def get_broker_isin_override(broker_id: str, isin: str) -> str | None:
    """Return broker-specific ticker for ISIN, or None."""
    data = _load_ticker_isin()
    return data.get(broker_id, {}).get("_isin_override", {}).get(isin.upper())


# ── main command ──────────────────────────────────────────────────────────────

async def cmd_sym(broker, args: list) -> None:
    broker_id = getattr(broker, "broker_id", "unknown")

    if not args:
        await _cmd_scan(broker, broker_id)
        return

    sub = args[0].lower()

    if sub == "cache":
        _cmd_cache()
        return

    if sub == "add":
        if len(args) < 3:
            print("Usage: /sym add ISIN TICKER")
            return
        _cmd_add(args[1].upper(), args[2].upper())
        return

    if sub == "rm":
        if len(args) < 2:
            print("Usage: /sym rm ISIN")
            return
        _cmd_rm(args[1].upper())
        return

    if sub in ("resolve", "refresh"):
        await _cmd_resolve(broker, broker_id)
        return

    if sub == "broker":
        rest = args[1:]
        if not rest:
            _cmd_broker_list(broker_id)
        elif rest[0].lower() == "add" and len(rest) >= 3:
            _cmd_broker_add(broker_id, rest[1].upper(), rest[2].upper())
        elif rest[0].lower() == "rm" and len(rest) >= 2:
            _cmd_broker_rm(broker_id, rest[1].upper())
        elif rest[0].lower() == "exec":
            exec_rest = rest[1:]
            if not exec_rest:
                _cmd_broker_exec_list(broker_id)
            elif exec_rest[0].lower() == "add" and len(exec_rest) >= 3:
                _cmd_broker_exec_add(broker_id, exec_rest[1].upper(), exec_rest[2])
            elif exec_rest[0].lower() == "rm" and len(exec_rest) >= 2:
                _cmd_broker_exec_rm(broker_id, exec_rest[1].upper())
            else:
                print("Usage: /sym broker exec [add TICKER ISIN | rm TICKER]")
        else:
            print(
                "Usage:\n"
                "  /sym broker                    — list ISIN overrides\n"
                "  /sym broker add ISIN TICKER    — add override\n"
                "  /sym broker rm  ISIN           — remove override\n"
                "  /sym broker exec               — list execution overrides\n"
                "  /sym broker exec add TICKER ISIN\n"
                "  /sym broker exec rm  TICKER"
            )
        return

    print(
        "Usage:\n"
        "  /sym                            — scan positions (cache only, fast)\n"
        "  /sym resolve                    — resolve all unresolved via FMP + save\n"
        "  /sym cache                      — list master ISIN→ticker cache\n"
        "  /sym add  ISIN TICKER           — pin to master cache\n"
        "  /sym rm   ISIN                  — remove from master cache\n"
        "  /sym broker                     — list broker ISIN overrides\n"
        "  /sym broker add  ISIN TICKER    — add broker override\n"
        "  /sym broker rm   ISIN           — remove broker override\n"
        "  /sym broker exec                — list execution overrides (ticker→ISIN)\n"
        "  /sym broker exec add TICKER ISIN\n"
        "  /sym broker exec rm  TICKER"
    )


# ── scan (cache only, fast) ───────────────────────────────────────────────────

async def _cmd_scan(broker, broker_id: str) -> None:
    print("⏳ Fetching positions…")
    try:
        positions = await broker.get_positions()
    except Exception as e:
        print(f"❌ {e}")
        return

    if not positions:
        print("📭 No open positions.")
        return

    master          = _load_master()
    ti_data         = _load_ticker_isin()
    broker_override = ti_data.get(broker_id, {}).get("_isin_override", {})

    ok = bad_isin = unresolved = 0
    rows = []
    for p in sorted(positions, key=lambda x: x.symbol):
        isin = (p.id or "").strip()
        if not isin or not isin[:2].isalpha() or len(isin) < 12:
            src, ticker_str = "—", "❌ no valid ISIN"
            bad_isin += 1
        else:
            isin_up = isin.upper()
            if isin_up in broker_override:
                src        = "broker"
                ticker_str = f"✅ {broker_override[isin_up]}"
                ok += 1
            elif isin_up in master:
                src        = "master"
                ticker_str = f"✅ {master[isin_up]}"
                ok += 1
            else:
                src        = "—"
                ticker_str = "❌ unresolved"
                unresolved += 1
        rows.append((p.symbol, isin or "—", src, ticker_str))

    print(f"\n📋 Symbol cache — {broker_id}  "
          f"({ok} resolved  {unresolved} unresolved  {bad_isin} no-ISIN)")
    print(f"  {'Name':<28}  {'ISIN':<16}  {'Source':<8}  Ticker")
    print("  " + "─" * 74)
    for name, isin, src, ticker_str in rows:
        print(f"  {name:<28}  {isin:<16}  {src:<8}  {ticker_str}")

    if unresolved:
        print(f"\n  Run /sym resolve to auto-resolve {unresolved} missing entries via FMP")
    print()


# ── resolve (auto-populate master cache via FMP) ──────────────────────────────

async def _cmd_resolve(broker, broker_id: str) -> None:
    import asyncio
    print("⏳ Fetching positions…")
    try:
        positions = await broker.get_positions()
    except Exception as e:
        print(f"❌ {e}")
        return

    master          = _load_master()
    ti_data         = _load_ticker_isin()
    broker_override = ti_data.get(broker_id, {}).get("_isin_override", {})

    to_resolve = []
    for p in positions:
        isin = (p.id or "").strip()
        if not isin or not isin[:2].isalpha() or len(isin) < 12:
            continue
        isin_up = isin.upper()
        if isin_up in broker_override or isin_up in master:
            continue
        to_resolve.append(p)

    if not to_resolve:
        print("✅ All positions already resolved — nothing to do.")
        return

    print(f"⏳ Resolving {len(to_resolve)} ISIN(s) via FMP…")

    try:
        from services.fundamentals_service import FundamentalsService
        svc = FundamentalsService(api_key=os.environ.get("FMP_API_KEY", ""))
    except Exception as e:
        print(f"❌ FundamentalsService unavailable: {e}")
        return

    async def _resolve_one(pos):
        try:
            t = await svc.get_ticker_from_isin(
                pos.id, name_hint=pos.symbol, broker_id=broker_id
            )
            return pos, t
        except Exception:
            return pos, None

    results = await asyncio.gather(*[_resolve_one(p) for p in to_resolve])

    saved = failed = 0
    for pos, ticker in results:
        if ticker:
            master[pos.id.upper()] = ticker
            print(f"  ✅ {pos.symbol:<28}  {pos.id}  →  {ticker}")
            saved += 1
        else:
            print(f"  ❌ {pos.symbol:<28}  {pos.id}  →  not found")
            failed += 1

    if saved:
        _save_master(master)

    print(f"\n  Resolved {saved}  failed {failed}")
    if failed:
        print("  For failed entries: /sym add ISIN TICKER  or  /sym broker add ISIN TICKER")
    print()


# ── master cache ──────────────────────────────────────────────────────────────

def _cmd_cache() -> None:
    data = _load_master()
    if not data:
        print("Master ISIN cache is empty.")
        return
    print(f"\n📦 Master ISIN→ticker cache  ({len(data)} entries)")
    print(f"  {'ISIN':<16}  Ticker")
    print("  " + "─" * 30)
    for isin, ticker in sorted(data.items()):
        print(f"  {isin:<16}  {ticker}")
    print()


def _cmd_add(isin: str, ticker: str) -> None:
    data = _load_master()
    data[isin] = ticker
    _save_master(data)
    print(f"✅ Master cache: {isin} → {ticker}")


def _cmd_rm(isin: str) -> None:
    data = _load_master()
    if isin in data:
        del data[isin]
        _save_master(data)
        print(f"✅ Removed {isin} from master cache")
    else:
        print(f"⚠️  {isin} not in master cache")


# ── broker overrides ──────────────────────────────────────────────────────────

def _cmd_broker_list(broker_id: str) -> None:
    data     = _load_ticker_isin()
    overrides = data.get(broker_id, {}).get("_isin_override", {})
    if not overrides:
        print(f"No broker-level ISIN overrides for '{broker_id}'.")
        return
    print(f"\n🔧 Broker ISIN overrides — {broker_id}  ({len(overrides)} entries)")
    print(f"  {'ISIN':<16}  Ticker")
    print("  " + "─" * 30)
    for isin, ticker in sorted(overrides.items()):
        print(f"  {isin:<16}  {ticker}")
    print()


def _cmd_broker_add(broker_id: str, isin: str, ticker: str) -> None:
    data = _load_ticker_isin()
    data.setdefault(broker_id, {}).setdefault("_isin_override", {})[isin] = ticker
    _save_ticker_isin(data)
    print(f"✅ Broker override [{broker_id}]: {isin} → {ticker}")


def _cmd_broker_rm(broker_id: str, isin: str) -> None:
    data     = _load_ticker_isin()
    overrides = data.get(broker_id, {}).get("_isin_override", {})
    if isin in overrides:
        del overrides[isin]
        _save_ticker_isin(data)
        print(f"✅ Removed broker override [{broker_id}]: {isin}")
    else:
        print(f"⚠️  {isin} not in broker overrides for '{broker_id}'")


def _cmd_broker_exec_list(broker_id: str) -> None:
    data = _load_ticker_isin()
    exec_map = {k: v for k, v in data.get(broker_id, {}).items() if k != "_isin_override"}
    if not exec_map:
        print(f"No execution overrides for '{broker_id}'.")
        return
    print(f"\n⚙️  Execution overrides (ticker→ISIN) — {broker_id}  ({len(exec_map)} entries)")
    print(f"  {'Ticker':<10}  ISIN")
    print("  " + "─" * 30)
    for ticker, isin in sorted(exec_map.items()):
        print(f"  {ticker:<10}  {isin}")
    print()


def _cmd_broker_exec_add(broker_id: str, ticker: str, isin: str) -> None:
    data = _load_ticker_isin()
    data.setdefault(broker_id, {})[ticker] = isin
    _save_ticker_isin(data)
    print(f"✅ Execution override [{broker_id}]: {ticker} → {isin}")


def _cmd_broker_exec_rm(broker_id: str, ticker: str) -> None:
    data     = _load_ticker_isin()
    exec_sec = data.get(broker_id, {})
    if ticker in exec_sec:
        del exec_sec[ticker]
        _save_ticker_isin(data)
        print(f"✅ Removed execution override [{broker_id}]: {ticker}")
    else:
        print(f"⚠️  {ticker} not in execution overrides for '{broker_id}'")

#!/usr/bin/env python3
"""
Super Ron — Market Scanner Console
Interactive menu to run market scanners and news.
"""

import importlib.util
import os
import sys

_SRC     = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../scripts"))

if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _load_env() -> None:
    """Load .env from project root into os.environ (if not already set)."""
    env_path = os.path.join(_SRC, "..", ".env")
    if not os.path.exists(env_path):
        return
    try:
        from dotenv import dotenv_values
        for k, v in dotenv_values(env_path).items():
            os.environ.setdefault(k, v or "")
    except ImportError:
        for line in open(env_path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _load(name):
    path = os.path.join(_SCRIPTS, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    _pm_mod  = _load("run_post_market_scanner")
    _pre_mod = _load("run_pre_market_scanner")
    _hv_mod  = _load("run_daily_high_volumes")
    _sp_mod  = _load("run_spikes_scanner")
except Exception as e:
    sys.exit(f"Failed to load scanner modules:\n  {e}")


# ── Terminal helpers ──────────────────────────────────────────────────────────

W = 52

def _clear():
    os.system("cls" if os.name == "nt" else "clear")

def _header():
    _clear()
    print()
    print(f"  {'═' * (W - 2)}")
    print(f"  {'SUPER RON':^{W-2}}")
    print(f"  {'Market Scanner Console':^{W-2}}")
    print(f"  {'═' * (W - 2)}")
    print()

def _prompt(msg, default=None):
    hint = f" [{default}]" if default is not None else ""
    try:
        raw = input(f"  {msg}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return raw if raw else (str(default) if default is not None else None)

def _float_prompt(msg, default):
    raw = _prompt(msg, default)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        print(f"  Invalid number — using {default}")
        return default

def _pause():
    try:
        input("\n  Press Enter to return to menu…")
    except (EOFError, KeyboardInterrupt):
        pass


# ── Scanner commands ──────────────────────────────────────────────────────────

def cmd_post_market():
    _header()
    print("  POST-MARKET MOVERS\n")
    limit = _float_prompt("Limit", 50)
    if limit is None:
        return
    pmchg = _float_prompt("|PM change| threshold %", 3.0)
    if pmchg is None:
        return
    _pm_mod.run_scanner(limit=int(limit), min_pmchg=pmchg)
    _pause()


def cmd_pre_market():
    _header()
    print("  PRE-MARKET MOVERS\n")
    limit = _float_prompt("Limit", 50)
    if limit is None:
        return
    pmchg = _float_prompt("|PM change| threshold %", 5.0)
    if pmchg is None:
        return
    _pre_mod.run_scanner(limit=int(limit), min_pmchg=pmchg)
    _pause()


def cmd_high_volumes():
    smart_min = _hv_mod.SMART_MIN
    smart_max = _hv_mod.SMART_MAX
    default   = _hv_mod.DEFAULT_REL

    while True:
        _header()
        print("  DAILY HIGH VOLUMES\n")
        print(f"  [s]  Smart  — auto-scales {smart_min:.0f}x → {smart_max:.0f}x over the session")
        print(f"  [f]  Fixed  — enter rel vol value  (default {default:.0f}x)")
        print(f"  [b]  Back")
        print()

        choice = (_prompt("Mode") or "b").lower()

        if choice == "b":
            return
        if choice == "s":
            threshold  = _hv_mod.smart_threshold()
            mode_label = "SMART"
        elif choice == "f":
            threshold = _float_prompt("Rel vol threshold", default)
            if threshold is None:
                return
            mode_label = f"FIXED ({threshold:.2f}x)"
        else:
            print("  Unknown option.")
            continue

        limit = _float_prompt("Limit", 50)
        if limit is None:
            return
        _hv_mod.run_scanner(min_relvol=threshold, mode_label=mode_label, limit=int(limit))
        _pause()
        return


def cmd_spikes():
    _header()
    print("  INTRADAY SPIKES\n")
    limit = _float_prompt("Limit", 50)
    if limit is None:
        return
    min_chg = _float_prompt("|Chg from open| threshold %", 2.0)
    if min_chg is None:
        return
    min_relvol = _float_prompt("Rel vol threshold", 4.0)
    if min_relvol is None:
        return
    _sp_mod.run_scanner(limit=int(limit), min_chg=min_chg, min_relvol=min_relvol)
    _pause()


def cmd_news():
    _header()
    print("  NEWS — Recent Stock News\n")

    raw = _prompt("Ticker(s) — comma-separated", "AAPL")
    if raw is None:
        return
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if not symbols:
        print("  No tickers entered.")
        _pause()
        return

    days_raw = _prompt("Lookback days", 2)
    try:
        days = max(1, int(days_raw))
    except (TypeError, ValueError):
        days = 2

    _load_env()

    try:
        from services.news_service import NewsService
    except ImportError as e:
        print(f"  Cannot load NewsService: {e}")
        _pause()
        return

    svc     = NewsService(lookback_days=days)
    sources = svc.sources
    print(f"\n  Sources: {', '.join(sources) if sources else 'none (check API keys)'}\n")

    import datetime as _dt

    for sym in symbols:
        news = svc.get_news(sym)
        stats = svc.last_fetch_stats

        # Freshness indicator
        freshness = ""
        if news:
            newest = max(n.published_date for n in news)
            delta  = _dt.datetime.now() - newest
            hrs    = delta.total_seconds() / 3600
            if hrs < 1:
                freshness = f", most recent {int(delta.total_seconds() / 60)}m ago"
            else:
                freshness = f", most recent {hrs:.0f}h ago"

        print(f"  {'═' * (W - 2)}")
        print(f"  {sym}  — {len(news)} item(s) in last {days}d{freshness}")

        # Per-source breakdown from last_fetch_stats
        src_parts = []
        for key in ("FMP", "Finnhub", "AlphaVantage", "Yahoo", "Benzinga"):
            if key in stats:
                src_parts.append(f"{key}={stats[key]}")
        dropped = stats.get("dropped_dups", 0)
        if src_parts:
            print(f"  Sources: {', '.join(src_parts)} → merged={stats.get('merged', len(news))} (-{dropped} dups)")

        print(f"  {'─' * (W - 2)}")
        if not news:
            print(f"  No news found in the last {days} day(s). Check /news with more days.")
        else:
            for i, n in enumerate(news[:25], 1):
                ts  = n.published_date.strftime("%m/%d %H:%M")
                src = n.publisher or n.site or ""
                src_tag = f"  [{src}]" if src else ""
                print(f"  {i:>2}. [{ts}]{src_tag}")
                title = n.title
                if len(title) > W - 8:
                    title = title[: W - 11] + "…"
                print(f"      {title}")
                print(f"      {n.url}")
                print()
        print()

    _pause()


# ── Help ─────────────────────────────────────────────────────────────────────

HELP = """
  ┌─────────────────────────────────────────────────────┐
  │  SUPER RON — Scanner Help                           │
  ├─────────────────────────────────────────────────────┤
  │                                                     │
  │  [1]  Post-Market Movers                            │
  │       US stocks with unusual post-market moves.     │
  │       Prompts:                                      │
  │         Limit              max rows shown  [50]     │
  │         |PM change| %      move threshold  [3.0]    │
  │       Static filters applied to every run:          │
  │         price >$2  |  mcap >$300M                  │
  │         avg vol 30d >500K  |  pm vol >100K          │
  │                                                     │
  │  [2]  Pre-Market Movers                             │
  │       Same structure, pre-market session.           │
  │       Prompts:                                      │
  │         Limit              max rows shown  [50]     │
  │         |PM change| %      move threshold  [5.0]    │
  │       Static filters:                               │
  │         price >$2  |  mcap >$300M                  │
  │         avg vol 30d >500K  |  pm vol >100K          │
  │                                                     │
  │  [3]  Daily High Volumes                            │
  │       Stocks at unusual relative volume vs          │
  │       their 10-day average.                         │
  │       Sub-menu:                                     │
  │         [s] Smart   rel vol threshold scales        │
  │                     1x→3x over session (9:30-4 ET). │
  │                     Early open: 1x is signal.       │
  │                     Near close: need 3x to stand out│
  │         [f] Fixed   enter any rel vol value         │
  │                     default 3.0x                    │
  │       Prompts (both modes):                         │
  │         Limit              max rows shown  [50]     │
  │       Static filters:                               │
  │         price >$2  |  mcap >$300M  |  avg vol >500K │
  │                                                     │
  │  [4]  Intraday Spikes                               │
  │       Stocks spiking hard from today's open.        │
  │       Prompts:                                      │
  │         Limit              max rows shown  [50]     │
  │         |Chg from open| %  spike threshold [2.0]    │
  │         Rel vol threshold  volume filter   [4.0]    │
  │       Static filters:                               │
  │         price >$2  |  mcap >$300M  |  avg vol >500K │
  │       Note: uses change_from_open (intraday vs      │
  │       today's open) — closest API proxy for 5m chg. │
  │                                                     │
  │  [5]  News                                          │
  │       Recent stock news from 4 sources.             │
  │       Prompts:                                      │
  │         Ticker(s)          comma-separated  [AAPL]  │
  │         Lookback days      1 = today only   [2]     │
  │       Sources:                                      │
  │         Yahoo    — always on (no key needed)        │
  │         FMP      — needs FMP_API_KEY                │
  │         Finnhub  — needs FINNHUB_API_KEY            │
  │         AV       — needs AV_API_KEY                 │
  │         Benzinga — needs BENZINGA_API_KEY           │
  │       Output: up to 25 items per ticker, newest     │
  │       first, deduped across sources.                │
  │                                                     │
  │  Press Enter at any prompt to accept the default.   │
  │  Ctrl-C during a scan returns to this menu.         │
  │                                                     │
  └─────────────────────────────────────────────────────┘
"""

def cmd_help():
    _clear()
    print(HELP)
    _pause()


# ── Command registry ──────────────────────────────────────────────────────────
# Add new scanners/tools here — key: menu key, value: (label, callable)

COMMANDS = {
    "1": ("Post-Market Movers",  cmd_post_market),
    "2": ("Pre-Market Movers",   cmd_pre_market),
    "3": ("Daily High Volumes",  cmd_high_volumes),
    "4": ("Intraday Spikes",     cmd_spikes),
    "5": ("News",                cmd_news),
}


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    while True:
        _header()
        for key, (label, _) in COMMANDS.items():
            print(f"  [{key}]  {label}")
        print()
        print(f"  [h]  Help")
        print(f"  [q]  Quit")
        print()

        choice = (_prompt("Select") or "q").lower()

        if choice == "q":
            _clear()
            print("\n  Bye.\n")
            break
        elif choice == "h":
            cmd_help()
        elif choice in COMMANDS:
            _, fn = COMMANDS[choice]
            try:
                fn()
            except KeyboardInterrupt:
                pass  # Ctrl-C during a scan → back to menu
        else:
            print("  Unknown option — try again.")


if __name__ == "__main__":
    main()

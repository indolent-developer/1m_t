#!/usr/bin/env python3
"""
interfaces.console.local_cli

Interactive local REPL for testing broker commands without Telegram.
Supports the same slash commands as the bot.

Usage:
    PYTHONPATH=src python src/interfaces/console/local_cli.py
    # or via the shell wrapper:
    ./run_scripts/run_local.sh
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

try:
    import readline as _rl
    _HIST = Path.home() / ".1m_cli_history"
    try:
        _rl.read_history_file(_HIST)
    except FileNotFoundError:
        pass
    _rl.set_history_length(500)
    import atexit as _atexit
    _atexit.register(_rl.write_history_file, _HIST)
except ImportError:
    pass  # Windows — no readline, arrow keys just won't work

_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Load .env before any imports that need env vars
_ENV = _SRC.parent / ".env"
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

from adapters.brokers.scalable_broker import ScalableBroker
from adapters.brokers.capital_broker  import CapitalBroker
from adapters.brokers.ibkr_broker     import IBKRBroker
from adapters.brokers.etoro_broker    import eToroBroker
from adapters.brokers.base_broker     import BaseBroker
from core.config.config_loader        import ConfigLoader

from interfaces.console.cmd_account  import cmd_account, cmd_positions, cmd_orders, cmd_pnl, cmd_quote, cmd_fills
from interfaces.console.cmd_trading  import cmd_trade, cmd_move, cmd_close, cmd_closeall, cmd_pending, cmd_cancel, cmd_size
from interfaces.console.cmd_monitor       import cmd_ind, cmd_indp, cmd_scan, cmd_news, cmd_ml
from interfaces.console.cmd_search        import cmd_search
from interfaces.console.cmd_trade_helper  import cmd_trade_helper
from interfaces.console.cmd_sym           import cmd_sym


_BROKER_NAMES = ("capital", "ibkr", "etoro", "scalable")
_SCAN_TYPES   = ("pm", "pre", "vol", "spikes", "parabolic")
_ML_FILTERS   = ("break_up", "break_down", "bounce", "reject", "false_break", "stop", "list", "status")


# ── Help ──────────────────────────────────────────────────────────────────────

HELP = """
  /account    — account, positions, orders, P&L, fills
  /trading    — buy, sell, move, close, cancel, size
  /monitor    — level alerts (/ml), indicators, scan, news, search
  /broker     — show or switch broker

  /th SYMBOL  — deep pre-trade analysis (technicals + news + LLM verdict)

  /h account | trading | monitor | broker   for details
""".strip()

HELP_ACCOUNT = """
  /a  /account                   — Account snapshot
  /p  /positions [SYMBOL]        — Open positions
  /o  /orders                    — Recent orders
  /pnl                           — Open P&L
  /fills SYMBOL [N]              — Fill history + realized P&L (last N trades)
  /q  /quote SYMBOL              — Live bid/ask/last
""".strip()

HELP_TRADING = """
  /buy  SYMBOL SIZE [@TRIGGER] [stop|limit]
  /sell SYMBOL SIZE [@TRIGGER] [stop|limit]
    SIZE     : N shares | N% | all | eN euros | $N USD
    @TRIGGER : @118 | @atr1.5 | @atr2:5m
    Examples:
      /buy  PLTR e3000 @118       stop entry (price ≥ 118)
      /buy  WOLF e2000 @2.10      limit entry (price ≤ 2.10)
      /sell WOLF all @2.50        stop-loss
      /sell WOLF 50% @4.00        take-profit

  /move SYMBOL1 SIZE SYMBOL2     — Sell → wait for fill → buy
  /close SYMBOL                  — Market-close full position
  /closeall [PCT%]               — Close all (or trim by PCT%)
  /pending  (/op)                — Resting stop/limit orders
  /cancel SYMBOL|ID|all  (/x)   — Cancel resting orders
  /size SYMBOL STOP_USD RISK_EUR — Position size by risk
""".strip()

HELP_MONITOR = """
  /ml SYMBOL LEVEL [LEVEL...] [filter...]
       Filters: break_up  break_down  bounce  reject  false_break  (default: all)
       /ml APLD 41.5 break_down
       /ml AAPL 200 210 bounce
       /ml status [SYMBOL]  — current price + distance from watched levels
       /ml stop SYMBOL | /ml list

  /ind  SYMBOL [TF]              — ATR, RSI, ADX, EMA, SuperTrend
  /indp [TF]                     — Portfolio indicators  (ignore/unignore/list)
  /scan pm|pre|vol|spikes|parabolic
  /news SYMBOL[,...] [DAYS]      — News (Yahoo+FMP+Finnhub+AV, default 2d)
  /search SYMBOL  (/sr)          — Tradability + price across all brokers

  /sym                           — Scan positions: ISIN → ticker (cache only)
  /sym resolve                   — Auto-resolve all missing via FMP + save
  /sym add  ISIN TICKER          — Pin to master cache
  /sym rm   ISIN                 — Remove from master cache
  /sym cache                     — List master cache
  /sym broker [add ISIN TICKER | rm ISIN]
  /sym broker exec [add TICKER ISIN | rm TICKER]
""".strip()

HELP_BROKER = """
  /broker                        — Show active broker
  /broker capital  [live|demo]   — Switch to Capital.com
  /broker ibkr     [live|demo]   — Switch to Interactive Brokers
  /broker etoro    [live|demo]   — Switch to eToro
  /broker scalable               — Switch to Scalable Capital
""".strip()

_HELP_TOPICS = {
    "account": HELP_ACCOUNT,
    "trading": HELP_TRADING,
    "monitor": HELP_MONITOR,
    "broker":  HELP_BROKER,
}

_COMMANDS = [
    "/exit", "/quit", "/h", "/help",
    "/a", "/account", "/p", "/positions", "/o", "/orders",
    "/pnl", "/q", "/quote", "/fills",
    "/buy", "/b", "/sell", "/s", "/move", "/mv",
    "/c", "/close", "/ca", "/closeall",
    "/pending", "/op", "/cancel", "/x", "/size",
    "/ind", "/indp", "/scan", "/news", "/n",
    "/ml", "/search", "/sr", "/th", "/tradehelp",
    "/sym", "/broker",
]


try:
    import readline as _rl

    def _rl_completer(text: str, state: int):
        try:
            buf   = _rl.get_line_buffer().lstrip()
            parts = buf.split()

            if not parts or (len(parts) == 1 and not buf.endswith(" ")):
                candidates = [c for c in _COMMANDS
                              if c.startswith(text) or c.lstrip("/").startswith(text)]
            else:
                cmd  = parts[0].lstrip("/").lower()
                word = text
                if cmd == "broker":
                    if len(parts) == 1 or (len(parts) == 2 and not buf.endswith(" ")):
                        candidates = [n for n in _BROKER_NAMES if n.startswith(word)]
                    else:
                        candidates = [m for m in ("live", "demo") if m.startswith(word)]
                elif cmd == "scan":
                    candidates = [t for t in _SCAN_TYPES if t.startswith(word)]
                elif cmd in ("ml", "monitor-levels", "monitor_levels"):
                    candidates = [f for f in _ML_FILTERS if f.startswith(word)]
                else:
                    candidates = []

            return candidates[state] if state < len(candidates) else None
        except Exception:
            return None

    # Remove / from delimiters so "/buy" arrives as a whole token, not just "buy"
    _rl.set_completer_delims(" \t\n")
    _rl.set_completer(_rl_completer)
    # macOS ships libedit which needs a different binding than GNU readline
    if "libedit" in getattr(_rl, "__doc__", ""):
        _rl.parse_and_bind("bind ^I rl_complete")
    else:
        _rl.parse_and_bind("tab: complete")

except ImportError:
    pass


# ── Broker factory ────────────────────────────────────────────────────────────

def _make_broker(name: str, is_demo: bool | None = None) -> BaseBroker:
    cfg = ConfigLoader().load_broker(name)
    if is_demo is not None and hasattr(cfg, "is_demo"):
        cfg.is_demo = is_demo
    if name == "ibkr" and is_demo is not None:
        cfg.port = 4002 if is_demo else 4001
    if name == "capital":  return CapitalBroker(cfg)
    if name == "ibkr":     return IBKRBroker(cfg)
    if name == "etoro":    return eToroBroker(cfg)
    if name == "scalable": return ScalableBroker(cfg)
    raise ValueError(f"Unknown broker '{name}'. Valid: {list(_BROKER_NAMES)}")


# ── Main REPL ─────────────────────────────────────────────────────────────────

async def run(broker: BaseBroker) -> None:
    try:
        acc = await broker.get_account_info()
        print(
            f"\n🟢  {broker.broker_id}  ·  {acc.account_id}  ·  "
            f"{acc.current_value:,.2f} {acc.currency}  ·  cash {acc.cash_in_hand:,.2f}"
        )
    except Exception:
        print(f"\n🟢  {broker.broker_id}")
    print("    /h for commands, /exit to quit.\n")

    ml_tasks: dict = {}  # symbol → {task, monitor, manager, levels, filters}
    # ml monitors are owned by run_live_monitor.py — do not auto-start here

    async def _shutdown_ml():
        for entry in list(ml_tasks.values()):
            entry["task"].cancel()
        if ml_tasks:
            await asyncio.gather(*[e["task"] for e in ml_tasks.values()], return_exceptions=True)
        ml_tasks.clear()
        from data_fetchers.finnhub_ws_data_fetcher import shutdown_shared_fetcher
        await shutdown_shared_fetcher()

    loop = asyncio.get_event_loop()

    while True:
        try:
            line = await loop.run_in_executor(None, lambda: input("› "))
            line = line.strip()
        except (EOFError, KeyboardInterrupt, asyncio.CancelledError):
            print("\nBye.")
            await _shutdown_ml()
            break

        if not line:
            continue

        parts = line.split()
        cmd   = parts[0].lower().lstrip("/")
        args  = parts[1:]

        # ── Exit ──────────────────────────────────────────────────────────────
        if cmd in ("exit", "quit", "q") and not args:
            print("Bye.")
            await _shutdown_ml()
            break

        # ── Help ──────────────────────────────────────────────────────────────
        elif cmd in ("h", "help"):
            if args:
                print(_HELP_TOPICS.get(args[0].lower()) or
                      f"Unknown topic '{args[0]}'. Try: {', '.join(_HELP_TOPICS)}")
            else:
                print(HELP)

        # ── Account ───────────────────────────────────────────────────────────
        elif cmd in ("a", "account"):          await cmd_account(broker, args)
        elif cmd in ("p", "positions"):        await cmd_positions(broker, args)
        elif cmd in ("o", "orders"):           await cmd_orders(broker, args)
        elif cmd == "pnl":                     await cmd_pnl(broker, args)
        elif cmd in ("q", "quote"):            await cmd_quote(broker, args)
        elif cmd in ("fills", "fill"):         await cmd_fills(broker, args)

        # ── Trading ───────────────────────────────────────────────────────────
        elif cmd in ("buy",  "b"):             await cmd_trade(broker, "buy",  args)
        elif cmd in ("sell", "s"):             await cmd_trade(broker, "sell", args)
        elif cmd in ("move", "mv"):            await cmd_move(broker, args)
        elif cmd in ("c", "close"):            await cmd_close(broker, args)
        elif cmd in ("ca", "closeall"):        await cmd_closeall(broker, args)
        elif cmd in ("pending", "op"):         await cmd_pending(broker, args)
        elif cmd in ("cancel", "x"):           await cmd_cancel(broker, args)
        elif cmd == "size":                    await cmd_size(broker, args)

        # ── Monitor ───────────────────────────────────────────────────────────
        elif cmd == "ind":                     await cmd_ind(broker, args)
        elif cmd in ("indp", "ind_port"):      await cmd_indp(broker, args)
        elif cmd == "scan":                    await cmd_scan(args)
        elif cmd in ("news", "n"):             await cmd_news(args)
        elif cmd in ("ml", "monitor-levels", "monitor_levels"):
                                               await cmd_ml(broker, args, ml_tasks)

        # ── Symbol cache ──────────────────────────────────────────────────────
        elif cmd in ("sym", "symbol"):         await cmd_sym(broker, args)

        # ── Search ────────────────────────────────────────────────────────────
        elif cmd in ("search", "sr"):          await cmd_search(broker, args)

        # ── Trade helper ──────────────────────────────────────────────────────
        elif cmd in ("th", "tradehelp"):       await cmd_trade_helper(broker, args)

        # ── Broker switch ─────────────────────────────────────────────────────
        elif cmd == "broker":
            if not args:
                print(f"Active broker: {broker.broker_id}")
                continue
            name = args[0].lower()
            if name not in _BROKER_NAMES:
                print(f"Unknown broker '{name}'. Valid: {', '.join(_BROKER_NAMES)}")
                continue
            is_demo: bool | None = None
            if len(args) > 1:
                mode = args[1].lower()
                if   mode == "live": is_demo = False
                elif mode == "demo": is_demo = True
                else:
                    print(f"Unknown mode '{mode}'. Use: live | demo")
                    continue
            try:
                await broker.disconnect()
                broker = _make_broker(name, is_demo)
                ok = await broker.connect()
                print(f"{'✅ Switched to' if ok else '❌ Failed to connect to'} {broker.broker_id}")
            except Exception as e:
                print(f"❌ {e}")

        else:
            print(f"Unknown command: /{cmd}  — type /h for help")


async def main() -> None:
    active = ConfigLoader().load_section("broker").active
    broker = _make_broker(active)
    ok = await broker.connect()
    if not ok:
        sys.exit(f"❌ Broker connect failed ({active}).")
    try:
        await run(broker)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await broker.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

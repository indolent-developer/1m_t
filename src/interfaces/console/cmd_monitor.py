"""
interfaces.console.cmd_monitor

Handlers for monitoring commands: /ind /indp /scan /news /ml
"""
from __future__ import annotations

from pathlib import Path

from interfaces.console.formatters import tty_colors

_SRC = Path(__file__).resolve().parents[2]  # src/


async def cmd_ind(broker, args: list) -> None:
    if not args:
        print("Usage: /ind SYMBOL [TF]   TF: 1m 5m 15m 30m 1h 4h 1d (default 1d)")
        return
    symbol   = args[0].upper()
    tf       = args[1].lower() if len(args) > 1 else "1d"
    extended = len(args) > 2 and args[2].lower() == "ext"
    ext_tag  = " +ext" if extended else ""
    print(f"⏳ Fetching {symbol} ({tf}{ext_tag})…")
    try:
        from services.command_service import CommandService
        r         = await CommandService(broker).indicators(symbol, tf, extended=extended)
        GREEN, RED, RESET = tty_colors()
        st_color  = GREEN if r.st_dir == 1 else RED
        st_label  = "Long " if r.st_dir == 1 else "Short"
        flip_tag  = "  [FLIP]" if r.st_flipped else ""
        rsi_tag   = "  🔥" if r.rsi and r.rsi >= 70 else ("  🧊" if r.rsi and r.rsi <= 30 else "")
        adx_str   = f"{r.adx:.1f}" if r.adx is not None else "—"
        def _v(v): return str(v) if v is not None else "—"
        print(
            f"\n📊 Indicators — {symbol} ({tf}{ext_tag})  {r.ts}\n"
            f"  {'─'*40}\n"
            f"  ATR        {_v(r.atr):>10}  ({_v(r.atr_pct)}%)\n"
            f"  RSI        {_v(r.rsi):>10}{rsi_tag}\n"
            f"  ADX 20     {adx_str:>10}\n"
            f"  EMA 8      {_v(r.ema8):>10}\n"
            f"  EMA 20     {_v(r.ema20):>10}\n"
            f"  SuperTrend {st_color}{st_label} {_v(r.st_value):>8}{RESET}{flip_tag}\n"
        )
    except Exception as e:
        print(f"❌ {e}")


async def cmd_indp(broker, args: list) -> None:
    from services.command_service import CommandService
    svc = CommandService(broker)

    if args and args[0].lower() == "ignore" and len(args) >= 2:
        name = " ".join(args[1:]).lower()
        svc.ignore_list_add(name)
        print(f"✅ Added '{name}' to ignore list")
        return
    if args and args[0].lower() == "unignore" and len(args) >= 2:
        name = " ".join(args[1:]).lower()
        svc.ignore_list_remove(name)
        print(f"✅ Removed '{name}' from ignore list")
        return
    if args and args[0].lower() == "list":
        ig = svc.ignore_list_get()
        print("Ignore list:") if ig else print("Ignore list is empty.")
        for e in sorted(ig): print(f"  • {e}")
        return

    tf = args[0].lower() if args else "1m"
    print(f"⏳ Running portfolio indicators ({tf} +ext)…")
    try:
        pr = await svc.portfolio_indicators(tf=tf, extended=True)
        if not pr.rows and not pr.skipped:
            print("📭 No open positions.")
            return
        GREEN, RED, RESET = tty_colors()
        rows      = sorted(pr.rows, key=lambda r: r.indicators.atr_pct or 0, reverse=True)
        oldest_ts = min((r.indicators.ts for r in rows if r.indicators.ts), default="?")
        print(f"\n📊 Portfolio — {tf} +ext  (data as of {oldest_ts})")
        print(f"  {'─'*84}")
        print(f"  {'Ticker':<6}  {'ST':>9}  {'ST%':>6}  {'RSI':>4}  {'ADX':>4}  {'ATR%':>5}  {'EMA8':>8}  {'EMA20':>8}  EMA")
        print(f"  {'─'*84}")
        for row in rows:
            ind       = row.indicators
            st_color  = GREEN if ind.st_dir == 1 else RED
            flip      = " [F]" if ind.st_flipped else ""
            st_label  = "▲" if ind.st_dir == 1 else "▼"
            ema_cross = ""
            if ind.ema8 is not None and ind.ema20 is not None:
                ema_cross = " (T)" if ind.ema8 > ind.ema20 else " (↓)"
            def _f(v, fmt): return f"{v:{fmt}}" if v is not None else "—"
            st_dist_str = "—"
            if ind.st_value is not None and ind.close:
                st_dist_str = f"{(ind.close - ind.st_value) / ind.st_value * 100:+.1f}%"
            print(
                f"  {row.ticker:<6}  "
                f"{st_color}{st_label} {_f(ind.st_value, '8.2f')}{RESET}"
                f"  {st_dist_str:>6}"
                f"  {_f(ind.rsi, '4.0f')}"
                f"  {_f(ind.adx, '4.0f')}"
                f"  {_f(ind.atr_pct, '5.2f')}%"
                f"  {_f(ind.ema8, '8.2f')}"
                f"  {_f(ind.ema20, '8.2f')}"
                f"{ema_cross}{flip}"
            )
        if pr.skipped:
            names  = ", ".join(n for n, _ in pr.skipped[:8])
            suffix = f" +{len(pr.skipped)-8} more" if len(pr.skipped) > 8 else ""
            print(f"\n  ⚠️  Skipped ({len(pr.skipped)}): {names}{suffix}")
        print()
    except Exception as e:
        print(f"❌ {e}")


async def cmd_scan(args: list) -> None:
    if not args:
        print("Usage: /scan pm | pre | vol | spikes | parabolic")
        return
    scan_type = args[0].lower()
    print(f"⏳ Running {scan_type} scanner…")
    try:
        from services.command_service import CommandService, CommandError
        output = await CommandService(broker=None).scan(scan_type)
        print(output)
    except CommandError as e:
        print(f"❌ {e}")
    except Exception as e:
        print(f"❌ Scanner error: {e}")


async def cmd_news(args: list) -> None:
    if not args:
        print("Usage: /news SYMBOL[,SYMBOL,...] [DAYS]")
        print("  e.g. /news AAPL        — last 2 days")
        print("       /news AAPL,TSLA 1 — today only, two tickers")
        return
    symbols = [s.strip().upper() for s in args[0].split(",") if s.strip()]
    days    = 2
    if len(args) >= 2:
        try:
            days = max(1, int(args[1]))
        except ValueError:
            pass
    try:
        import datetime as _dt
        from services.news_service import NewsService
        svc       = NewsService(lookback_days=days)
        src_label = ", ".join(svc.sources) if svc.sources else "none (check API keys)"
        print(f"\n📰 News  sources: {src_label}  |  lookback: {days}d\n")
        for sym in symbols:
            news  = svc.get_news(sym)
            stats = svc.last_fetch_stats

            freshness = ""
            if news:
                newest = max(n.published_date for n in news)
                delta  = _dt.datetime.now() - newest
                hrs    = delta.total_seconds() / 3600
                freshness = (
                    f"  most recent {int(delta.total_seconds() / 60)}m ago"
                    if hrs < 1 else f"  most recent {hrs:.0f}h ago"
                )

            print(f"  {'─'*56}")
            print(f"  {sym}  — {len(news)} item(s) in last {days}d{freshness}")

            src_parts = [
                f"{k}={stats[k]}"
                for k in ("FMP", "Finnhub", "AlphaVantage", "Yahoo")
                if k in stats
            ]
            dropped = stats.get("dropped_dups", 0)
            if src_parts:
                print(f"  {', '.join(src_parts)} → merged={stats.get('merged', len(news))} (-{dropped} dups)")

            print(f"  {'─'*56}")
            if not news:
                print(f"  No news found in the last {days} day(s).\n")
                continue
            for i, n in enumerate(news[:20], 1):
                ts      = n.published_date.strftime("%m/%d %H:%M")
                src     = n.publisher or n.site or ""
                src_tag = f"  [{src}]" if src else ""
                print(f"  {i:>2}. [{ts}]{src_tag}")
                print(f"      {n.title}")
                print(f"      {n.url}")
            print()
    except Exception as e:
        print(f"❌ {e}")


_tg_bot = None   # lazy singleton — reused across break alerts


def _make_tg_break_fn():
    """Return an async callable that sends a Telegram message, or None if not configured."""
    import os
    token   = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return None

    from core.utils.log_helper import getLogger as _getLogger
    _log = _getLogger(__name__)

    async def _fn(text: str) -> None:
        global _tg_bot
        try:
            from telegram import Bot
            if _tg_bot is None:
                _tg_bot = Bot(token=token)
            await _tg_bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as exc:
            _log.warning("Telegram break alert failed: %s", exc)

    return _fn


def _ml_persist(ml_tasks: dict, save_fn) -> None:
    """Write current ml_tasks to disk (called automatically on add/stop)."""
    data = {
        sym: {"levels": entry["levels"], "filters": [f.value for f in entry["filters"]]}
        for sym, entry in ml_tasks.items()
    }
    save_fn(data)


async def ml_autoload(ml_tasks: dict) -> None:
    """
    Called once at startup. Silently restores monitors saved to disk.
    Missing or empty file is a no-op.
    """
    from interfaces.telegram.commands import _load_ml_levels, _ml_start_monitor
    from core.entities.level_event import LevelEvent as _LE
    import re as _re

    saved = _load_ml_levels()
    if not saved:
        return

    async def _send(text: str) -> None:
        print(f"\n{_re.sub(r'[*`]', '', text)}")

    break_fn = _make_tg_break_fn()
    started = []
    for sym, cfg in saved.items():
        if sym in ml_tasks:
            continue
        lvls = [float(v) for v in cfg.get("levels", [])]
        filt = set()
        for v in cfg.get("filters", []):
            try:
                filt.add(_LE(v))
            except ValueError:
                pass
        try:
            task, monitor, manager = await _ml_start_monitor(sym, lvls, filt, _send, break_fn)
        except Exception as e:
            print(f"⚠️  /ml auto-load {sym}: {e}")
            continue
        ml_tasks[sym] = {"task": task, "monitor": monitor, "manager": manager,
                          "levels": lvls, "filters": list(filt)}
        started.append(sym)
    if started:
        lvl_summary = "  ".join(
            f"{s}@[{','.join(str(l) for l in saved[s]['levels'])}]" for s in started
        )
        tg_note = "  → Telegram" if break_fn else ""
        print(f"🔄 Auto-resumed {len(started)} monitor(s): {lvl_summary}{tg_note}")


async def _cmd_ml_levels(broker, symbol: str) -> None:
    """
    Compute key levels for SYMBOL using KeyLevelService and print them
    sorted by distance from current price.
    """
    import os
    from data_fetchers.financial_modelling_prep_data_fetcher import FmpDataFetcher
    from infrastructure.cache.memory_cache import MemoryCache
    from services.key_level_service import KeyLevelService
    from services.price_history_service import PriceHistoryService

    print(f"⏳ Computing key levels for {symbol}…")
    try:
        fmp_key = os.environ.get("FMP_API_KEY", "")
        fetcher = FmpDataFetcher({"api_key": fmp_key})
        history = PriceHistoryService(fetcher=fetcher, cache=MemoryCache())
        svc     = KeyLevelService(history)
        kl      = await svc.compute_levels(symbol)
    except Exception as e:
        print(f"❌ Failed to compute levels: {e}")
        return

    # Get current price from broker quote (via ISIN if available), fallback to last FMP bar
    price = None
    try:
        q     = await broker.get_quote(symbol)
        price = float(q.bid or q.mid or q.last or 0) or None
    except Exception:
        pass

    if not price:
        try:
            from services.fundamentals_service import FundamentalsService
            fmp   = FundamentalsService(api_key=fmp_key)
            df    = await fmp.get_ohlcv(symbol, "1d", 2)
            price = float(df["c"].iloc[-1])
        except Exception:
            pass

    labels = kl.labels()
    levels = kl.all_levels()

    GREEN, RED, RESET = tty_colors()
    price_str = f"  current price: {price:.2f}" if price else ""
    print(f"\n📐 Key levels — {symbol}{price_str}")
    print(f"  {'Level':>8}  {'Label':<20}  {'Distance':>10}  {'%':>6}  Side")
    print("  " + "─" * 58)

    for lvl in sorted(levels, key=lambda l: abs(l - price) if price else l):
        label = labels.get(round(lvl, 4), "")
        if price:
            dist     = lvl - price
            dist_pct = dist / price * 100
            side     = "above" if dist > 0 else "below"
            col      = GREEN if dist > 0 else RED
            dist_str = f"{col}{dist:>+10.2f}{RESET}"
            pct_str  = f"{col}{dist_pct:>+6.1f}%{RESET}"
        else:
            dist_str = "      —"
            pct_str  = "     —"
            side     = ""
        print(f"  {lvl:>8.2f}  {label:<20}  {dist_str}  {pct_str}  {side}")

    # Suggest the closest levels above and below as monitor candidates
    if price and levels:
        above = [l for l in levels if l > price]
        below = [l for l in levels if l < price]
        suggest = []
        if below: suggest.append(f"{below[-1]:.2f}")
        if above: suggest.append(f"{above[0]:.2f}")
        if suggest:
            print(f"\n  → /ml {symbol} {' '.join(suggest)}")
    print()


async def cmd_ml(broker, args: list, ml_tasks: dict) -> None:
    from interfaces.telegram.commands import (
        _ml_parse_args, _ml_start_monitor,
        _load_ml_levels, _save_ml_levels,
    )

    if not args:
        print(
            "Usage: /ml SYMBOL LEVEL [LEVEL...] [filter...]\n"
            "       Filters: break_up  break_down  bounce  reject  false_break\n"
            "       /ml status [SYMBOL]  — current price + distance from watched levels\n"
            "       /ml levels SYMBOL   — compute + show key levels with distance\n"
            "       /ml stop SYMBOL\n"
            "       /ml list\n"
            "       /ml save   — save active monitors to disk (auto-saved on add/stop)\n"
            "       /ml load   — start saved monitors\n"
            "       /ml clear  — delete saved monitors"
        )
        return

    sub = args[0].lower()

    if sub == "list":
        saved = _load_ml_levels()
        if not saved:
            print("No saved level monitors.")
        else:
            for sym, cfg in saved.items():
                lvl_str = ", ".join(str(l) for l in cfg.get("levels", []))
                flt_str = ", ".join(cfg.get("filters", [])) or "all"
                print(f"  • {sym}: [{lvl_str}]  filters=[{flt_str}]")
        return

    if sub == "save":
        data = {
            sym: {"levels": entry["levels"], "filters": [f.value for f in entry["filters"]]}
            for sym, entry in ml_tasks.items()
        }
        _save_ml_levels(data)
        print(f"✅ Saved {len(data)} monitor(s)")
        return

    if sub == "clear":
        _save_ml_levels({})
        print("✅ Saved levels cleared")
        return

    if sub == "load":
        from core.entities.level_event import LevelEvent as _LE
        saved = _load_ml_levels()
        if not saved:
            print("No saved levels found.")
            return
        import re as _re
        async def _load_send(text: str) -> None:
            print(f"\n{_re.sub(r'[*`]', '', text)}")
        break_fn = _make_tg_break_fn()
        started = []
        for sym, cfg in saved.items():
            if sym in ml_tasks:
                continue
            lvls = [float(v) for v in cfg.get("levels", [])]
            filt = set()
            for v in cfg.get("filters", []):
                try:
                    filt.add(_LE(v))
                except ValueError:
                    pass
            print(f"⏳ Loading {sym}…")
            try:
                task, monitor, manager = await _ml_start_monitor(sym, lvls, filt, _load_send, break_fn)
            except Exception as e:
                print(f"❌ {sym}: {e}")
                continue
            ml_tasks[sym] = {"task": task, "monitor": monitor, "manager": manager,
                              "levels": lvls, "filters": list(filt)}
            started.append(sym)
        if started:
            print(f"✅ Loaded: {', '.join(started)}")
            if break_fn:
                print("   Break alerts → Telegram")
            print("   ⚠️  Price monitor service must be running (run_live_monitor.py) for alerts to fire.")
        else:
            print("Nothing new to load.")
        return

    if sub == "status":
        target   = args[1].upper() if len(args) > 1 else None
        snapshot = {s: e for s, e in ml_tasks.items() if target is None or s == target}
        if not snapshot:
            print(f"No monitor for {target}" if target else "No active level monitors.")
            return
        import os as _os
        GREEN, RED, RESET = tty_colors()
        for sym, entry in snapshot.items():
            price = None
            try:
                q     = await broker.get_quote(sym)
                price = float(q.last or q.mid or q.bid or 0) or None
            except Exception:
                pass
            if not price:
                try:
                    from services.price_service import FmpPriceService as _FmpPS
                    _svc    = _FmpPS(api_key=_os.environ.get("FMP_API_KEY", ""), symbols=[sym])
                    _quotes = await _svc.get_quotes()
                    _q      = _quotes.get(sym)
                    price   = float(_q.price) if _q and _q.price else None
                except Exception:
                    pass
            price_str = f"{price:,.4f}" if price else "N/A"
            flt_str = ", ".join(f.value for f in entry["filters"]) if entry["filters"] else "all"
            print(f"\n📍 {sym}  price: {price_str}  ({flt_str})")
            print(f"  {'Level':>10}   {'Dir':>3}   {'Distance':>10}   {'%':>6}")
            print(f"  {'─'*44}")
            for level in sorted(entry["levels"]):
                if price:
                    diff   = level - price
                    pct    = diff / price * 100
                    arrow  = "↑" if diff > 0 else "↓"
                    col    = GREEN if diff > 0 else RED
                    print(f"  {level:>10.4f}   {col}{arrow}{RESET}     {col}{abs(diff):>10.4f}{RESET}   {col}{abs(pct):>5.2f}%{RESET}")
                else:
                    print(f"  {level:>10.4f}   —        —          —")
        print()
        return

    if sub == "stop":
        if len(args) < 2:
            print("Usage: /ml stop SYMBOL")
            return
        symbol = args[1].upper()
        entry  = ml_tasks.pop(symbol, None)
        if not entry:
            print(f"No monitor for {symbol}")
        else:
            entry["task"].cancel()
            _ml_persist(ml_tasks, _save_ml_levels)
            print(f"Stopped monitor for {symbol}")
        return

    if sub in ("levels", "lvl", "calc"):
        if len(args) < 2:
            print("Usage: /ml levels SYMBOL")
            return
        await _cmd_ml_levels(broker, args[1].upper())
        return

    try:
        symbol, levels, event_filters = _ml_parse_args(args)
    except ValueError as e:
        print(f"❌ {e}")
        return

    if not levels:
        print("❌ No levels given.  Usage: /ml SYMBOL LEVEL [LEVEL...]")
        return

    if symbol in ml_tasks:
        print(f"Already monitoring {symbol}. Use /ml stop {symbol} first.")
        return

    import re as _re
    async def _console_send(text: str) -> None:
        print(f"\n{_re.sub(r'[*`]', '', text)}")

    break_fn = _make_tg_break_fn()

    print(f"⏳ Loading indicators for {symbol}…")
    try:
        task, monitor, manager = await _ml_start_monitor(
            symbol, levels, event_filters, _console_send, break_fn,
        )
    except Exception as e:
        print(f"❌ Failed to start: {e}")
        return

    ml_tasks[symbol] = {
        "task":    task,
        "monitor": monitor,
        "manager": manager,
        "levels":  levels,
        "filters": list(event_filters),
    }
    _ml_persist(ml_tasks, _save_ml_levels)

    flt_str = ", ".join(f.value for f in event_filters) if event_filters else "all events"
    lvl_str = ", ".join(str(l) for l in levels)
    tg_note = "  (break alerts → Telegram)" if break_fn else ""
    print(f"✅ Monitoring {symbol} at [{lvl_str}]  filters=[{flt_str}]{tg_note}")
    print("   ⚠️  Price monitor service must be running (run_live_monitor.py) for alerts to fire.")


async def cmd_profile(args: list) -> None:
    """Fetch company profile via FundamentalsService (/stable/profile) and show candidate filter verdict."""
    if not args:
        print("Usage: /profile SYMBOL")
        return
    symbol = args[0].upper()

    _MIN_MCAP = 300_000_000
    _MIN_VOL  = 1_000_000

    print(f"\n  Fetching profile for {symbol} (stable/profile)…\n")

    try:
        from services.fundamentals_service import FundamentalsService
        svc = FundamentalsService()
        profile = await svc.get_profile(symbol)
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return

    if profile is None:
        print(f"  ❌ No profile returned for {symbol}")
        print(f"     → FMP_API_KEY may be invalid or {symbol} not found on stable/profile")
        return

    mcap = profile.market_cap or 0
    avol = profile.average_volume

    print(f"  company    : {profile.company_name}")
    print(f"  exchange   : {profile.exchange}")
    print(f"  industry   : {profile.industry or '—'}")
    print(f"  market_cap : {mcap/1e9:.2f}B  (min {_MIN_MCAP/1e6:.0f}M)  {'✅' if mcap > _MIN_MCAP else '❌'}")
    print(f"  avg_volume : {f'{avol/1e6:.2f}M' if avol else 'n/a'}  (min {_MIN_VOL/1e6:.1f}M)  "
          f"{'✅' if avol and avol > _MIN_VOL else ('⚠ n/a — treated as pass' if not avol else '❌')}")

    print()
    mcap_ok = mcap > _MIN_MCAP
    vol_ok  = avol is None or avol > _MIN_VOL
    if mcap_ok and vol_ok:
        print(f"  ✅ {symbol} PASSES candidate filter — news reactions will be monitored")
    elif not mcap_ok:
        print(f"  ❌ {symbol} FAILS — mcap {mcap/1e9:.2f}B below {_MIN_MCAP/1e6:.0f}M minimum")
    else:
        print(f"  ❌ {symbol} FAILS — avgvol {avol/1e6:.2f}M below {_MIN_VOL/1e6:.1f}M minimum")
    print()

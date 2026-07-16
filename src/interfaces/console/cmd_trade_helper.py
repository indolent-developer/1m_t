"""
interfaces.console.cmd_trade_helper

/th SYMBOL  — Deep pre-trade analysis: technicals, market context (SPY/QQQ/VIX),
              news, earnings check, and LLM synthesis verdict.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from interfaces.console.formatters import tty_colors

_YELLOW = "\033[33m"
_RESET  = "\033[0m"

# Project root is three levels up from this file (src/interfaces/console/)
_CONFIG_YAML = Path(__file__).resolve().parents[3] / "config.yaml"


async def _analyse_symbol(broker, symbol: str, print_prompt: bool = False):
    """Run the full /th pipeline for one symbol.

    Returns (report, spread_info, own_position) on success, or None if the
    symbol doesn't resolve to a live quote / the user aborts a no-broker run.
    Raises on downstream analysis failures — callers decide how to report them.
    """
    # Quick symbol existence check before doing any heavy work
    try:
        import os
        from services.price_service import FmpPriceService
        _quotes = await FmpPriceService(
            api_key=os.environ.get("FMP_API_KEY", ""), symbols=[symbol]
        ).get_quotes()
        if not _quotes.get(symbol) or not _quotes[symbol].price:
            print(f"❌ Unknown symbol: {symbol}")
            return None
    except Exception:
        pass  # network hiccup — let the main analysis fail with its own error

    # ── Spread check ──────────────────────────────────────────────────────────
    spread_pct: float | None = None
    spread_info: str = ""
    live_bid: float | None = None
    live_ask: float | None = None
    if broker is not None:
        try:
            q = await broker.get_quote(symbol)
            if q and q.ask and q.bid and float(q.ask) > 0:
                live_bid   = float(q.bid)
                live_ask   = float(q.ask)
                spread_pct = float(q.spread / q.ask * 100)
                spread_info = f"bid ${live_bid:.4f} / ask ${live_ask:.4f} / spread {spread_pct:.2f}%"
                if spread_pct > 1.5:
                    print(f"\n{'!'*60}")
                    print(f"⚠️  WARNING: {symbol}  spread {spread_pct:.2f}% exceeds 1.5% threshold — DO NOT ENTER")
                    print(f"   {spread_info}")
                    print(f"   Run /q or check liquidity before trading this stock.")
                    print(f"{'!'*60}\n")
        except Exception as e:
            print(f"  ⚠ Spread check failed ({e}) — continuing without it")
    else:
        print(f"⚠️  No broker connected — spread check skipped for {symbol}.")
        answer = input("   Continue without spread check? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return None

    print(f"⏳ Analysing {symbol} — fetching prices, market data, news…")

    from services.trade_helper_service import TradeHelperService
    from data_fetchers.financial_modelling_prep_data_fetcher import FmpDataFetcher
    from infrastructure.cache.redis_cache import RedisCache
    from services.price_history_service import PriceHistoryService
    import os as _os

    _fmp_key = _os.environ.get("FMP_API_KEY", "")
    _redis_url = _os.environ.get("REDIS_URL", "redis://localhost:6379")
    _price_history_svc = PriceHistoryService(
        fetcher=FmpDataFetcher({"api_key": _fmp_key}),
        cache=RedisCache(url=_redis_url),
        fetcher_name="fmp",
    ) if _fmp_key else None

    if _CONFIG_YAML.exists():
        svc = TradeHelperService.from_config_yaml(str(_CONFIG_YAML),
                                                  price_history_svc=_price_history_svc)
    else:
        svc = TradeHelperService(price_history_svc=_price_history_svc)

    # Fetch open positions from the active broker (same as /p)
    positions    = []
    own_position = None   # rich data for the symbol being analysed, if held
    if broker is not None:
        try:
            raw_positions = await broker.get_positions()
            try:
                acc = await broker.get_account_info()
                total_value = acc.current_value or 1.0
            except Exception:
                total_value = sum(abs(p.market_value) for p in raw_positions) or 1.0
            for p in raw_positions:
                if not p.symbol or p.quantity == 0:
                    continue
                weight_pct = abs(p.market_value) / total_value * 100
                side_str   = p.side.value if hasattr(p.side, "value") else str(p.side)
                positions.append({
                    "symbol":     p.symbol,
                    "isin":       getattr(p, "id", "") or "",
                    "side":       side_str,
                    "weight_pct": round(weight_pct, 1),
                    "sector":     "",
                })
                if p.symbol.upper() == symbol:
                    own_position = {
                        "side":        side_str,
                        "quantity":    p.quantity,
                        "avg_price":   getattr(p, "average_price", 0.0) or 0.0,
                        "market_value": abs(p.market_value),
                        "weight_pct":  round(weight_pct, 1),
                        "upnl":        getattr(p, "unrealized_pnl", 0.0) or 0.0,
                        "upnl_pct":    getattr(p, "unrealized_pnl_percentage", 0.0) or 0.0,
                    }
        except Exception as e:
            print(f"  ⚠ Could not fetch positions: {e}")

    report = await svc.analyse(symbol, current_positions=positions or None,
                               live_bid=live_bid, live_ask=live_ask,
                               own_position=own_position,
                               print_prompt=print_prompt)
    return report, spread_info, own_position


async def cmd_trade_helper(broker, args: list) -> None:
    if not args:
        print("Usage: /th SYMBOL [--pp]")
        print("  Deep pre-trade analysis — technicals + market context + news + LLM verdict.")
        print("  --pp   Print the full LLM prompt and exit (no API call).")
        print("  Requires FMP_API_KEY (prices/profile). XAI_API_KEY for LLM synthesis.")
        return

    print_prompt = "--pp" in args
    args         = [a for a in args if a != "--pp"]
    symbol       = args[0].upper()

    try:
        result = await _analyse_symbol(broker, symbol, print_prompt=print_prompt)
    except Exception as e:
        print(f"❌ {e}")
        return

    if result is None:
        return

    report, spread_info, own_position = result
    if not print_prompt:
        _print_report(report, spread_info=spread_info, own_position=own_position)


# ── Formatting ────────────────────────────────────────────────────────────────

def _pct(v: float, sign: bool = True) -> str:
    return f"{v:+.2f}%" if sign else f"{v:.2f}%"


def _position_action(verdict: str, side_bias: str, pos: dict) -> tuple[str, str]:
    """Return (action_label, reason) for an existing position given the current verdict."""
    pos_side   = pos["side"].lower()
    bias       = (side_bias or "long").lower()
    same_dir   = (bias in pos_side) or (pos_side in bias) or (
                  bias == "long"  and pos_side in ("buy",  "long")  or
                  bias == "short" and pos_side in ("sell", "short")
                 )
    upnl_pct   = pos["upnl_pct"]

    if verdict == "AVOID":
        if same_dir:
            if upnl_pct >= 10:
                return "TRIM / TAKE PROFITS", f"verdict AVOID — up {upnl_pct:+.1f}%, protect gains"
            elif upnl_pct <= -8:
                return "EXIT / CUT LOSSES", f"verdict AVOID — down {upnl_pct:+.1f}%, limit drawdown"
            else:
                return "TRIM", f"verdict AVOID — reduce exposure"
        else:
            return "HOLD / ADD", "verdict AVOID confirms short thesis — consider adding"

    elif verdict == "ENTER":
        if same_dir:
            return "ADD", f"verdict ENTER confirms direction — consider adding to position"
        else:
            return "FLIP / CLOSE", f"verdict ENTER {bias.upper()} contradicts current {pos_side.upper()} — close or flip"

    else:  # WATCH
        if same_dir:
            if upnl_pct >= 15:
                return "HOLD / PARTIAL TRIM", f"up {upnl_pct:+.1f}% — consider locking partial profits while waiting"
            return "HOLD", "verdict WATCH — no action yet, monitor for confirmation"
        else:
            return "HOLD", "verdict WATCH — short thesis unconfirmed, hold and watch"


def _print_report(r, spread_info: str = "", own_position: dict | None = None) -> None:
    GREEN, RED, RESET = tty_colors()
    YELLOW = _YELLOW

    VERDICT_COLOR = {"ENTER": GREEN, "AVOID": RED, "WATCH": YELLOW}
    vc = VERDICT_COLOR.get(r.verdict, "")

    mktcap_str = f"${r.market_cap / 1e9:.1f}B" if r.market_cap else "N/A"
    beta_str   = f"β {r.beta:.2f}" if r.beta is not None else ""

    bar  = "━" * 60
    dash = "─" * 54

    # ── Header ────────────────────────────────────────────────────────────────
    print(f"\n{bar}")
    print(f"  📊  Trade Helper — {r.symbol}   {r.timestamp}")
    print(bar)
    print(f"  {r.company_name}")
    meta_parts = [p for p in [r.sector, r.industry, mktcap_str, beta_str] if p]
    print(f"  {' | '.join(meta_parts)}")
    spread_tag = f"   {spread_info}" if spread_info else ""
    print(f"  Price: ${r.price:,.2f}   ATR(14): {r.atr:.4f}  ({r.atr_pct:.2f}%){spread_tag}")

    # Data-quality line: last intraday bar time + positions passed in
    lb = getattr(r, "last_bar_5m", None)
    pp = getattr(r, "portfolio_positions", [])
    bar_tag  = f"last 5m bar: {lb}" if lb else "last 5m bar: n/a"
    pos_tag  = f"portfolio: {', '.join(p['symbol'] for p in pp)}" if pp else "portfolio: (none passed)"
    print(f"  {YELLOW}[data] {bar_tag} | {pos_tag}{RESET}")

    # ── Open position (if held) ───────────────────────────────────────────────
    if own_position:
        pos    = own_position
        upnl_c = GREEN if pos["upnl"] >= 0 else RED
        print(f"\n  Open Position")
        print(f"  {dash}")
        print(
            f"  {pos['side'].upper():5}  {pos['quantity']:,.0f} sh"
            f"  ·  avg ${pos['avg_price']:,.2f}"
            f"  ·  mkt ${pos['market_value']:,.0f}"
            f"  ·  weight {pos['weight_pct']:.1f}%"
        )
        print(f"  Unrealized:  {upnl_c}{pos['upnl']:+,.2f}  ({pos['upnl_pct']:+.2f}%){RESET}")

        # Show AI recommendation if the LLM returned position_action, else fall back
        pa = getattr(r, "position_action", None)
        if pa and pa.get("action"):
            action   = pa["action"]
            reason   = pa.get("reason", "")
            size_pct = pa.get("size_pct")
            re_entry = pa.get("re_entry")
            ac = RED if action in ("TRIM", "EXIT") else (GREEN if action == "ADD" else YELLOW)
            size_tag = f"  {size_pct}% of position" if size_pct else ""
            print(f"  Rec:  {ac}{action}{RESET}{size_tag}  — {reason}")
            if re_entry and action in ("TRIM", "EXIT"):
                print(f"  Re-entry:  {re_entry}")
        else:
            # Deterministic fallback when LLM didn't return position_action
            action, reason = _position_action(r.verdict, getattr(r, "side_bias", "long"), pos)
            ac = RED if any(w in action for w in ("TRIM", "EXIT", "CUT", "FLIP", "CLOSE")) else (
                 GREEN if "ADD" in action else YELLOW)
            print(f"  Rec:  {ac}{action}{RESET}  — {reason}")

    # ── Market context ────────────────────────────────────────────────────────
    mkt = r.market_context
    print(f"\n  Market Context")
    print(f"  {dash}")
    spy_trend = f"{'↑ ABOVE' if mkt.spy_above_200d else '↓ BELOW'} 200d MA"
    vix_color = RED if mkt.vix > 25 else (GREEN if mkt.vix < 15 else "")
    print(
        f"  SPY   ${mkt.spy_price:.2f}"
        f"  1d {_pct(mkt.spy_ret_1d * 100)}"
        f"  5d {_pct(mkt.spy_ret_5d * 100)}"
        f"  20d {_pct(mkt.spy_ret_20d * 100)}"
        f"  {spy_trend}"
    )
    print(f"  QQQ   1d {_pct(mkt.qqq_ret_1d * 100)}  5d {_pct(mkt.qqq_ret_5d * 100)}")
    print(f"  VIX   {vix_color}{mkt.vix:.1f}  [{mkt.vix_regime.upper()}]{RESET}")

    # ── SuperTrend alignment (1m / 5m / 15m / 1d) ────────────────────────────
    tech = r.technicals
    px   = r.price
    print(f"\n  SuperTrend Alignment")
    print(f"  {dash}")
    for tf in ("1m", "5m", "15m"):
        s = r.intraday_st.get(tf, {})
        if not s:
            print(f"  {tf:>3}:  unavailable")
            continue
        d    = s.get("direction", 0)
        sc   = GREEN if d == 1 else RED
        lbl  = "LONG " if d == 1 else "SHORT"
        dist = s.get("dist_pct", 0.0)
        dc   = GREEN if dist >= 0 else RED
        flip = "  [FLIP]" if s.get("flipped") else ""
        print(f"  {tf:>3}:  {sc}{lbl}{RESET}  {s['value']:>10.4f}  dist {dc}{dist:+.2f}%{RESET}{flip}")
    st_dir  = tech.get("st_dir", 0)
    st_val  = tech.get("st_value", 0.0)
    st_dist = round((px - st_val) / px * 100, 2) if px else 0.0
    print(
        f"   1d:  {GREEN if st_dir == 1 else RED}{'LONG ' if st_dir == 1 else 'SHORT'}{RESET}"
        f"  {st_val:>10.2f}"
        f"  dist {GREEN if st_dist >= 0 else RED}{st_dist:+.2f}%{RESET}"
        + ("  [FLIP]" if tech.get("st_flipped") else "")
    )

    # ── Indicators (1d) ───────────────────────────────────────────────────────
    print(f"\n  Indicators (1d)")
    print(f"  {dash}")
    rsi_v   = tech.get("rsi", 50)
    rsi_tag = "  🔥 overbought" if rsi_v > 70 else ("  🧊 oversold" if rsi_v < 30 else "")
    adx_v   = tech.get("adx", 0)
    adx_tag = "  [trending]" if adx_v > 25 else "  [ranging]"
    ema_cx  = ""
    if tech.get("ema8") and tech.get("ema20"):
        ema_cx = f"  ({'↑ bull' if tech['ema8'] > tech['ema20'] else '↓ bear'})"
    print(f"  RSI(14):      {rsi_v:>6.1f}{rsi_tag}")
    print(f"  ADX(14):      {adx_v:>6.1f}{adx_tag}")
    print(f"  EMA 8/20/50:  {tech.get('ema8', 0):.2f} / {tech.get('ema20', 0):.2f} / {tech.get('ema50', 0):.2f}{ema_cx}")
    print(
        f"  Returns:      "
        f"1d {_pct(tech.get('ret_1d', 0))}"
        f"  5d {_pct(tech.get('ret_5d', 0))}"
        f"  20d {_pct(tech.get('ret_20d', 0))}"
        f"  50d {_pct(tech.get('ret_50d', 0))}"
    )
    print(f"  Rel Volume:   {tech.get('rel_vol', 1):.2f}x")

    # ── Earnings ──────────────────────────────────────────────────────────────
    if r.earnings_days is not None:
        days = r.earnings_days
        ec   = RED if days <= 5 else (YELLOW if days <= 10 else "")
        tag  = "  ⚠️  BLACKOUT ZONE" if days <= 3 else ""
        print(f"\n  Earnings:  {ec}in {days} day(s){RESET}{tag}")

    # ── News ──────────────────────────────────────────────────────────────────
    news_items = getattr(r, "news_items", [])
    if r.news_events or news_items:
        print(f"\n  News  (last 3 days)")
        print(f"  {dash}")
        for e in r.news_events:
            ec = RED if e["score"] < 0 else GREEN
            print(f"  {ec}⚡ HARD EVENT [{e['event'].upper()}]  score={e['score']:+.1f}{RESET}")
            print(f"     {e['headline'][:80]}")
        for item in news_items[:8]:
            ts  = item.get("ts", "")
            src = item.get("source", "")
            src_tag = f"  [{src}]" if src else ""
            print(f"  [{ts}]{src_tag}")
            print(f"    {item['title'][:78]}")
        if len(news_items) > 8:
            print(f"  … +{len(news_items) - 8} more")

    # ── Analysis ──────────────────────────────────────────────────────────────
    print(f"\n  Analysis")
    print(f"  {dash}")
    lr = getattr(r, "level_read", None)
    if lr:
        print(f"  Structure:  {lr}")
    if r.bull_case:
        print(f"  {GREEN}✅ Bull case:{RESET}")
        for b in r.bull_case:
            print(f"     • {b}")
    if r.bear_case:
        print(f"  {RED}❌ Bear case:{RESET}")
        for b in r.bear_case:
            print(f"     • {b}")
    if r.key_risks:
        print(f"  {YELLOW}⚠️  Key risks:{RESET}")
        for b in r.key_risks:
            print(f"     • {b}")

    pf = getattr(r, "portfolio_fit", None)
    if pf:
        effect_color = {"stacks_risk": RED, "hedges": GREEN, "diversifies": GREEN}.get(pf.get("effect", ""), "")
        print(f"\n  Portfolio fit:  {effect_color}{pf.get('effect', '')}{RESET}")
        if pf.get("note"):
            print(f"  {pf['note']}")

    hp = getattr(r, "hold_plan", None)
    if hp:
        carry = hp.get("carry_condition", "")
        print(f"\n  Hold plan:   {hp.get('horizon', '')}")
        if carry:
            print(f"  Carry check: {carry}")

    if r.llm_synthesis:
        print(f"\n  Synthesis:")
        for line in textwrap.wrap(r.llm_synthesis, width=70):
            print(f"  {line}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    conf_stars = {"high": "★★★", "medium": "★★☆", "low": "★☆☆"}.get(r.confidence, "★☆☆")
    tf_str   = f"  [{getattr(r, 'timeframe', None) or ''}]" if getattr(r, "timeframe", None) else ""
    sent     = getattr(r, "sentiment", None)
    sent_str = f"  sentiment {sent['score']:+.1f} — {sent['driver']}" if sent else ""
    print(f"\n{bar}")
    side_str = f" {r.side_bias.upper()}" if r.verdict in ("ENTER", "WATCH") else ""
    print(
        f"  Verdict:  {vc}{r.verdict}{side_str}{RESET}"
        f"  [{r.confidence.upper()} confidence {conf_stars}]{tf_str}"
    )
    if sent_str:
        print(f"  {sent_str}")

    # ── WATCH condition ───────────────────────────────────────────────────────
    wf = getattr(r, "watch_for", None)
    if wf and r.verdict == "WATCH":
        print(f"\n  Watch for:  {wf}")

    # ── Entry / stop / targets ────────────────────────────────────────────────
    if r.verdict == "ENTER":
        ez = getattr(r, "entry_zone", None)
        if ez:
            trigger = ez.get("trigger", "")
            print(
                f"\n  Entry zone:  ${ez.get('zone_low', 0):,.2f} – ${ez.get('zone_high', 0):,.2f}"
            )
            if trigger:
                print(f"  Trigger:     {trigger}")
        elif r.entry_ref is not None:
            print(f"\n  Entry:  ${r.entry_ref:,.2f}")

        if r.stop is not None:
            sb = getattr(r, "stop_basis", None)
            sb_str = f"  ({sb})" if sb else ""
            print(f"  Stop:   ${r.stop:,.2f}{sb_str}")

        tgts = getattr(r, "targets_list", [])
        if tgts:
            for i, t in enumerate(tgts, 1):
                basis = f"  ({t['basis']})" if t.get("basis") else ""
                print(f"  T{i}:     ${t['level']:,.2f}{basis}")
        elif r.target is not None:
            print(f"  Target: ${r.target:,.2f}")

        if r.rr is not None:
            print(f"  R:R =   {r.rr:.1f}:1")

        sz = getattr(r, "sizing", None)
        if sz and sz.get("shares", 0) > 0:
            bind_tag = f"  [capped by {sz['binding']}]" if sz.get("binding") else ""
            print(
                f"  Size:  {sz['shares']:,} shares"
                f"  ·  Notional ${sz['notional']:,.0f}"
                f"  ·  Risk ${sz['risk_dollars']:,.0f}{bind_tag}"
            )
            sect_pct = sz.get("sector_pct", 0.0)
            sect_max = sz.get("sector_max_pct", 30.0)
            sect_ok  = sz.get("sector_ok", True)
            sect_col = RED if not sect_ok else (YELLOW if sect_pct > sect_max * 0.8 else GREEN)
            sect_tag = "  ⚠️  EXCEEDS LIMIT" if not sect_ok else ""
            print(
                f"  Sector: {sz.get('sector', r.sector)}"
                f"  →  {sect_col}{sect_pct:.1f}%{RESET} of equity"
                f"  (max {sect_max:.0f}%){sect_tag}"
            )
        elif sz:
            print(f"  Size:  0 shares — {sz.get('reason', 'capped to zero')}")

    vi = getattr(r, "validation_issues", [])
    if vi:
        print(f"\n  {YELLOW}⚠  Level validation:{RESET}")
        for issue in vi:
            print(f"     • {issue}")

    print(bar)
    print()

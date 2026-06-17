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


async def cmd_trade_helper(broker, args: list) -> None:
    if not args:
        print("Usage: /th SYMBOL")
        print("  Deep pre-trade analysis — technicals + market context + news + LLM verdict.")
        print("  Requires FMP_API_KEY (prices/profile). XAI_API_KEY for LLM synthesis.")
        return

    symbol = args[0].upper()

    # Quick symbol existence check before doing any heavy work
    try:
        import os
        from services.price_service import FmpPriceService
        _quotes = await FmpPriceService(
            api_key=os.environ.get("FMP_API_KEY", ""), symbols=[symbol]
        ).get_quotes()
        if not _quotes.get(symbol) or not _quotes[symbol].price:
            print(f"❌ Unknown symbol: {symbol}")
            return
    except Exception:
        pass  # network hiccup — let the main analysis fail with its own error

    # ── Spread check ──────────────────────────────────────────────────────────
    spread_pct: float | None = None
    spread_info: str = ""
    if broker is not None:
        try:
            q = await broker.get_quote(symbol)
            if q and q.ask and q.bid and float(q.ask) > 0:
                spread_pct = float(q.spread / q.ask * 100)
                spread_info = f"bid ${float(q.bid):.4f} / ask ${float(q.ask):.4f} / spread {spread_pct:.2f}%"
                if spread_pct > 1.5:
                    print(f"\n⚠️  {symbol}  spread {spread_pct:.2f}% exceeds 1.5% threshold — DO NOT ENTER")
                    print(f"   {spread_info}")
                    print("   Run /q or check liquidity before trading this stock.\n")
                    return
        except Exception as e:
            print(f"  ⚠ Spread check failed ({e}) — continuing without it")
    else:
        print(f"⚠️  No broker connected — spread check skipped for {symbol}.")
        answer = input("   Continue without spread check? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return

    print(f"⏳ Analysing {symbol} — fetching prices, market data, news…")

    try:
        from services.trade_helper_service import TradeHelperService
        if _CONFIG_YAML.exists():
            svc = TradeHelperService.from_config_yaml(str(_CONFIG_YAML))
        else:
            svc = TradeHelperService()

        # Fetch open positions from the active broker (same as /p)
        positions = []
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
                    positions.append({
                        "symbol":     p.symbol,
                        "isin":       getattr(p, "id", "") or "",
                        "side":       p.side.value if hasattr(p.side, "value") else str(p.side),
                        "weight_pct": round(weight_pct, 1),
                        "sector":     "",
                    })
            except Exception as e:
                print(f"  ⚠ Could not fetch positions: {e}")

        report = await svc.analyse(symbol, current_positions=positions or None)
        _print_report(report, spread_info=spread_info)
    except Exception as e:
        print(f"❌ {e}")


# ── Formatting ────────────────────────────────────────────────────────────────

def _pct(v: float, sign: bool = True) -> str:
    return f"{v:+.2f}%" if sign else f"{v:.2f}%"


def _print_report(r, spread_info: str = "") -> None:
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

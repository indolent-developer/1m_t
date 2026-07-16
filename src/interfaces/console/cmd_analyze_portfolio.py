"""
interfaces.console.cmd_analyze_portfolio

/analyzep [delay_seconds] — for every symbol held in the active broker's
portfolio: fetch a lean technical + event snapshot (TradeHelperService.
portfolio_stock_analysis — no LLM call, fully separate from /th) and run a
dedicated position-management review (thesis intact/weakened/broken,
HOLD/ADD/TRIM/EXIT, invalidation/confirmation levels, event risk). Saves the
review plus the data snapshot to data/ai_analysis/portfolio/<date>/<SYMBOL>.txt,
plus a _summary.txt index. Does not touch /th's own LLM pipeline at all.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from pathlib import Path

from services.position_review_service import review_position

_OUT_ROOT = Path(__file__).resolve().parents[3] / "data" / "ai_analysis" / "portfolio"

_DEFAULT_DELAY = 2.0  # seconds between symbols — avoid hammering rate-limited APIs


def _next_out_dir(date_str: str) -> Path:
    """Return _OUT_ROOT/<date_str>, or /<date_str>-2, -3, ... if a run already happened today."""
    base = _OUT_ROOT / date_str
    if not base.exists():
        return base
    i = 2
    while (_OUT_ROOT / f"{date_str}-{i}").exists():
        i += 1
    return _OUT_ROOT / f"{date_str}-{i}"


def _build_own_positions(raw_positions: list, total_value: float) -> dict[str, dict]:
    """symbol -> own_position dict (side, quantity, avg_price, weight_pct, upnl, upnl_pct)."""
    out: dict[str, dict] = {}
    for p in raw_positions:
        if not p.symbol or p.quantity == 0:
            continue
        symbol     = p.symbol.upper()
        weight_pct = abs(p.market_value) / total_value * 100
        side_str   = p.side.value if hasattr(p.side, "value") else str(p.side)
        out[symbol] = {
            "side":         side_str,
            "quantity":     p.quantity,
            "avg_price":    getattr(p, "average_price", 0.0) or 0.0,
            "market_value": abs(p.market_value),
            "weight_pct":   round(weight_pct, 1),
            "upnl":         getattr(p, "unrealized_pnl", 0.0) or 0.0,
            "upnl_pct":     getattr(p, "unrealized_pnl_percentage", 0.0) or 0.0,
        }
    return out


def _format_hard_events(events: list[dict]) -> str:
    if not events:
        return "none"
    return "; ".join(
        f"[{e['event'].upper()}] score={e['score']:+.1f} {e['headline'][:80]}" for e in events
    )


def _format_headlines(items: list[dict]) -> str:
    """All headlines fetched (up to 20, 3-day lookback — see _get_news_sync) — not
    trimmed further here, so the LLM can correlate a big move with recent news."""
    if not items:
        return "(none)"
    return "\n".join(
        f"- [{item.get('ts', '')}] [{item.get('source', '')}] {item.get('title', '')}"
        for item in items
    )


def _pct_from_price(level: float, current_price: float) -> str:
    if not current_price:
        return ""
    pct = (level - current_price) / current_price * 100
    return f"  ({pct:+.1f}% from current ${current_price:,.2f})"


def _format_review_block(card: dict, current_price: float) -> str:
    bar = "━" * 60
    lines = [bar, "  POSITION REVIEW", bar]
    lines.append(f"  Thesis:  {card.get('thesis_status', '?')}")
    action   = card.get("action", "?")
    size_pct = card.get("size_pct")
    size_tag = f"  ({size_pct}% of shares)" if size_pct else ""
    lines.append(f"  Action:  {action}{size_tag}")
    lines.append(f"  Reason:  {card.get('reason', '')}")
    inval = card.get("invalidation_level")
    if inval is not None:
        lines.append(f"  Invalidation level:  ${inval:,.2f}{_pct_from_price(inval, current_price)}")
    conf = card.get("confirmation_level")
    if conf is not None:
        lines.append(f"  Confirmation level:  ${conf:,.2f}{_pct_from_price(conf, current_price)}")
    risk = card.get("event_risk")
    if risk:
        lines.append(f"  Event risk:  {risk}")
    moved = card.get("move_explained")
    if moved:
        lines.append(f"  Move explained:  {moved}")
    re_entry = card.get("re_entry")
    if re_entry:
        lines.append(f"  Re-entry:  {re_entry}")
    errs = card.get("_validation_errors") or []
    if errs:
        lines.append(f"  ⚠ Validation issues (verify manually): {'; '.join(errs)}")
    lines.append(bar)
    return "\n".join(lines) + "\n\n"


def _format_data_snapshot(snap, own_position: dict) -> str:
    bar = "─" * 60
    lines = [bar, f"  DATA SNAPSHOT — {snap.symbol}  ({snap.company_name})", bar]
    lines.append(f"  Sector: {snap.sector}")
    lines.append(f"  Price: ${snap.price:,.2f}   ATR(14): {snap.atr:.4f} ({snap.atr_pct:.2f}%)")
    lines.append(
        f"  Position: {own_position['side'].upper()} {own_position['quantity']:,.0f} sh"
        f"  ·  avg ${own_position['avg_price']:,.2f}"
        f"  ·  weight {own_position['weight_pct']:.1f}%"
        f"  ·  P&L {own_position['upnl_pct']:+.2f}% (${own_position['upnl']:+,.2f})"
    )
    trend = "bull" if snap.ema8 > snap.ema20 else "bear"
    lines.append(
        f"  RSI(14): {snap.rsi:.1f}   ADX(14): {snap.adx:.1f}   "
        f"EMA 8/20/50: {snap.ema8:.2f}/{snap.ema20:.2f}/{snap.ema50:.2f}  ({trend})"
    )
    lines.append(
        f"  SuperTrend (1d): {'LONG' if snap.st_dir == 1 else 'SHORT'} @ {snap.st_value:.2f}"
    )
    lines.append(
        f"  Return: 1d {snap.ret_1d:+.2f}%  5d {snap.ret_5d:+.2f}%   Rel volume: {snap.rel_vol:.1f}x"
    )
    lines.append(
        f"  Market: SPY ${snap.spy_price:,.2f} ({'above' if snap.spy_above_200d else 'below'} 200d, "
        f"1d {snap.spy_ret_1d:+.2f}%)   VIX {snap.vix:.1f} [{snap.vix_regime.upper()}]"
    )
    if snap.earnings_days is not None:
        lines.append(f"  Earnings: in {snap.earnings_days} day(s)")
    if snap.news_events:
        for e in snap.news_events:
            lines.append(f"  ⚡ HARD EVENT [{e['event'].upper()}] score={e['score']:+.1f}  {e['headline'][:80]}")
    if snap.news_items:
        lines.append("  Recent headlines:")
        for item in snap.news_items[:5]:
            lines.append(f"    [{item.get('ts', '')}] {item.get('title', '')[:78]}")
    lines.append(bar)
    return "\n".join(lines) + "\n"


async def cmd_analyze_portfolio(broker, args: list) -> None:
    if broker is None:
        print("⚠️  No broker connected — /analyzep needs open positions to know what to analyse.")
        return

    delay = _DEFAULT_DELAY
    if args and args[0].replace(".", "", 1).isdigit():
        delay = float(args[0])

    try:
        raw_positions = await broker.get_positions()
    except Exception as e:
        print(f"❌ Could not fetch positions: {e}")
        return

    try:
        acc = await broker.get_account_info()
        total_value    = acc.current_value or 1.0
        cash_available = f"${acc.cash_in_hand:,.0f}"
    except Exception:
        total_value    = sum(abs(p.market_value) for p in raw_positions) or 1.0
        cash_available = "unknown"

    own_positions = _build_own_positions(raw_positions, total_value)
    symbols = sorted(own_positions.keys())
    if not symbols:
        print("No open positions.")
        return

    xai_key = os.environ.get("XAI_API_KEY", "")
    if not xai_key:
        print("❌ XAI_API_KEY not set — /analyzep requires it for position reviews.")
        return

    from services.trade_helper_service import TradeHelperService
    _CONFIG_YAML = Path(__file__).resolve().parents[3] / "config.yaml"
    svc = (
        TradeHelperService.from_config_yaml(str(_CONFIG_YAML))
        if _CONFIG_YAML.exists() else TradeHelperService()
    )

    out_dir = _next_out_dir(dt.date.today().isoformat())
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"📊 Analysing {len(symbols)} portfolio symbol(s) → {out_dir}")

    summary_rows: list[str] = []
    for i, symbol in enumerate(symbols, 1):
        print(f"  [{i}/{len(symbols)}] {symbol} …", end=" ", flush=True)
        own_position = own_positions[symbol]

        try:
            snap = await svc.portfolio_stock_analysis(symbol)
        except Exception as e:
            print(f"❌ snapshot failed: {e}")
            summary_rows.append(f"{symbol:<8} {'ERROR':<6} snapshot failed: {e}")
            continue

        try:
            card = await review_position(
                symbol, own_position, snap.price, snap.earnings_days,
                _format_hard_events(snap.news_events),
                _format_headlines(snap.news_items),
                cash_available, xai_key,
                ret_1d=snap.ret_1d, ret_5d=snap.ret_5d, rel_vol=snap.rel_vol,
                spy_ret_1d=snap.spy_ret_1d, vix=snap.vix, vix_regime=snap.vix_regime,
            )
        except Exception as e:
            card = {"action": "?", "_validation_errors": [f"LLM call failed: {e}"]}

        fpath = out_dir / f"{symbol}.txt"
        fpath.write_text(_format_review_block(card, snap.price) + _format_data_snapshot(snap, own_position))

        action = card.get("action", "n/a")
        print(f"{action} → {fpath.name}")
        note = card.get("event_risk") or card.get("reason") or (card.get("_validation_errors") or [""])[0]
        summary_rows.append(f"{symbol:<8} {action:<6} {note}")

        if i < len(symbols) and delay > 0:
            await asyncio.sleep(delay)

    summary_path = out_dir / "_summary.txt"
    header = f"{'SYMBOL':<8} {'ACTION':<6} NOTE"
    summary_path.write_text(
        f"Portfolio analysis — {dt.date.today().isoformat()}\n{header}\n"
        + "\n".join(summary_rows) + "\n"
    )
    print(f"\n✅ Done. Summary → {summary_path}")

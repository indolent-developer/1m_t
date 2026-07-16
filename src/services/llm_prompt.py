"""
llm_prompt.py — final pre-entry analysis prompt for the trade engine.

Usage:
    from llm_prompt import SYSTEM_PROMPT, build_user_prompt, validate_card
    user = build_user_prompt(ctx)          # ctx = dict of all fields below
    card = call_llm(SYSTEM_PROMPT, user)   # your API call, parse JSON
    card = validate_card(card, ctx["price"], ctx["atr"])

Design notes (from prompt review):
  - Schema ordered reasoning-first, verdict-last (avoids zero-shot JSON trap)
  - risk_reward NOT in schema — computed deterministically in validate_card
  - System prompt is static => enable prompt caching on it
"""
from __future__ import annotations
import json

_SYSTEM_BASE = """You are an experienced discretionary trader. {holding_context}

Style: intraday momentum with structure-based entries. Prefer confirmation over anticipation. A setup needs roughly 1.5R or better to be worth taking. WATCH is not a hedge — it must come with a specific, observable trigger. ENTER and AVOID are both legitimate; do not default to the middle.

Use your full judgment — the data below is evidence, not rules. Worth noting:
- Hard event scale: +/-1 moderate (downgrade), +/-2 severe (offering, legal), +/-3 existential (bankruptcy).
- 5m vs 1h structure agreement or divergence.
- Price location vs obvious levels in the bars; volume confirming or not.
- Market regime as tailwind/headwind, not a veto.
- Portfolio overlap: the corr numbers are computed for you, but judge the
  thematic/factor overlap they can't see (shared catalyst, same supply chain,
  candidate behaving as a leveraged proxy of an existing holding). If the
  trade mostly stacks risk you already hold, that should lower conviction.

Derive entry, stop, and target levels FROM THE BARS (swing highs/lows, consolidation edges, gaps) — not generic ATR math. If no attractive entry exists at current price, verdict WATCH with the level that changes that.

Sentiment score = news/headlines. Let price action override only when it is screaming (e.g. gap down on heavy volume through prior support).

Timeframe: default intraday, position closed by EOD. Mandatory EOD close if earnings or a significant event is imminent. Never hold a speculative position over a weekend. "swing" (1-3 days) ONLY on genuinely high conviction: daily AND intraday structure aligned, plus a catalyst or clean daily setup supporting continuation.

Fill the output schema in the exact field order given — analysis fields first, verdict last, so your conclusion follows from your written analysis.

Output ONLY valid JSON matching the schema. No preamble, no markdown."""


def build_system_prompt(own_position: dict | None = None) -> str:
    """Return the system prompt, framed for position management when we hold the symbol."""
    if own_position:
        side    = own_position["side"].upper()
        qty     = int(own_position["quantity"])
        avg     = own_position["avg_price"]
        pct     = own_position["upnl_pct"]
        upnl    = own_position["upnl"]
        weight  = own_position["weight_pct"]
        ctx = (
            f"We are MANAGING an existing {side} position: "
            f"{qty} sh, avg ${avg:.2f}, weight {weight:.1f}%, "
            f"P&L {pct:+.1f}% (${upnl:+.0f}). "
            f"Your primary deliverable is `position_action`: give a clear "
            f"HOLD / ADD / TRIM / EXIT call with the size to act on and a crisp reason. "
            f"If trimming or exiting, state what level or condition would let you re-enter."
        )
    else:
        ctx = "We hold NO position in this name. Assess whether an entry is attractive now, which direction, and at what levels."
    return _SYSTEM_BASE.format(holding_context=ctx)


# Keep for backwards compatibility (used when no position is held)
SYSTEM_PROMPT = build_system_prompt()


USER_TEMPLATE = """=== STOCK ===
{symbol} | {company} | {sector} | {mkt_cap} | beta {beta}
{description_300}

=== NOW ===
{timestamp} ET | {weekday} | {session_phase} | bars through {last_bar_time}

=== LIVE QUOTE ===
{live_quote}{bar_staleness_warn}

=== DAILY ===
Px {price} | ATR14 {atr} ({atr_pct}%) | RSI {rsi} | ADX {adx} {trend_label}
ST daily: {st_dir} @ {st_value}, flip today: {st_flip}
EMA 8/20/50: {ema8}/{ema20}/{ema50}
Ret 1d/5d/20d/50d: {r1d}/{r5d}/{r20d}/{r50d}%
RelVol: {rel_vol}x

=== INTRADAY ST ===
{intraday_st}

=== 5M BARS (20, oldest first) ===
time,o,h,l,c,v
{bars_5m_csv}

=== 1H BARS (20, oldest first) ===
time,o,h,l,c,v
{bars_1h_csv}

=== VOLUME ===
Cur bar {vol_cur} vs prior {vol_prev} | session {session_vol_ratio}x same-time avg

=== MARKET ===
SPY {spy_px} 1d/5d/20d {spy_r1d}/{spy_r5d}/{spy_r20d}%, {spy_ma200} 200d | QQQ 1d/5d {qqq_r1d}/{qqq_r5d}%
VIX {vix} ({vix_regime})

=== PORTFOLIO (current open positions) ===
{portfolio_lines}
Avg 60d corr of candidate to book: {avg_corr} | Net book bias: {net_bias}

=== OPEN POSITION (THIS SYMBOL) ===
{open_position_section}

=== EARNINGS ===
{days_to_earnings} days{blackout_flag}

=== MACRO CALENDAR (US, today+5d) ===
{econ_calendar}

=== ANALYST RATINGS (14d) ===
{analyst_ratings}

=== HARD EVENTS ===
{hard_events}

=== HEADLINES (3d) ===
{headlines}

=== SCHEMA (fill in this exact field order) ===
{{
 "sentiment": {{"score": -1.0to1.0, "driver": "phrase, news-based"}},
 "level_read": "1 sentence — key structure visible in the bars",
 "bull_case": ["max 3 short points"],
 "bear_case": ["max 3 short points"],
 "entry": {{"zone_low": 0.0, "zone_high": 0.0, "trigger": "what confirms the entry"}},
 "stop": {{"level": 0.0, "basis": "structural reason"}},
 "targets": [{{"level": 0.0, "basis": ""}}, {{"level": 0.0, "basis": ""}}],
 "watch_for": "condition that upgrades WATCH to ENTER, else null",
 "portfolio_fit": {{"effect": "diversifies|stacks_risk|hedges|neutral",
                   "note": "1 sentence — thematic/factor overlap with holdings, incl. anything the correlation number misses"}},
 "hold_plan": {{"horizon": "EOD|1-3 days",
               "carry_condition": "must be true at 15:50 ET to hold overnight, else flatten"}},
 "position_action": {{"action": "HOLD|ADD|TRIM|EXIT", "size_pct": 0-100,
                      "reason": "1 sentence", "re_entry": "level or condition to re-enter after trim/exit, else null"}},
 "verdict": "ENTER|WATCH|AVOID",
 "side_bias": "long|short|neutral",
 "confidence": "high|medium|low",
 "timeframe": "scalp|intraday|swing",
 "synthesis": "2-3 sentences, honest read incl. what the data misses"
}}
Note: fill position_action only when an open position is shown above; otherwise set it to null."""


def format_open_position_section(own_position: dict | None) -> str:
    """Build the OPEN POSITION section for the prompt, or '(none)' when flat."""
    if not own_position:
        return "(none — fresh entry analysis)"
    p = own_position
    return (
        f"{p['side'].upper()} {int(p['quantity'])} sh"
        f" | avg ${p['avg_price']:.2f}"
        f" | mkt ${p['market_value']:,.0f}"
        f" | weight {p['weight_pct']:.1f}%"
        f" | P&L {p['upnl_pct']:+.1f}% (${p['upnl']:+.0f})"
    )


def format_portfolio_lines(positions: list[dict],
                           corrs: dict[str, float]) -> tuple[str, str, str]:
    """Build the PORTFOLIO section inputs from engine data.

    positions: [{symbol, side, weight_pct, sector}, ...]
    corrs: {symbol: 60d return correlation of candidate vs that holding}
    Returns (portfolio_lines, avg_corr_str, net_bias_str).
    """
    if not positions:
        return "(book is flat)", "n/a", "flat"
    lines = []
    for p in positions:
        c = corrs.get(p["symbol"])
        c_str   = f"{c:+.2f}" if c is not None else "n/a"
        sector  = p.get("sector", "")
        sec_tag = f" | {sector}" if sector else ""
        lines.append(f"{p['symbol']} {p['side']} {p['weight_pct']:.1f}%{sec_tag} | corr {c_str}")
    valid = [v for v in corrs.values() if v is not None]
    avg   = f"{sum(valid)/len(valid):+.2f}" if valid else "n/a"
    net_long  = sum(p["weight_pct"] for p in positions if p["side"] == "long")
    net_short = sum(p["weight_pct"] for p in positions if p["side"] == "short")
    net  = net_long - net_short
    bias = f"{'long' if net > 0 else 'short' if net < 0 else 'flat'} {abs(net):.0f}% net"
    return "\n".join(lines), avg, bias


def build_user_prompt(ctx: dict) -> str:
    """ctx keys must match the placeholders in USER_TEMPLATE."""
    return USER_TEMPLATE.format(**ctx)


def parse_card(raw: str) -> dict:
    """Strip accidental fences and parse the model's JSON."""
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(cleaned)


def validate_card(card: dict, price: float, atr: float) -> dict:
    """Deterministic post-processing: compute R/R, sanity-check levels.

    Any issue appended to card['validation_issues'] means the model's levels
    are structurally suspect — discard or re-query rather than trade on them.
    """
    issues = []
    try:
        e    = (card["entry"]["zone_low"] + card["entry"]["zone_high"]) / 2
        stop = card["stop"]["level"]
        t1   = card["targets"][0]["level"]
        side = card.get("side_bias", "neutral")

        card["risk_reward"] = round(abs(t1 - e) / max(abs(e - stop), 1e-9), 2)

        if side == "long" and not (stop < e < t1):
            issues.append("levels not ordered for long (need stop < entry < target)")
        if side == "short" and not (t1 < e < stop):
            issues.append("levels not ordered for short (need target < entry < stop)")
        if abs(e - price) > 3 * atr:
            issues.append(f"entry zone {e:.2f} implausibly far from price {price:.2f}")
        if abs(e - stop) < 0.15 * atr:
            issues.append("stop suspiciously tight (<0.15 ATR)")
        if card["risk_reward"] < 1.5 and card.get("verdict") == "ENTER":
            issues.append(f"ENTER verdict with R/R {card['risk_reward']} below 1.5")
    except (KeyError, IndexError, TypeError, ZeroDivisionError) as exc:
        issues.append(f"schema/level extraction failed: {exc}")

    card["validation_issues"] = issues
    return card

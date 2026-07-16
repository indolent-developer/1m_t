"""
prompts.post_market_morning

Templates and formatters for the three-run post-market morning routine.

Run 1 — 07:00 DE / 01:00 ET  — Overnight Thesis Check
Run 2 — 14:00 DE / 08:00 ET  — Pre-Market Decision Run
Run 3 — 16:10 DE / 10:05 ET  — Opening Confirmation

Usage:
    from prompts.post_market_morning import build_prompt
    text = build_prompt(1, ticker_data_text)
    text = build_prompt(2, ticker_data_text, prev_output=run1_text)
"""
from __future__ import annotations

import math
from typing import Optional

try:
    import pandas as pd
except ImportError:
    pd = None  # type: ignore

# ── Prompt templates ──────────────────────────────────────────────────────────

PROMPT_1 = """\
ROLE
You are a senior equity analyst and former prop-desk trader. It is ~1:00 am ET
(overnight). The regular after-hours session has closed and pre-market has NOT
opened; the only live prices are thin overnight ECN trading and index futures.
Your job is NOT to read price action — it is to judge whether each post-market
mover has a REAL, DURABLE catalyst worth tracking into the open, and to flag any
overnight development that would kill the gap.

INPUTS
- Last night's movers with technicals and 5-day OHLC (see MARKET DATA below).
- Use web access to retrieve per name: the exact catalyst, any overnight news
  (offerings, dilution, downgrades, guidance, regulatory), float, short interest.
  Note index futures (ES/NQ) and VIX direction for the macro block.

FRAMEWORK (per ticker)
1. Catalyst: identify it exactly. Classify DURABLE (earnings/guidance/M&A/regulatory/
   contract — repricing the business) vs MECHANICAL (squeeze, low-float momentum,
   sympathy, single headline).
2. Gap-killers: scan for overnight secondary offering, ATM dilution, downgrade, or
   "sell the news" risk — these flip a continuation into a fade.
3. Structure: float + short interest (squeeze-prone & fade-risky?) and magnitude
   (>20% = exhaustion risk).
4. Durability: would this catalyst still matter at 9:30 ET?

OUTPUT
Return ONLY one valid JSON object. No markdown fences, no explanation, no text
outside the JSON. The schema below is the contract — follow it exactly.

SCHEMA
{{
  "schema_version": "1.0",
  "run": {{
    "stage": 1,
    "label": "overnight_thesis_check",
    "generated_at_et": "<ISO-8601 datetime in America/New_York>",
    "session": "post_market",
    "is_actionable": false,
    "gating_policy": "for_watchlist_only_no_trading"
  }},
  "macro": {{
    "regime": "<risk_on|risk_off|neutral>",
    "spx_chg_pct": "<float|null>",
    "es_chg_pct": "<float|null>",
    "nq_chg_pct": "<float|null>",
    "vix": "<float|null>",
    "oil_trend": "<rising|falling|flat|null>",
    "bias": "<bullish|bearish|neutral>",
    "summary": "<one sentence>"
  }},
  "universe": [
    {{
      "symbol": "<TICKER>",
      "bias": "<long|short|flat>",
      "conviction": "<track_continuation|track_fade|watch|skip>",
      "verdict": null,
      "action": null,
      "score": "<integer 0-100>",
      "catalyst": {{
        "classification": "<durable|mechanical|unknown>",
        "type": "<earnings|guidance|M&A|contract|regulatory|squeeze|sympathy|single_headline|other>",
        "description": "<one sentence>",
        "confirmed": "<true|false>"
      }},
      "gap_killers": ["<string, or empty array []>"],
      "structure": {{
        "trend": "<uptrend|downtrend|range>",
        "note": "<one sentence>",
        "volume_signal": "<confirming|declining|neutral>",
        "behavior": "<extension_risk|durable|choppy|unknown>"
      }},
      "levels": {{
        "prior_close": "<float — use close from market data>",
        "premarket_high": null,
        "premarket_low": null,
        "current_price": null,
        "opening_range_high": null,
        "opening_range_low": null,
        "vwap": null,
        "above_vwap": null,
        "gap_status": null
      }},
      "confirm_at_premarket": ["<1-3 specific things to verify at Run 2>"],
      "signal": {{
        "direction": "<long|short|none>",
        "entry": {{"condition": null, "price": null}},
        "invalidation": {{"condition": "<string|null>", "price": "<float|null>"}},
        "require_catalyst_confirmation": "<true|false>"
      }},
      "pct_vs_prior_close": null,
      "holding_ah_level": null,
      "catalyst_confirmed": null,
      "changed_vs_run1": null,
      "changed_vs_run2": null,
      "notes": "<string>"
    }}
  ]
}}

FIELD RULES
- run.is_actionable: always false at stage 1 — never trade on this run alone
- conviction: "track_continuation" (durable catalyst, clean structure),
  "track_fade" (mechanical or gap-killer present),
  "watch" (catalyst plausible but needs pre-market confirmation),
  "skip" (no real catalyst or too risky to monitor)
- score: 0–100; rate catalyst strength and durability only — not the size of the move
- levels.prior_close: use "close" from market data (regular session close)
- All other levels fields: null at this stage — filled in later runs
- confirm_at_premarket: 1–3 specific, concrete things to check at 8 am ET
- signal.entry: always {{"condition": null, "price": null}} — no entry without pre-market data
- gap_killers: [] if none found; never omit the field

RULES
- Thesis strength = conviction in the CATALYST, not the size of the move.
- Do NOT anchor to overnight prices — they are illiquid and unreliable.
- Never fabricate facts; use null for unknowns; note "unconfirmed" in descriptions.
- This run sets the watchlist only — it is not a trade call.
- Return ONLY valid JSON. No trailing commas. No comments inside JSON.

TICKERS: {ticker_list}

MARKET DATA:
{ticker_data}
"""

PROMPT_2 = """\
ROLE
You are a senior equity analyst and former prop-desk trader. It is ~8:00 am ET —
US pre-market is liquid and the open (9:30 ET) is ~90 minutes away. Update the
overnight thesis for each ticker based on live pre-market data, and decide whether
the post-market gain will CONTINUE or FADE at the open. At this hour the tape is
tradeable — trust it.

INPUTS
- Last night's post-market data and technicals per name (see MARKET DATA below).
- MY OVERNIGHT THESIS (Run 1 output — pasted below).
- Use web access to retrieve NOW: current pre-market price & volume, the pre-market
  HIGH and LOW, news/analyst notes published overnight (upgrades, downgrades, target
  changes, secondary offerings), and the macro tape (ES/NQ futures, VIX, 10Y,
  pre-9:30 economic calendar).

FRAMEWORK (per ticker)
1. Tape confirmation: compare current pre-market price to (a) prior close and
   (b) last night's post-market high. Holding/extending = bullish; fading back
   toward prior close = warning.
2. Volume: is real pre-market volume confirming, or did the move go quiet?
3. News delta vs overnight: any fresh catalyst or gap-killer since Run 1?
   (A pre-market offering is a classic fade trigger.)
4. Magnitude: over-extended gaps fade; flag >20% movers.
5. Crowding: euphoric, retail-driven hype = contrarian fade risk.
6. Macro fit: does today's tape support holding gains or fading them?

OUTPUT
Return ONLY one valid JSON object. No markdown fences, no explanation, no text
outside the JSON. The schema below is the contract — follow it exactly.

SCHEMA
{{
  "schema_version": "1.0",
  "run": {{
    "stage": 2,
    "label": "premarket_decision",
    "generated_at_et": "<ISO-8601 datetime in America/New_York>",
    "session": "pre_market",
    "is_actionable": "<true if score >= 70 AND conviction == continue, else false>",
    "gating_policy": "act_on_score_gte_70"
  }},
  "macro": {{
    "regime": "<risk_on|risk_off|neutral>",
    "spx_chg_pct": "<float|null>",
    "es_chg_pct": "<float|null>",
    "nq_chg_pct": "<float|null>",
    "vix": "<float|null>",
    "oil_trend": "<rising|falling|flat|null>",
    "bias": "<bullish|bearish|neutral>",
    "summary": "<one sentence>"
  }},
  "universe": [
    {{
      "symbol": "<TICKER>",
      "bias": "<long|short|flat>",
      "conviction": "<continue|fade|mixed>",
      "verdict": null,
      "action": null,
      "score": "<integer 0-100>",
      "catalyst": {{
        "classification": "<durable|mechanical|unknown>",
        "type": "<earnings|guidance|M&A|contract|regulatory|squeeze|sympathy|single_headline|other>",
        "description": "<one sentence>",
        "confirmed": "<true|false>"
      }},
      "gap_killers": ["<string, or empty array []>"],
      "structure": {{
        "trend": "<uptrend|downtrend|range>",
        "note": "<one sentence>",
        "volume_signal": "<confirming|declining|neutral>",
        "behavior": "<extension_risk|durable|choppy|unknown>"
      }},
      "levels": {{
        "prior_close": "<float — use close from market data>",
        "premarket_high": "<float from web|null>",
        "premarket_low": "<float from web|null>",
        "current_price": null,
        "opening_range_high": null,
        "opening_range_low": null,
        "vwap": null,
        "above_vwap": null,
        "gap_status": null
      }},
      "confirm_at_premarket": null,
      "signal": {{
        "direction": "<long|short|none>",
        "entry": {{"condition": "<string|null>", "price": "<float|null>"}},
        "invalidation": {{"condition": "<string|null>", "price": "<float|null>"}},
        "require_catalyst_confirmation": "<true|false>"
      }},
      "pct_vs_prior_close": "<((current_premarket_price / prior_close) - 1) * 100 | null>",
      "holding_ah_level": "<true if pre-mkt price >= last night's postmarket high | false | null>",
      "catalyst_confirmed": "<true if original catalyst still stands | false | null>",
      "changed_vs_run1": "<one sentence describing the flip, or null if no material change>",
      "changed_vs_run2": null,
      "notes": "<string>"
    }}
  ]
}}

FIELD RULES
- conviction: "continue" (tape + volume confirm the thesis),
  "fade" (price fading / gap-killer appeared / volume dried up),
  "mixed" (conflicting signals — not clear yet)
- score: 0–100; conviction the thesis holds through the open; 80+ = durable + confirmed;
  60–79 = solid with one risk; 40–59 = conflicting; <40 = likely fade
- run.is_actionable: true only when score >= 70 AND conviction == "continue"
- levels.premarket_high / premarket_low: live values from web access; null if unavailable
- levels.prior_close: use "close" from market data
- pct_vs_prior_close: number (e.g. 4.2 means +4.2%); null if price unavailable
- holding_ah_level: compare current pre-market price to postmarket_close from data
- catalyst_confirmed: false if any gap-killer (offering, downgrade, reversal) appeared
- changed_vs_run1: null if no change; one sentence if conviction or bias flipped
- confirm_at_premarket: null (stage 1 field — do not populate)
- verdict / action: null (stage 3 fields — do not populate)
- signal.entry: provide specific level or condition that triggers entry at the open

RULES
- Prior close and pre-market high become the reference points for Run 3 — be precise.
- Never fabricate prices; separate confirmed from inferred; flag thin/low-float traps.
- Return ONLY valid JSON. No trailing commas. No comments inside JSON.

TICKERS: {ticker_list}

MARKET DATA:
{ticker_data}

RUN 1 OVERNIGHT THESIS (paste below):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{prev_run_output}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

PROMPT_3 = """\
ROLE
You are a senior equity analyst and former prop-desk trader running an EXECUTION
read. It is ~10:05 am ET. The session opened at 9:30 and the first two 15-minute
candles (9:30–9:45, 9:45–10:00) have printed, establishing the OPENING RANGE. You
are no longer predicting — you are confirming whether each gap is real and issuing
an action with levels.

INPUTS
- MY PRE-MARKET CALL (Run 2 output — pasted below): conviction, score, prior
  close, pre-market high/low, signal levels per name.
- TODAY'S SESSION DATA is pre-computed and embedded in MARKET DATA below for each
  ticker: 5-min OHLC bars with cumulative VWAP, opening-range high/low (9:30–10:00
  ET), current price (last bar close), gap status vs prior close, and 30-min volume.
  Read these values directly — do NOT fetch price, VWAP, OR levels, or gap status
  from the web; they are already calculated.
- Use web access ONLY for: macro snapshot (ES/NQ futures, VIX, sector ETFs) and
  any fresh catalyst or breaking news that appeared after Run 2.

FRAMEWORK (per ticker)
1. Gap status: holding the gap, partially filled, or fully filled to prior close?
2. VWAP: trading ABOVE VWAP (buyers in control = continuation) or lost VWAP early
   (fade warning)?
3. Opening range: broke ABOVE the OR high on volume (breakout/continuation), broke
   BELOW the OR low (failed gap/exit), or stuck inside (chop, no edge)?
4. Volume: first-30-min volume confirming, or drying up?
5. Vs the pre-market call: did the open confirm or invalidate Run 2's conviction?

OUTPUT
Return ONLY one valid JSON object. No markdown fences, no explanation, no text
outside the JSON. The schema below is the contract — follow it exactly.

SCHEMA
{{
  "schema_version": "1.0",
  "run": {{
    "stage": 3,
    "label": "opening_confirmation",
    "generated_at_et": "<ISO-8601 datetime in America/New_York>",
    "session": "regular_open",
    "is_actionable": true,
    "gating_policy": "execute_on_confirmed_continuation_only"
  }},
  "macro": {{
    "regime": "<risk_on|risk_off|neutral>",
    "spx_chg_pct": "<float|null>",
    "es_chg_pct": "<float|null>",
    "nq_chg_pct": "<float|null>",
    "vix": "<float|null>",
    "oil_trend": "<rising|falling|flat|null>",
    "bias": "<bullish|bearish|neutral>",
    "summary": "<one sentence>"
  }},
  "universe": [
    {{
      "symbol": "<TICKER>",
      "bias": "<long|short|flat>",
      "conviction": null,
      "verdict": "<confirmed_continuation|fading|choppy>",
      "action": "<hold|add|trim|avoid|none>",
      "score": "<integer 0-100>",
      "catalyst": {{
        "classification": "<durable|mechanical|unknown>",
        "type": "<earnings|guidance|M&A|contract|regulatory|squeeze|sympathy|single_headline|other>",
        "description": "<one sentence>",
        "confirmed": "<true|false>"
      }},
      "gap_killers": ["<string, or empty array []>"],
      "structure": {{
        "trend": "<uptrend|downtrend|range>",
        "note": "<one sentence>",
        "volume_signal": "<confirming|declining|neutral>",
        "behavior": "<extension_risk|durable|choppy|unknown>"
      }},
      "levels": {{
        "prior_close": "<float>",
        "premarket_high": "<float|null>",
        "premarket_low": "<float|null>",
        "current_price": "<float from web — live price now>",
        "opening_range_high": "<float — high of 9:30–10:00 ET range|null>",
        "opening_range_low": "<float — low of 9:30–10:00 ET range|null>",
        "vwap": "<float — current session VWAP|null>",
        "above_vwap": "<true if current_price > vwap | false | null>",
        "gap_status": "<holding|partial_fill|full_fill|null>"
      }},
      "confirm_at_premarket": null,
      "signal": {{
        "direction": "<long|short|none>",
        "entry": {{"condition": "<string|null>", "price": "<float|null>"}},
        "invalidation": {{"condition": "<string|null>", "price": "<float|null>"}},
        "require_catalyst_confirmation": "<true|false>"
      }},
      "pct_vs_prior_close": "<((current_price / prior_close) - 1) * 100 | null>",
      "holding_ah_level": "<true|false|null>",
      "catalyst_confirmed": "<true|false|null>",
      "changed_vs_run1": "<carry forward from Run 2 or update if needed | null>",
      "changed_vs_run2": "<one sentence describing what changed from Run 2, or null>",
      "notes": "<string>"
    }}
  ]
}}

FIELD RULES
- conviction: always null at stage 3 — replaced by verdict
- verdict: "confirmed_continuation" (above VWAP, gap held, OR intact or broken upward),
  "fading" (below VWAP, filling gap, or broke OR low),
  "choppy" (stuck inside OR, no clear edge yet)
- action: "hold" (in position, thesis intact), "add" (high conviction — add on OR-break),
  "trim" (reduce position — losing conviction), "avoid" (do not enter),
  "none" (skip / already closed)
- run.is_actionable: always true at stage 3
- levels.current_price: last 5-min bar close from embedded session data
- levels.opening_range_high/low: from embedded session data (9:30–10:00 ET window)
- levels.vwap: cumulative session VWAP at last bar, from embedded session data
- levels.above_vwap: derive from embedded current_price vs vwap
- levels.gap_status: from embedded session data — "holding" (price at or above PM
  close), "partial_fill" (above prior close but below PM close), "full_fill" (at or
  below prior close)
- pct_vs_prior_close: compute from embedded current_price and prior_close
- changed_vs_run2: null if no change; one sentence if verdict/action differs from Run 2 conviction
- confirm_at_premarket: null (stage 1 field — do not populate)

RULES
- This is a confirmation read — favor what the tape is DOING over what you expected.
  If Run 2 said "continue" but it's below VWAP and filling the gap, verdict = "fading".
- Be concrete with every level — these are live stop and entry references.
- Never fabricate prices; set any unavailable level to null.
- Return ONLY valid JSON. No trailing commas. No comments inside JSON.

TICKERS: {ticker_list}

MARKET DATA:
{ticker_data}

RUN 2 PRE-MARKET CALL (paste below):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{prev_run_output}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

_TEMPLATES = {1: PROMPT_1, 2: PROMPT_2, 3: PROMPT_3}

_PREV_RUN_PLACEHOLDER = "[PASTE PREVIOUS RUN OUTPUT HERE]"


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fv(v, decimals: int = 2) -> str:
    """Format a numeric value or return '—' if missing."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:.{decimals}f}"

def _fvol(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return f"{v:.0f}"

def _fmktcap(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9:  return f"${v/1e9:.2f}B"
    return f"${v/1e6:.0f}M"

def _arrow(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return "▲" if v >= 0 else "▼"


def format_ticker_block(
    row: "pd.Series",
    ohlc_5d: list[dict] | None = None,
    intraday: dict | None = None,
    news: list[dict] | None = None,
) -> str:
    """
    Format one scanner row into a readable text block for prompt injection.

    ohlc_5d:  list of dicts (oldest first) with keys: date, open, high, low, close, volume
    intraday: dict with keys: bars (list of 5-min bar dicts with time/OHLC/volume/vwap),
              vwap, or_high, or_low, current_price, above_vwap, gap_status, vol_30m
    """
    symbol = row.get("name", "?")
    desc   = row.get("description", "")
    sector = row.get("sector", "")

    pm_chg  = row.get("postmarket_change")
    pm_vol  = row.get("postmarket_volume")
    pm_close = row.get("postmarket_close")
    close   = row.get("close")
    day_chg = row.get("change")
    vol     = row.get("volume")
    avg10   = row.get("average_volume_10d_calc")
    avg30   = row.get("average_volume_30d_calc")
    relvol  = row.get("relative_volume_intraday|5")
    mktcap  = row.get("market_cap_basic")

    rsi    = row.get("RSI")
    adx    = row.get("ADX")
    adx_p  = row.get("ADX+DI")
    adx_m  = row.get("ADX-DI")
    ema20  = row.get("EMA20")
    ema50  = row.get("EMA50")

    pm_arrow = _arrow(pm_chg)
    day_arrow = _arrow(day_chg)

    lines = [
        f"{'━'*55}",
        f"{symbol}  |  {desc}  |  {sector}",
        f"{'━'*55}",
        (
            f"POST-MARKET:  {pm_arrow}{_fv(abs(pm_chg) if pm_chg is not None else None)}%"
            f"   PM Vol: {_fvol(pm_vol)}"
            + (f"   PM Close: ${_fv(pm_close)}" if pm_close else "")
        ),
        (
            f"Close: ${_fv(close)}   Day: {day_arrow}{_fv(abs(day_chg) if day_chg is not None else None)}%"
            f"   Vol: {_fvol(vol)}   Avg10d: {_fvol(avg10)}   Avg30d: {_fvol(avg30)}"
            + (f"   RelVol: {_fv(relvol)}x" if relvol else "")
        ),
        f"MCap: {_fmktcap(mktcap)}",
        (
            f"TECHNICALS:  RSI: {_fv(rsi)}   ADX: {_fv(adx)}"
            f"  (+DI: {_fv(adx_p)}, -DI: {_fv(adx_m)})"
            f"   EMA20: ${_fv(ema20)}   EMA50: ${_fv(ema50)}"
        ),
    ]

    if ohlc_5d:
        lines.append("")
        lines.append("5-DAY OHLC:")
        lines.append(f"  {'Date':<14} {'Open':>7} {'High':>7} {'Low':>7} {'Close':>7} {'Volume':>9}")
        for bar in ohlc_5d:
            marker = "  ← today" if bar.get("today") else ""
            lines.append(
                f"  {bar.get('date',''):<14}"
                f" {_fv(bar.get('open')):>7}"
                f" {_fv(bar.get('high')):>7}"
                f" {_fv(bar.get('low')):>7}"
                f" {_fv(bar.get('close')):>7}"
                f" {_fvol(bar.get('volume')):>9}"
                f"{marker}"
            )

    if intraday and intraday.get("bars"):
        bars     = intraday["bars"]
        or_high  = intraday.get("or_high")
        or_low   = intraday.get("or_low")
        vwap     = intraday.get("vwap")
        cur      = intraday.get("current_price")
        above    = intraday.get("above_vwap")
        gap_st   = intraday.get("gap_status")
        vol_30m  = intraday.get("vol_30m")

        lines.append("")
        lines.append("TODAY'S 5-MIN SESSION (ET):")
        lines.append(
            f"  {'Time':<7} {'Open':>7} {'High':>7} {'Low':>7} {'Close':>7} {'Volume':>9}  {'VWAP':>8}"
        )
        for i, bar in enumerate(bars):
            marker = "  ← last" if i == len(bars) - 1 else ""
            lines.append(
                f"  {bar.get('time', ''):<7}"
                f" {_fv(bar.get('open')):>7}"
                f" {_fv(bar.get('high')):>7}"
                f" {_fv(bar.get('low')):>7}"
                f" {_fv(bar.get('close')):>7}"
                f" {_fvol(bar.get('volume')):>9}"
                f"  {_fv(bar.get('vwap')):>8}"
                f"{marker}"
            )

        lines.append("")
        lines.append(
            f"OPENING RANGE (9:30–10:00):  OR-High: {_fv(or_high)}  |  OR-Low: {_fv(or_low)}"
        )
        above_str = "YES" if above is True else ("NO" if above is False else "—")
        lines.append(
            f"VWAP: ${_fv(vwap)}  |  Current: ${_fv(cur)}  |  Above VWAP: {above_str}"
        )
        gap_label   = (gap_st or "—").upper().replace("_", " ")
        prior_close = row.get("close")
        lines.append(f"Gap Status: {gap_label}  (prior close: ${_fv(prior_close)})")
        lines.append(f"30-min Volume: {_fvol(vol_30m)}")

    if news:
        lines.append("")
        lines.append("NEWS (last 2d):")
        for item in news[:5]:
            ts  = item.get("ts", "")
            src = item.get("source", "")
            hdr = f"  [{ts}] {src}" if src else f"  [{ts}]"
            lines.append(hdr)
            lines.append(f"    {item.get('title', '')[:110]}")

    return "\n".join(lines)


def format_ticker_data(rows: list[dict], ohlc_map: dict[str, list[dict]] | None = None) -> str:
    """
    Format a list of ticker row dicts (or a DataFrame iterated via iterrows)
    into the full ticker data text block.

    ohlc_map: symbol → list of 5 OHLC dicts (oldest first)
    """
    if pd is not None and hasattr(rows, "iterrows"):
        # Accept DataFrame directly
        blocks = []
        for _, row in rows.iterrows():
            sym = row.get("name", "")
            ohlc = ohlc_map.get(sym) if ohlc_map else None
            blocks.append(format_ticker_block(row, ohlc))
        return "\n\n".join(blocks)

    blocks = []
    for row in rows:
        sym = row.get("name", "")
        ohlc = ohlc_map.get(sym) if ohlc_map else None
        if pd is not None:
            import pandas as _pd
            blocks.append(format_ticker_block(_pd.Series(row), ohlc))
        else:
            blocks.append(format_ticker_block(row, ohlc))  # type: ignore
    return "\n\n".join(blocks)


def build_prompt(
    prompt_num: int,
    ticker_data: str,
    ticker_list: str = "",
    prev_run_output: str = "",
) -> str:
    """
    Fill the prompt template for the given run number (1, 2, or 3).

    ticker_data:     formatted text from format_ticker_data()
    ticker_list:     comma-separated symbols e.g. "AAPL, TSLA, NVDA"
    prev_run_output: pasted output from the previous run (for prompts 2 and 3)
    """
    template = _TEMPLATES.get(prompt_num)
    if template is None:
        raise ValueError(f"prompt_num must be 1, 2, or 3 — got {prompt_num!r}")

    prev = prev_run_output.strip() or _PREV_RUN_PLACEHOLDER

    return template.format(
        ticker_list=ticker_list or "—",
        ticker_data=ticker_data,
        prev_run_output=prev,
    )

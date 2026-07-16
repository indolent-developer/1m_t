"""
prompts.momentum_rotation

LLM review layer for the `/rsuggest` momentum-rotation scan (docs/MomentumRotation.md).

The FROM/TO candidate lists and the 60d correlation matrix are already computed
deterministically (see cmd_rotate_suggest in interfaces/console/cmd_trading.py and
CommandService.portfolio_correlations). This prompt does NOT re-derive those —
its job is judgment the numbers can't supply: which FROM->TO pairing makes sense
given thematic/factor overlap, catalyst timing, and portfolio concentration, and
to call out when a mechanically-qualified TO is a poor rotation despite passing
the rules (e.g. it only "diversifies" on paper because the correlation window is
short, or every surviving TO candidate is a proxy for the same trade).

Usage:
    from prompts.momentum_rotation import build_user_prompt, SYSTEM_PROMPT, validate_response
    user = build_user_prompt(ctx)                    # ctx = dict of fields below
    resp = call_llm(SYSTEM_PROMPT, user)              # your API call, parse JSON
    resp = validate_response(resp, from_tickers, to_tickers)
"""
from __future__ import annotations

import json

SYSTEM_PROMPT = """You are a portfolio manager reviewing a mechanical momentum-rotation scan (see MomentumRotation.md rules, already applied upstream).

You are given:
- FROM candidates: portfolio names already qualified as stalled/low-conviction (safe to temporarily vacate capital from).
- TO candidates: portfolio names already qualified as confirmed-momentum (candidate capital destinations).
- A 60d daily-return correlation matrix across the whole book, plus a flagged max-correlation for each TO candidate against other holdings.
- Any open rotations already in flight, and today's guardrail check results.

Your job is NOT to re-run the FROM/TO filters — trust them. Your job is judgment on TOP of the filters:
1. Which FROM -> TO pairing(s) make the most sense to actually execute today, and why.
2. Whether a mechanically-qualified TO is a weak rotation despite passing the rules — e.g. it's a near-duplicate of an existing holding (thematic overlap the correlation number may understate or overstate), it shares an imminent catalyst/earnings date with something already held, or it's really the same trade as another TO candidate on the list.
3. Whether the batch as a whole increases concentration risk (e.g. two suggested rotations both landing in the same sector).

Hard constraints:
- Only propose from_ticker/to_ticker pairs using tickers that appear in the FROM CANDIDATES / TO CANDIDATES lists you're given. Never invent or substitute a ticker.
- size_pct must be 25-50 (Section 5 sizing rule).
- tp_pct: 1.5x-2x the TO ticker's ATR%, rounded to a sane integer. sl_pct: 1x the TO ticker's ATR%.
- Respect the max concurrent rotation limit (2-3 open at once, counting already-open rotations shown below) — propose fewer rotations, or none, rather than exceeding it.
- If every qualifying TO is a poor fit (high correlation, shared catalyst, or duplicates an already-open rotation), it is correct to propose zero rotations and say so — do not force a pairing to fill the schema.

Output ONLY valid JSON matching the schema below. No markdown fences, no prose outside the JSON."""

USER_TEMPLATE = """=== SCAN CONTEXT ===
Timeframe: {tf} | Portfolio median ATR%: {median_atr:.2f}%
Open rotations already active: {open_rotation_count} (max 2-3 concurrent)

=== FROM CANDIDATES (ranked ADX ascending — weakest trend first) ===
{from_candidates_text}

=== TO CANDIDATES (ranked ADX descending, ST% ascending) ===
{to_candidates_text}

=== CORRELATION FLAGS (60d daily return, |corr| > 0.7 vs another current holding) ===
{correlation_flags_text}

=== GUARDRAIL CHECK (already evaluated against Section 8) ===
{guardrail_text}

=== SCHEMA (fill in this exact field order) ===
{{
  "rotations": [
    {{
      "from_ticker": "<must be one of the FROM candidates above>",
      "to_ticker": "<must be one of the TO candidates above>",
      "size_pct": "<integer 25-50>",
      "tp_pct": "<integer, 1.5x-2x TO's ATR%>",
      "sl_pct": "<float, 1x TO's ATR%>",
      "rationale": "<1-2 sentences: why this pairing, beyond the mechanical filters>",
      "correlation_note": "<1 sentence: diversification benefit, or what the corr number misses>",
      "confidence": "<high|medium|low>"
    }}
  ],
  "skipped": [
    {{"ticker": "<TO candidate not used>", "reason": "<1 sentence>"}}
  ],
  "batch_note": "<1-2 sentences on overall concentration/risk of the proposed batch, or 'n/a' if no rotations proposed>"
}}
Note: "rotations" may be an empty list if no pairing is worth executing today — see hard constraints."""


def _fmt_from(row: dict) -> str:
    return (
        f"{row['ticker']}: ST {row['st_dir']} ST% {row.get('st_pct', '—')}, "
        f"ADX {row['adx']:.0f}, RSI {row['rsi']:.0f}, ATR% {row['atr_pct']:.2f}%, EMA {row.get('ema_flag', '—')}"
    )


def _fmt_to(row: dict) -> str:
    corr = row.get("max_corr")
    corr_str = f", max corr {corr[1]:+.2f} vs {corr[0]}" if corr else ""
    return (
        f"{row['ticker']}: ST {row['st_dir']} ST% {row.get('st_pct', '—')}, "
        f"ADX {row['adx']:.0f}, RSI {row['rsi']:.0f}, ATR% {row['atr_pct']:.2f}%, "
        f"EMA {row.get('ema_flag', '—')}{corr_str}"
    )


def build_user_prompt(ctx: dict) -> str:
    """ctx keys:
    tf, median_atr, open_rotation_count,
    from_candidates: list[dict] (ticker, st_dir, st_pct, adx, rsi, atr_pct, ema_flag),
    to_candidates:   list[dict] (same + optional max_corr: (other_ticker, corr)),
    correlation_flags: list[str] (pre-formatted "TICKER: +0.82 vs OTHER" lines),
    guardrail_violations: list[str],
    """
    from_text = "\n".join(f"- {_fmt_from(r)}" for r in ctx["from_candidates"]) or "(none)"
    to_text   = "\n".join(f"- {_fmt_to(r)}" for r in ctx["to_candidates"]) or "(none)"
    corr_text = "\n".join(f"- {line}" for line in ctx.get("correlation_flags", [])) or "(none flagged)"
    guard_text = "\n".join(f"- {v}" for v in ctx.get("guardrail_violations", [])) or "No guardrail violations in top candidates"

    return USER_TEMPLATE.format(
        tf=ctx["tf"],
        median_atr=ctx["median_atr"],
        open_rotation_count=ctx["open_rotation_count"],
        from_candidates_text=from_text,
        to_candidates_text=to_text,
        correlation_flags_text=corr_text,
        guardrail_text=guard_text,
    )


def parse_response(raw: str) -> dict:
    """Strip accidental fences and parse the model's JSON."""
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(cleaned)


def validate_response(
    response: dict,
    from_tickers: set[str],
    to_tickers: set[str],
    max_open_rotations: int = 3,
    open_rotation_count: int = 0,
) -> dict:
    """Deterministic post-processing: reject hallucinated tickers and out-of-policy sizing.

    Any issue appended to response['validation_issues'] means the proposed rotation(s)
    should not be auto-executed — surface for manual review instead.
    """
    issues: list[str] = []
    rotations = response.get("rotations", [])

    if open_rotation_count + len(rotations) > max_open_rotations:
        issues.append(
            f"{len(rotations)} proposed + {open_rotation_count} open exceeds max {max_open_rotations} concurrent"
        )

    for r in rotations:
        fr, to = r.get("from_ticker"), r.get("to_ticker")
        if fr not in from_tickers:
            issues.append(f"from_ticker {fr!r} not in FROM candidates — hallucinated, drop this rotation")
        if to not in to_tickers:
            issues.append(f"to_ticker {to!r} not in TO candidates — hallucinated, drop this rotation")
        size = r.get("size_pct")
        if not isinstance(size, (int, float)) or not (25 <= size <= 50):
            issues.append(f"{fr}->{to}: size_pct {size!r} outside 25-50 sizing rule")
        tp, sl = r.get("tp_pct"), r.get("sl_pct")
        if isinstance(tp, (int, float)) and isinstance(sl, (int, float)) and tp <= sl:
            issues.append(f"{fr}->{to}: tp_pct {tp} <= sl_pct {sl} — inverted risk/reward")

    response["validation_issues"] = issues
    return response

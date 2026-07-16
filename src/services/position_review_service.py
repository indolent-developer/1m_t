"""
services.position_review_service

Position-management LLM call for an EXISTING portfolio holding — thesis_status
(INTACT/WEAKENED/BROKEN), action (HOLD/ADD/TRIM/EXIT), invalidation/confirmation
levels, and event risk (earnings, hearings, litigation, regulatory, contracts).

Deliberately separate from TradeHelperService's /th prompt: that one pitches
fresh entries (technicals, structure, R:R). This one only answers "do we still
want this position, at this price, at this weight" — no technical dump needed.

Used by /analyzep. Thesis/horizon per symbol come from data/portfolio_thesis.json,
maintained by hand; symbols not yet in that file get "unknown — infer from
available data" and the model is expected to reason more conservatively.
"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT        = Path(__file__).resolve().parents[2]
_THESIS_FILE = _ROOT / "data" / "portfolio_thesis.json"

_UNKNOWN = "unknown — infer from available data"

SYSTEM_PROMPT = """You are reviewing an EXISTING position in a portfolio. You are NOT pitching a fresh trade — do not produce a buy thesis or entry analysis. Your deliverable is a position management decision.

## DECISION RULES
1. Evaluate the position as if deciding TODAY whether you would want this exposure at the CURRENT price and weight. The entry price and unrealized loss are context only — never inputs to the decision. Do not recommend ADD merely because the price is lower than cost, and do not recommend EXIT merely because the position is red.
2. First determine thesis_status, then derive the action from it. The core question: is the original thesis intact, weakened, or broken — and what is the evidence?
3. ADD is permitted only if thesis_status is INTACT and you can name what specifically improved or what the market is mispricing since the original thesis. "It's cheaper" is not a reason.
4. One call only. No hedged multiple verdicts. If the honest answer is "HOLD with a tight invalidation," say exactly that with the number.
5. For event_risk: check days to next earnings and any hearing, litigation, regulatory, or contract catalyst inferable from headlines or hard events. Mark any date you are inferring rather than confirming with "~est". Use "none" only if genuinely nothing is visible.
6. If your call relies on data you cannot verify (headlines, inferred fundamentals), keep the reason falsifiable and flag inferred facts — do not present guesses as confirmed.
7. Check the stock's 1d/5d return against the headlines and hard events provided. If the move is large (relative to a normal day for this name) and a headline plausibly explains it, name that headline in move_explained. If the move is large and NOTHING in the provided news explains it, say so explicitly ("no news found for this move") rather than inventing a cause — that absence is itself useful signal (could be sector-wide, flow-driven, or an unreported event worth checking manually). If the move isn't unusual, say "no notable move".

## OUTPUT
Return ONLY valid JSON matching this schema. No prose before or after, no markdown fences, no comments.

{
  "thesis_status": "INTACT | WEAKENED | BROKEN",
  "action": "HOLD | ADD | TRIM | EXIT",
  "size_pct": <number 0-100: percent of CURRENT SHARES to buy (ADD) or sell (TRIM/EXIT); EXIT = 100, HOLD = 0>,
  "reason": "<1 falsifiable sentence that references thesis_status>",
  "invalidation_level": <number or null: price below which the thesis is dead and the position should be exited regardless of this call>,
  "confirmation_level": <number or null: price or condition above/upon which to add, or re-enter if exiting>,
  "event_risk": "<nearest catalyst first: type + date (mark inferred dates with ~est) + skew (upside/downside); 'none' if nothing visible>",
  "move_explained": "<per rule 7: the headline that explains the 1d/5d move, 'no news found for this move', or 'no notable move'>",
  "re_entry": "<string: level or condition to re-enter — REQUIRED if action is TRIM or EXIT, otherwise null>"
}"""

_VALID_THESIS  = {"INTACT", "WEAKENED", "BROKEN"}
_VALID_ACTIONS = {"HOLD", "ADD", "TRIM", "EXIT"}


def load_thesis(symbol: str) -> tuple[str, str]:
    """Read (thesis, horizon) for symbol from data/portfolio_thesis.json, or unknown defaults."""
    try:
        data = json.loads(_THESIS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    entry = data.get(symbol, {})
    return entry.get("thesis") or _UNKNOWN, entry.get("horizon") or _UNKNOWN


def build_user_prompt(
    symbol: str,
    own_position: dict,
    current_price: float,
    thesis: str,
    horizon: str,
    cash_available: str,
    days_to_earnings: int | None,
    hard_events_text: str,
    headlines_text: str,
    ret_1d: float = 0.0,
    ret_5d: float = 0.0,
    rel_vol: float = 1.0,
    spy_ret_1d: float = 0.0,
    vix: float = 0.0,
    vix_regime: str = "",
) -> str:
    side = own_position["side"].upper()
    qty  = int(own_position["quantity"])
    avg  = own_position["avg_price"]
    weight   = own_position["weight_pct"]
    upnl_pct = own_position["upnl_pct"]
    upnl     = own_position["upnl"]
    earnings_str = str(days_to_earnings) if days_to_earnings is not None else "unknown"

    return f"""## POSITION
- Ticker: {symbol}
- Side: {side}
- Shares: {qty}
- Avg cost: ${avg:.2f}
- Current price: ${current_price:.2f}
- Portfolio weight: {weight:.1f}%
- Unrealized P&L: {upnl_pct:+.1f}% (${upnl:+.0f})

## MARKET CONTEXT (for move_explained — is this stock-specific or broad market?)
- Stock return: 1d {ret_1d:+.1f}%, 5d {ret_5d:+.1f}%   Volume vs 20d avg: {rel_vol:.1f}x
- SPY 1d return: {spy_ret_1d:+.1f}%   VIX: {vix:.1f} [{vix_regime}]

## CONTEXT
- Original thesis: {thesis}
- Time horizon: {horizon}
- Cash available to add: {cash_available}

## EVENT DATA (for event_risk and move_explained)
- Days to next earnings: {earnings_str}
- Hard events (structured): {hard_events_text or "none"}
- Recent headlines: {headlines_text or "(none)"}"""


def validate_card(card: dict) -> list[str]:
    """Run the five post-parse checks. Returns a list of failure reasons (empty = valid)."""
    errs = []
    if card.get("thesis_status") not in _VALID_THESIS:
        errs.append(f"invalid thesis_status: {card.get('thesis_status')!r}")
    action = card.get("action")
    if action not in _VALID_ACTIONS:
        errs.append(f"invalid action: {action!r}")
        return errs
    if action == "ADD" and card.get("thesis_status") != "INTACT":
        errs.append("action=ADD requires thesis_status=INTACT")
    if action in ("TRIM", "EXIT") and not card.get("re_entry"):
        errs.append(f"action={action} requires non-null re_entry")
    if action == "EXIT" and card.get("size_pct") != 100:
        errs.append("action=EXIT requires size_pct=100")
    if action == "HOLD" and card.get("size_pct") != 0:
        errs.append("action=HOLD requires size_pct=0")
    if action in ("HOLD", "ADD") and card.get("invalidation_level") is None:
        errs.append(f"action={action} requires non-null invalidation_level")
    return errs


def parse_card(raw: str) -> dict:
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(cleaned)


async def review_position(
    symbol: str,
    own_position: dict,
    current_price: float,
    days_to_earnings: int | None,
    hard_events_text: str,
    headlines_text: str,
    cash_available: str,
    xai_key: str,
    ret_1d: float = 0.0,
    ret_5d: float = 0.0,
    rel_vol: float = 1.0,
    spy_ret_1d: float = 0.0,
    vix: float = 0.0,
    vix_regime: str = "",
    llm_model: str = "grok-3",
    max_retries: int = 2,
) -> dict:
    """Run the position-review LLM call, validating and retrying on schema violations.

    Returns the parsed card with an added "_validation_errors" list — empty on a
    clean pass, non-empty (best-effort card) if retries were exhausted.
    """
    from infrastructure.gateways.llms.grok_client import GrokLLM
    from core.adapters.llm import LLMRequest

    thesis, horizon = load_thesis(symbol)
    user_prompt = build_user_prompt(
        symbol, own_position, current_price, thesis, horizon, cash_available,
        days_to_earnings, hard_events_text, headlines_text,
        ret_1d=ret_1d, ret_5d=ret_5d, rel_vol=rel_vol,
        spy_ret_1d=spy_ret_1d, vix=vix, vix_regime=vix_regime,
    )

    client = GrokLLM(api_key=xai_key)
    card: dict = {}
    last_errs: list[str] = ["no attempt made"]

    for attempt in range(max_retries + 1):
        req = LLMRequest(
            prompt=user_prompt, system=SYSTEM_PROMPT,
            model=llm_model, max_tokens=500, temperature=0.2,
        )
        resp = await client.complete(req)
        try:
            card = parse_card(resp.text)
        except json.JSONDecodeError as e:
            last_errs = [f"JSON parse error: {e}"]
            user_prompt += f"\n\nNOTE: your previous response failed to parse as JSON ({e}). Resend valid JSON only, matching the schema exactly."
            continue

        last_errs = validate_card(card)
        if not last_errs:
            card["_validation_errors"] = []
            return card

        user_prompt += (
            f"\n\nNOTE: your previous response failed validation: {'; '.join(last_errs)}. "
            f"Fix and resend valid JSON only, matching the schema exactly."
        )

    card["_validation_errors"] = last_errs
    return card

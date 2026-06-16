# Mechanical Trading System v1 — Rules Engine
**Effective:** June 2026 | **Review cycle:** Every Sunday
**Prime directive:** No decision is made while a position is live. Everything is decided before the open, in writing.

---

## Phase 0 — Prerequisite (one-time, before system goes live)
The system cannot run on top of an emotional book. Before Day 1:

- [ ] Triage all 22 current positions. For each: "Would I open this position fresh, today, at this price, under the new rules?" If No → close. If Yes → it must immediately get a hard stop in the market and count against the 3-position limit.
- [ ] Result: ≤ 3 positions, all with broker-side stops, OR 100% cash.
- [ ] Enable any broker-side protections available (daily loss limit, order confirmations, bracket-order defaults).
- [ ] Print the One-Page Checklist (bottom of this doc). It lives next to the keyboard.

**System start date: ____________ (no trades before triage is complete)**

---

## Layer 1 — Capital Protection (broker-enforced where possible)

| Rule | Limit | Trigger action |
|---|---|---|
| Per-trade risk | 0.5% of own equity (currently ≈ $280–$360) | Sizing formula only — no override |
| Daily loss limit | −1% own equity (≈ $560–$700) | Flat all day trades, platform closed, walk away |
| Daily trade-count | Max 2 new entries per day | 3rd setup goes on tomorrow's watchlist |
| Max open positions | 3 | No exceptions, including "small" ones |
| Weekly loss limit | −$2,000 realized | No trading for the rest of the week |
| Monthly loss limit | −$4,000 realized | Full month off + system review |
| Equity floor | Own equity $55,000 | 100% cash. Loan repayment plan. Trading paused. |

**Every entry is a bracket order.** Stop and target attached at fill. An entry without an attached stop is a rule violation even if it makes money.

---

## Layer 2 — Position Sizing (formula, zero discretion)

```
Risk$ = 0.005 × own equity        (recalculate Sunday only, not daily)
Shares = floor( Risk$ / (Entry − Stop) )
```

- If shares × entry > 15% of own equity → trade is too tight-stopped or too expensive. Skip.
- Options: max premium at risk = Risk$. Stop = −25% of premium, in the market.
- The formula's output is final. Rounding up "a little" is a violation.

---

## Layer 3 — Entry Checklist (ALL must be Yes, or no trade)

1. Was this exact setup written in last night's / pre-market Daily Plan with entry, stop, and target? **Y/N**
2. Is there a dated, verifiable catalyst (not just "it's moving")? **Y/N**
3. Is R:R ≥ 3:1 measured to the technical stop (not a hoped-for stop)? **Y/N**
4. Does the size come from the Layer 2 formula? **Y/N**
5. Fewer than 2 losses so far today? **Y/N**
6. Is this ticker OFF the revenge-ban list (Layer 4)? **Y/N**
7. Are fewer than 3 positions open? **Y/N**

One "No" = no trade. There is no override procedure. That's the point.

---

## Layer 4 — Anti-Revenge Rules (your known failure mode)

- **30-minute lockout** after any stop-out. No orders of any kind. Timer goes on, screen goes off.
- **Two losses in a day = done for the day.** Win/loss streak resets at midnight, not when you "feel ready."
- **Revenge-ban list:** any ticker that stops you out is untradeable for the remainder of that week. Write it on the list immediately.
- **Averaging down is banned.** Adding to a loser is not a strategy; it is the single behavior that produced the −$4,367 and −$2,733 positions. No exceptions clause exists.
- **No order entry from the phone.** Trades happen at the desk, during the defined window, or via pre-set limit orders only.

---

## Layer 5 — Trade Management (decided at entry, executed by orders)

- Stop is in the market from second one. It may be moved **up** (trailing) only — never down, never widened, never "to give it room."
- At +2R: sell half, move stop to breakeven on the remainder.
- Remainder trails (structure-based or 20% trail on options premium).
- Time stop: if the catalyst thesis hasn't played out by its dated event + 1 session → exit at market. A trade with no catalyst left is just exposure.

---

## Layer 6 — Measurement (what "winning" means for the next 60 days)

Daily journal (existing template) + one new column: **Violations (count + which rule)**.

Weekly scorecard (Sunday):
| Metric | Target |
|---|---|
| Rule violations | **0** ← the only KPI that matters for 60 days |
| Trades taken | ≤ 10 |
| Avg R on winners | ≥ 2.0R |
| Avg R on losers | ≤ −1.0R (proves stops are honored) |
| Win rate | Informational only — do not optimize for it |

Grading: A losing week with 0 violations = **successful week**. A winning week with violations = **failed week**. P&L follows process with a lag; violations predict blowups immediately.

After 60 days of clean execution → review and consider scaling risk from 0.5% to 0.75%.

---

## Process Goals (replaces daily % targets)

The old 1.9266%/day target is retired — it forces oversizing, and oversizing breaks every rule above. New goals:

1. **Months 1–2:** Zero-violation execution. Account survival. Any green = bonus.
2. **Months 3–4:** Positive expectancy proven over ≥ 30 trades (avg win × win% > avg loss × loss%).
3. **Months 5–6:** If expectancy is proven and violations stay at zero → scale risk gradually. If not → the data tells you what to fix.

---

## One-Page Checklist (print this)

```
BEFORE OPEN:  Daily Plan written? Setups have entry/stop/target/size?
ON ENTRY:     Bracket order (stop+target attached). Size from formula.
IF STOPPED:   30-min lockout. Ticker → ban list. 2nd loss = done today.
NEVER:        Add to a loser. Move a stop down. Trade off the phone.
              Trade without a written plan. Override the checklist.
EOD:          Journal + violation count. Friday/Sunday: scorecard.
```
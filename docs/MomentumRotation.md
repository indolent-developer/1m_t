# Momentum Rotation Strategy — ST / ATR% / ADX / EMA

A rules-based framework for using `/rotate` and `/restore` to shift capital
from stalled long positions into confirmed-momentum names, then restoring
the original holding once profit is captured.

---

## 1. Objective

Keep core long positions intact over time while temporarily redeploying
capital sitting in "dead" names (flat/negative trend, low volatility) into
names showing fresh, confirmed upside momentum — then restore.

This is a **long-only, capital-efficiency strategy**, not a directional bet
against the FROM position. You are not shorting or abandoning FROM; you are
parking its capital somewhere more productive until it's needed back.

---

## 2. Inputs Required

Per ticker, from `/indp`:

| Field | Use |
|---|---|
| ST direction (▲/▼) | Primary trend filter |
| ST% | Distance from supertrend line (proxy for trend strength/extension) |
| RSI | Overbought/oversold filter |
| ADX | Trend *strength* (directional conviction) |
| ATR% | Volatility / how much room the name has to move |
| EMA8 vs EMA20 + flag (T/↓/↑) | Confirmation of trend freshness |

---

## 3. FROM Candidates (sell side — capital source)

A position qualifies as a **FROM** candidate if it meets **all** of:

1. **ST = ▼** (in a bearish supertrend), or ST = ▲ but ST% < 1% (barely
   holding, effectively flat)
2. **ATR% below the portfolio median** — low volatility, little edge either
   direction
3. **ADX < 25** — trend is weak/directionless, not a strong committed move
   (avoid pulling capital from names with ADX > 30 even if ST is bearish —
   that's a strong trend, not a stalled one, and could still be reversing
   violently against you)
4. **RSI between ~35–55** — avoid pulling from names that are oversold
   (RSI < 30). An oversold ST▼ name risks a mean-reversion bounce right
   after you exit.

**Rank FROM candidates by ADX ascending (weakest trend first).** The lower
the ADX, the more "asleep" the name is — the safer it is to temporarily
vacate.

---

## 4. TO Candidates (buy side — capital destination)

A position qualifies as a **TO** candidate if it meets **all** of:

1. **ST = ▲**
2. **EMA8 > EMA20 with flag = T (fresh cross)** — do not select names
   flagged `↓` even if ST has flipped bullish; the faster trend filter
   hasn't confirmed yet and these are higher whipsaw risk
3. **ADX > 20** — some real directional conviction behind the move
4. **RSI between ~45–65** — momentum is active but not yet overbought;
   avoid RSI > 70 (limited near-term room, chasing risk)
5. **ATR% at or above the portfolio median** — enough volatility to hit a
   take-profit target in a reasonable timeframe

**Rank TO candidates by ADX descending, then by ST% ascending** (prefer
strong trend that hasn't extended too far yet, to avoid buying an extended
move).

---

## 5. Sizing Rule

- Rotate a **fixed fraction** of the FROM position, not all of it, unless
  FROM is a small/non-core holding.
  - Suggested default: **25–50%** of the FROM position per rotation.
  - Never rotate a position below a size where restoring it back is
    impractical (e.g. odd lots).
- Do not run more than **2–3 concurrent open rotations** at a time. Each
  open rotation is capital you don't fully control until `/restore`
  completes — too many open at once makes tracking and risk management
  harder.

---

## 6. Entry Execution

For each qualifying FROM → TO pair:

```
/rotate FROM SIZE TO +X%
```

- **SIZE**: per the sizing rule above (e.g. `50%`, or a share count, or
  `e2000`)
- **+X%**: take-profit target on TO. Suggested default: **1.5× to 2× the
  TO name's ATR%**, rounded. Example: TO has ATR% 1.09% → set `+8%` to
  `+10%` as a realistic multi-day target, not `+1%` (too tight, noise) or
  `+25%` (unrealistic without a strong catalyst).

Example from current scan:
```
/rotate STZ 50 RDW +10      (STZ: ADX 17, ATR% 1.16%, RSI 38 → RDW: ADX 28, ATR% 1.09%, RSI 57, fresh T cross)
/rotate ALVO all PLUG +8    (ALVO: ADX 22, ATR% 0.77%, RSI 41 → PLUG: ADX 37, ATR% 0.93%, RSI 63, fresh T cross)
```

---

## 7. Exit / Restore Rules

Restore when **any** of the following triggers:

1. **Take-profit fills** on TO (automatic via the `+X%` limit) → run
   `/restore TO FROM` immediately once notified.
2. **ST flips bearish on TO** before the take-profit is hit — exit
   regardless of P/L. The thesis (fresh confirmed uptrend) is invalidated.
3. **ADX on TO drops below 15** — trend has lost conviction; don't wait for
   a reversal to confirm it, restore proactively.
4. **Time stop: 5 trading days** with no take-profit hit and no clear
   deterioration — restore anyway. This is a rotation strategy, not a
   long-term hold; stale rotations defeat the purpose.

Check open rotations daily with `/rotations`.

---

## 8. Guardrails

| Rule | Reason |
|---|---|
| Never rotate out of a FROM name with ADX > 30 | Strong trend, not stalled — too risky to vacate |
| Never rotate into a TO name with EMA flag `↓` | Unconfirmed cross, higher whipsaw risk |
| Never rotate into RSI > 70 | Chasing extended moves |
| Never rotate out of RSI < 30 | Bounce risk right after exit |
| Max 2–3 concurrent open rotations | Preserve visibility/control over deployed capital |
| Hard time stop at 5 trading days | Prevents rotations turning into unmanaged new positions |
| If BUY leg fails after SELL fills | Follow the CLI's printed manual `/buy` command immediately — don't leave proceeds as uninvested cash for FROM |

---

## 9. Risk Management — What Happens When Price Moves Against You

A rotation has **two legs** exposed to risk simultaneously: the TO position
you just bought, and the FROM position you're temporarily out of. Treat
them as two separate risk problems.

### 9.1 Risk on the TO leg (the new position)

This is the leg with active market risk — you bought it, it can drop.

**Hard stop-loss, set at entry, every time:**
- Stop-loss = **1× TO's ATR%** below your entry price (tighter than the
  take-profit distance, which is 1.5–2× ATR%). This gives a rough 1:1.5–2
  risk/reward skew per rotation.
- Example: TO entry €23.76, ATR% 0.85% → stop ≈ €23.56, take-profit
  (+8%) ≈ €25.66.
- Place this as a resting stop order if your CLI supports it
  (`/sell TO all @PRICE stop`), or check manually every session if not —
  but never leave it unmonitored.

**If the stop is hit:**
- Exit TO immediately at market. Do **not** wait for a bounce — the thesis
  (fresh confirmed momentum) is already broken if price is moving against
  a fresh ST▲/EMA-T entry this fast.
- Immediately restore FROM with whatever proceeds remain. If proceeds fall
  short of the full FROM qty, take the shortfall path (Section 9.3) rather
  than delaying restoration to "wait for a better price" — an unrestored
  core position is itself a risk (see 9.2).

**If TO stalls (doesn't hit stop or target) but conditions deteriorate:**
- ST flips bearish on TO → exit regardless of P/L (already in Section 7).
- ADX drops below 15 → exit regardless of P/L (already in Section 7).
- These are trend-invalidation exits, distinct from the hard stop-loss,
  and should be checked independently — a position can lose its trend
  characteristics before it ever touches the stop price.

### 9.2 Risk on the FROM leg (the position you're not holding)

This is easy to overlook because you don't "feel" it day to day, but it's
real risk: **if FROM rallies hard while you're rotated out, you miss that
move on the shares you sold.**

Mitigate this at the *selection* stage, not the exit stage — this is why
Section 3 excludes:
- FROM candidates with ADX > 30 (strong trend = higher chance of a sharp
  continuation/reversal you'd miss)
- FROM candidates with RSI < 30 (oversold = elevated bounce risk right
  after you sell)

**Ongoing monitoring while a rotation is open:**
- If FROM's ST flips from ▼ to ▲ with a fresh EMA-T cross *while your
  rotation is still open*, that's a signal FROM's thesis for being a FROM
  candidate has reversed. Treat this as an early-restore trigger — close
  the rotation early via `/restore` even without a TP hit, rather than
  waiting out the time stop. Don't chase a full take-profit on TO at the
  cost of missing a real move on FROM.
- This is the single most common way a rotation "goes wrong": not TO
  crashing, but FROM waking up while you're not in it.

### 9.3 Shortfall handling (TO leg lost value, can't fully restore FROM)

If TO is sold at a loss (stopped out or time-stopped below entry), proceeds
may not cover the full FROM share count.

1. `/restore` will show the affordable reduced qty automatically — accept
   the partial restoration rather than leaving 100% of the capital in cash
   or delaying to "make it back."
2. Log the shortfall (shares short + € amount). Do **not** immediately open
   a new rotation to try to recoup it — this is how a risk-managed strategy
   turns into revenge trading. Return to the normal scan/selection process
   next session.
3. If shortfalls happen on 2+ rotations in a row, pause the strategy and
   revisit thresholds in Section 3/4 — it's a signal your TO selection
   criteria are letting through names that don't actually hold their
   trend.

### 9.4 Portfolio-level circuit breaker

- If **total realized rotation P/L for the week goes negative**, stop
  opening new rotations until the following week. Let any open rotations
  finish per their normal exit rules, but don't add new exposure while the
  process is underperforming.
- If a single rotation stop-loss loss exceeds your normal per-position risk
  budget (e.g. more than 1% of total portfolio value), treat that as a
  sizing error, not just a losing trade — revisit Section 5 sizing rule
  before continuing.

---

## 10. Auto-Exit Criteria — Full Reference

All conditions that should trigger `/restore`, checked in this priority
order (top triggers first if multiple apply simultaneously):

| Priority | Trigger | Condition | Action |
|---|---|---|---|
| 1 | **Stop-loss hit** | TO price ≤ entry − (1× TO ATR%) | Restore immediately at market, accept shortfall if needed |
| 2 | **FROM reversal** | FROM flips ST▼→▲ with fresh EMA-T cross while rotation open | Restore early, even without TP |
| 3 | **Take-profit hit** | TO price ≥ entry + X% (as set at `/rotate`) | Restore (this is the "successful" path — usually automatic if TP order fills) |
| 4 | **TO trend invalidation** | TO ST flips ▲→▼ | Restore regardless of P/L |
| 5 | **TO momentum decay** | TO ADX drops below 15 | Restore regardless of P/L |
| 6 | **Time stop** | 5 trading days elapsed, none of the above triggered | Restore at current price, reassess |

**Rule: only one trigger needs to fire.** Don't wait for multiple
conditions to align — the first one to trigger ends the rotation. This
keeps the strategy mechanical and removes the temptation to hold a losing
or stale rotation "just a bit longer."

---

## 11. Daily Workflow

1. Run `/indp 5m` (or your preferred timeframe) each session.
2. Filter FROM candidates (Section 3) and TO candidates (Section 4).
3. Cross-check against Section 8 guardrails.
4. Check `/rotations` for any existing open positions — apply exit rules
   (Section 7 and Section 10) before opening new ones.
5. Respect the max concurrent rotation limit before opening new trades.
6. Log rotations opened/closed and realized surplus (the CLI reports this
   automatically at `/restore`) to track whether the strategy is net
   additive over time.

---

## 12. Review Cadence

Track, weekly:
- Number of rotations opened vs. completed vs. time-stopped out
- Average surplus (profit extracted) per completed rotation
- Win rate (take-profit hit vs. time-stopped/stopped early)
- Whether FROM names would have outperformed the TO name over the same
  window (i.e., was the rotation actually additive vs. just holding FROM)

If time-stopped/stopped-early rotations exceed ~40% of total, tighten the
TO selection criteria (raise ADX/RSI thresholds) before continuing.
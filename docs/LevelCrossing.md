# Level Crossing Alerts — Requirements & Design

Detects and notifies when price meaningfully interacts with a key technical
level — break, bounce, rejection, or false break — across the watched symbol
universe, with enough confirmation logic to filter out tick noise and enough
context in the alert to act without opening a chart.

---

## 1. Sample Output

```
⚡ ABCL — Break Above HOD
Level: $8.07  |  Price: $8.13  convincing
Zone: [$8.04 – $8.10]  |  ATR: 0.089
Dwell: 30s  |  Source: ibkr
⏰ 10:20 ET
https://www.tradingview.com/chart/3UGuuzJ4/?symbol=ABCL
```

```
↩️ RDW — Rejection HOD
Level: $12.08  |  Price: $11.97  convincing
Zone: [$12.06 – $12.10]  |  ATR: 0.058
Dwell: 133s  |  Source: ibkr
⏰ 05:11 ET
https://www.tradingview.com/chart/3UGuuzJ4/?symbol=RDW
```

---

## 2. Purpose

Give an early, low-noise signal that a name is at a decision point (a real
support/resistance level), classify what price actually did there, and hand
that off to the trader with enough structure (zone, ATR, dwell, conviction)
to size up the reaction without independently re-checking the chart first.

---

## 3. Scope

**In scope**
- Level computation: previous-day H/L, floor pivots (PP, R1–R3, S1–S3), 1H and
  1D swing support/resistance, and dynamic HOD/LOD (5-min-ago).
- Per-(symbol, level) break/bounce/rejection/false-break detection with
  ATR-derived zone width and dwell/confirmation gating.
- Conviction classification (marginal vs. convincing).
- HOD/LOD-specific reversal confirmation (two-stage, 1-minute Supertrend gated).
- Delivery to Telegram with de-duplication, cooldowns, and a TradingView deep
  link.
- Dynamic add/remove of symbols without restarting the monitor.

**Out of scope**
- Multi-timeframe divergence analysis, order execution/auto-trading on these
  events, backtesting/replay tooling (events are consumed live off the event
  bus only).

---

## 4. Functional Requirements

| # | Requirement |
|---|---|
| FR1 | Static levels (prev day H/L, pivots, 1H/1D S&R) are computed once per symbol at session start from historical bars. |
| FR2 | HOD/LOD-5min-ago are dynamic: recomputed on a fixed refresh cadence, excluding the currently-open 5-min bar so the level reflects a *confirmed* high/low. |
| FR3 | Each level gets a "zone" — a symmetric band around the level sized off ATR — inside which price is considered "at" the level rather than clearly above/below it. |
| FR4 | ATR used for zone width and conviction must be floored at a minimum percentage of price, so a stale/near-zero ATR reading cannot collapse the zone to ~0 width. |
| FR5 | Five event types are emitted: `BREAK_ABOVE`, `BREAK_BELOW`, `BOUNCE`, `REJECTION`, `FALSE_BREAK` — see state machine (§7) for exact trigger conditions. |
| FR6 | A break must hold outside the zone for a minimum dwell time *and* tick count before being confirmed — this filters single-tick wicks. |
| FR7 | A confirmed break that returns to the zone within a window, and stays inside long enough, is reclassified `FALSE_BREAK` referencing the original break. |
| FR8 | Every emitted event carries a `convincing` flag — true only if price cleared the level by at least half an ATR, not just the zone edge. |
| FR9 | Events that don't make semantic sense are suppressed: a "rejection" at a support level, or a "bounce" at a resistance level, are dropped (rejection only applies coming down into resistance from below; bounce only applies coming down from above into support). |
| FR10 | LOD bounces, HOD break-belows, and reversed-LOD break-aboves are held in a pending state and only emitted once confirmed either by the 1-minute Supertrend flipping to agree with the reversal direction, or by price moving 2×ATR past the reversal target (strong-price bypass). |
| FR11 | HOD/LOD trackers must be rebuilt at the start of each new ET session so overnight dwell state can't corrupt the next day's break timing. |
| FR12 | Symbols can be added/removed at runtime; a newly added symbol gets levels computed and trackers built without disturbing other symbols' in-flight state. |
| FR13 | Delivered alerts must be de-duplicated: max one alert per symbol per 5 minutes across all event types, and the same (symbol, event type) suppressed for 10 minutes. |
| FR14 | Alerts in pre-market/post-market are dropped below a minimum volume floor; alerts on very low-ATR names are dropped outright (near-zero zone → noise). |
| FR15 | Every alert includes a TradingView deep link to the symbol's saved chart layout. |

---

## 5. Data Model

**`KeyLevels`** (`src/services/key_level_service.py`) — one per symbol, computed
from daily/hourly/intraday bars:

| Field | Meaning |
|---|---|
| `prev_day_high` / `prev_day_low` | Previous complete session's H/L |
| `pivot_pp/r1-r3/s1-s3` | Standard floor pivots off previous day OHLC |
| `hourly_resistance` / `hourly_support` | Swing highs/lows, last 5 days of 1H bars (closest 4 to price kept) |
| `daily_resistance` / `daily_support` | Swing highs/lows, last 60 1D bars |
| `hod_5min_ago` / `lod_5min_ago` | Intraday high/low excluding the current (open) 5-min bar |

Nearby levels are clustered (merged into their mean) within 0.5% of each
other to avoid near-duplicate levels.

**`LevelEvent`** (`src/core/entities/level_event.py`) — enum:
`BREAK_ABOVE`, `BREAK_BELOW`, `BOUNCE`, `REJECTION`, `FALSE_BREAK`.

**`PriceLevelEvent`** — emitted on the bus per interaction:

| Field | Meaning |
|---|---|
| `event` | one of `LevelEvent` |
| `symbol`, `level`, `price` | what happened and where |
| `zone_lo` / `zone_hi` | zone bounds active when the event fired |
| `atr` | effective (floored) ATR used for this event |
| `convincing` | price cleared level ± 0.5×ATR |
| `tick_source` | `ibkr` \| `finnhub` \| `fmp` |
| `dwell_seconds` | time spent in the prior state before this event fired |
| `original_break` | set on `FALSE_BREAK`, references the break it's cancelling |
| `label` | human label ("HOD", "Pivot R1", "1D Support", …) |
| `level_touched_at` | (HOD/LOD only) when price last entered this level's zone — used to distinguish a fresh reversal ("Reversed") from a stale one ("Bounce") |

---

## 6. Design — Components

```
KeyLevelService                 — computes KeyLevels from OHLC bars (§5)
        │
        ▼
KeyLevelMonitorService           — owns one LevelTracker per (symbol, level)
        │  - subscribes to QUOTE_UPDATE ticks
        │  - refreshes HOD/LOD every hod_lod_refresh_seconds
        │  - runs semantic filtering + HOD/LOD bounce-park confirmation
        │  - emits PriceLevelEvent on the bus
        ▼
IEventBus  (LevelEvent.* topics)
        ▼
TelegramAlertSubscriber          — formats + delivers, applies delivery-layer
                                    filters (§4 FR13/FR14) independent of the
                                    detection-layer filters above
```

`LevelTracker` (`src/services/level_tracker.py`) is the shared state machine —
it's also used directly by `PriceStateManager` for user-specified watch levels,
so detection logic (§7) is identical regardless of where a level came from.

---

## 7. State Machine (`LevelTracker`)

**Position** relative to the zone: `UNKNOWN → ABOVE | IN_ZONE | BELOW`.

**Zone:** `[level − atr×band_mult, level + atr×band_mult]`, where `atr` is
floored at `price × min_atr_pct` before the multiply (see §8). The zone used
while a pending break/false-break confirmation is in progress is *snapshotted*
at the start of that pending state, so ATR drift mid-confirmation can't
generate a phantom transition.

**Convincing:** price beyond `level ± 0.5×atr` (independent of `band_mult`).

| Transition | Condition | Result |
|---|---|---|
| `ABOVE`/`BELOW` → `IN_ZONE` | price re-enters the band | dwell timer starts; any unconfirmed pending break is cancelled silently |
| `IN_ZONE` → `ABOVE` (came from `ABOVE`) | dwell within `[dwell_seconds, bounce_max_dwell_seconds]` | **BOUNCE** |
| `IN_ZONE` → `BELOW` (came from `BELOW`) | same dwell bounds | **REJECTION** |
| `IN_ZONE` → opposite side of where it came from | dwell ≤ `break_max_zone_dwell_seconds` | pending break opens |
| pending break | stays outside zone ≥ `break_confirm_seconds` **and** ≥ `break_confirm_ticks` | **BREAK_ABOVE** / **BREAK_BELOW** |
| price re-enters zone within `false_break_window_seconds` of a confirmed break | stays inside ≥ `false_break_confirm_seconds` **and** ≥ `false_break_confirm_ticks` | **FALSE_BREAK** (references the original break) |
| price gaps directly from `ABOVE` to `BELOW` (or vice versa), skipping the zone | — | pending break opens directly (no bounce/rejection possible) |
| dwell in zone exceeds `bounce_max_dwell_seconds` on exit | — | silent no-op (consolidation, not a reversal) |
| confirmed break's false-break window expires without reclassification | — | break stands permanently |

**HOD/LOD-specific overlay** (`KeyLevelMonitorService`):
- `REJECTION` at a support label, or `BOUNCE` at a resistance label, is
  suppressed outright — semantically backwards (§4 FR9).
- A `BOUNCE` at LOD, `BREAK_BELOW` at HOD, or `BREAK_ABOVE` at LOD
  ("reversed LOD") is *parked*, not emitted immediately: it needs the
  1-minute Supertrend to flip in agreement with the reversal direction, or
  price to move 2×ATR past the reversal target (whichever comes first),
  within a 5-minute timeout, and is cancelled if price drifts back into the
  zone or more than 5×ATR away from it.
  - Previously, "reversed LOD" (`BREAK_ABOVE` at LOD) used a separate,
    one-shot check: Supertrend was read once, at the instant of the break,
    and the event was permanently dropped if it wasn't already bullish — no
    retry. This meant a real reversal was missed whenever Supertrend flipped
    bullish even a few ticks *after* the break (observed on APLD,
    2026-07-15: break at 16:34:52 ET with Supertrend still bearish,
    permanently suppressed with no second chance). It now goes through the
    same park/retry path as the other two gated events (`_PARK_EVENTS` in
    `key_level_monitor_service.py`).

---

## 8. Configuration (defaults)

| Parameter | Default | Effect |
|---|---|---|
| `band_mult` | 0.3 | zone half-width = `0.3 × ATR` |
| `min_atr_pct` | 0.005 (0.5%) | floor on ATR (as % of price) feeding zone + conviction |
| `atr_period` | 14 | ATR lookback, on `band_timeframe` bars (default 5-min) |
| `dwell_seconds` | 120 | min time in zone to qualify a bounce/rejection |
| `bounce_max_dwell_seconds` | 300 | max time in zone before an exit is called consolidation, not reversal |
| `break_confirm_seconds` / `break_confirm_ticks` | 30–60 / 2–3 | min time+ticks outside zone to confirm a break |
| `break_max_zone_dwell_seconds` | 300 | if price lingered in-zone longer than this before exiting, the exit is too ambiguous to call a break |
| `false_break_window_seconds` | 180 | how long after a break a zone re-entry still counts as a candidate false-break |
| `false_break_confirm_seconds` / `false_break_confirm_ticks` | 15 / 2 | min time+ticks back inside zone to confirm a false break |
| `hod_lod_refresh_seconds` | 300 | HOD/LOD recompute cadence |
| bounce-park timeout | 300s | pending HOD/LOD reversal confirmation expires after this |
| Supertrend gate (HOD/LOD reversal) | length 7, mult 3.0, 1-min bars | confirms reversal direction |
| Telegram: per-symbol cooldown | 300s | max 1 alert per symbol regardless of event type |
| Telegram: per-(symbol, event) cooldown | 600s | same event type suppressed |
| Telegram: extended-hours min volume | 100,000 | alerts dropped pre/post-market below this |
| Telegram: min ATR | 0.05 | alerts dropped outright below this (near-zero zone) |

---

## 9. Non-Functional Requirements

- **Latency:** detection is tick-driven and in-process; the only external call
  on the hot path is an on-demand 1-minute bar refresh, and only while an
  HOD/LOD reversal is parked awaiting confirmation.
- **Resilience:** the HOD/LOD refresh loop wraps each symbol in its own
  try/except so one symbol's fetch failure doesn't stall the rest.
- **No duplicate alerts:** dedup is enforced both in detection (zone
  snapshotting prevents phantom re-triggers) and in delivery (cooldowns,
  §8).
- **Extensibility:** adding a new level type only requires producing floats
  into `KeyLevelService`/`KeyLevels` — the tracker, event bus, and delivery
  layer are level-agnostic.

---

## 10. Known Limitations / Future Work

- ATR is computed on `band_timeframe` bars (default 5-min) without a
  session-aware adjustment — pre-market ATR can understate true range on thin
  liquidity, making the zone tighter than intended during those hours (partly
  mitigated by the `min_atr_pct` floor in §8, but not session-specific).
- `band_mult` and `min_atr_pct` are hand-tuned constants with no automated
  backtest/calibration process behind them.
- No correlation or portfolio-context awareness — each (symbol, level) is
  evaluated independently.

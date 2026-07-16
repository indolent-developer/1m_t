# News Catalyst Alert — Requirements & Design

Detects when a news article causes a stock to move sharply, and notifies via
Telegram as soon as the move confirms — with the news, the price move, and a
chart link, so the reaction can be assessed without independently pulling up
the article or the chart first.

Implemented by `NewsReactionAnalyzer` (`src/services/news_reaction_analyzer.py`).

---

## 1. Sample Output

```
📰 News Catalyst: PYPL
Move: -17.0% in 31m after news
News: "Michael Burry Says $53B Offer For PayPal Is 'Simply Too..." [FMP, latency 27.6m]
Published: 07:06 ET
Price at news: $57.04 → Now: $47.37
⏰ 07:38 ET

https://www.tradingview.com/chart/3UGuuzJ4/?symbol=PYPL
```

---

## 2. Purpose

Give an early signal that a name is moving *because of* a specific, named
news event (not generic volatility), with the move sized against the price
at the moment the news actually printed — so it can be treated as a genuine
catalyst-driven read, not noise.

---

## 3. Scope

**In scope**
- Watching price action after every ticker-specific `NEWS_PUBLISHED` event for
  up to 60 minutes.
- Snapshotting the reference price ("price at news") from the actual traded
  price at the article's `published_date`, not whatever price happens to be
  on hand when the event is processed.
- Firing one alert per news item, the first time |move| ≥ 2 % from that
  reference price.
- Delivery to Telegram with a TradingView deep link.
- Feeding a `SYMBOL_DETECTED` scanner hit into the key-level monitor so the
  symbol gets tracked going forward.

**Out of scope**
- News sentiment/classification (any published article can trigger a watch,
  regardless of whether it's actually the cause of a subsequent move).
- Sub-minute reference pricing — the reference price resolution is limited to
  1-minute bars (§6).
- Order execution/auto-trading on these alerts.

---

## 4. Functional Requirements

| # | Requirement |
|---|---|
| FR1 | Every ticker-specific news item (`NewsEvent.NEWS_PUBLISHED`) opens a `PendingWatch` recording `price_at_news` and an expiry 60 minutes after `published_date`. |
| FR2 | `price_at_news` is resolved from the FMP 1-minute historical bar covering `published_date` (§6) — not the live streaming tick at watch-creation time, since `NEWS_PUBLISHED` can be delivered well after actual publication (see `latency_seconds`, §7). |
| FR3 | If the historical bar lookup fails or returns no data, fall back to the last known streaming tick for that symbol, then to a live FMP quote snapshot. A watch is only skipped if all three return nothing. |
| FR4 | Watches are held in a FIFO queue capped at 30 items; a 31st silently evicts the oldest. |
| FR5 | Every `QUOTE_UPDATE` tick for a watched symbol is checked against all its active, unalerted watches; `|price / price_at_news − 1| ≥ 2%` is a *candidate* trigger. |
| FR5a | A candidate trigger is confirmed before alerting: the latest FMP 1-minute bar close (§6a) is fetched and the move is recomputed against it. The alert only fires — and is reported — using this confirmed price/move, not the raw tick. If the bar data is unavailable or doesn't independently confirm the threshold, no alert fires and the watch stays active for the next qualifying tick. |
| FR6 | Symbols not already on the live streaming watchlist are polled via FMP every 30 s instead of relying on ticks. |
| FR7 | Each watch fires at most once — `alerted` is set on first trigger and the watch is excluded from further checks. Because confirmation (FR5a) awaits an HTTP call, a watch is also synchronously claimed (`checking`) the instant a candidate trigger is detected, *before* that await — otherwise concurrent ticks from multiple simultaneously-live sources (IBKR/Finnhub/FMP under `source: auto`) can each pass the `alerted` check during the same confirmation window and all fire. See §6b. |
| FR8 | A watch past its 60-minute expiry is never fired, even if a later tick would satisfy the threshold. A cleanup pass purges alerted/expired watches every 5 minutes. |
| FR9 | On alert: send the Telegram message (§1) and emit `ScannerEvent.SYMBOL_DETECTED` so `SymbolAutoWatcher` adds the symbol to the key-level monitor. |
| FR10 | The alert's "elapsed" figure (`{n}m after news`) is measured from `published_date` to alert-fire time — i.e. real-world time since the article printed, not since the watch was created or the news was received. |
| FR11 | The alert displays feed latency (`fetched_at − published_date`) in minutes, one decimal place, when known. |

---

## 5. Data Model

**`PendingWatch`** (`src/services/news_reaction_analyzer.py`):

| Field | Meaning |
|---|---|
| `symbol` | ticker the watch applies to |
| `news` | the triggering `StockNews` item |
| `price_at_news` | reference price resolved per §6 |
| `expires_at` | `published_date + 60min` |
| `alerted` | set once the 2% threshold has fired for this watch |
| `checking` | claimed synchronously while a candidate move is being bar-confirmed; guards against concurrent duplicate fires (§6b) |

**`StockNews`** (`src/core/entities/market_data.py`) — relevant fields:

| Field | Meaning |
|---|---|
| `published_date` | when the publisher says the article went out (US/Eastern, from FMP `publishedDate`) |
| `fetched_at` | when *our* system received/processed the article |
| `latency_seconds` | `fetched_at − published_date`; `None` if `fetched_at` unset |
| `news_source` | `"FMP"` \| `"Finnhub"` \| `"Yahoo"` \| `"IBKR"` |

---

## 6. Reference Price Resolution (`_price_at_publish`)

`price_at_news` must reflect what the stock actually traded at when the news
printed — **not** whatever price is convenient at the moment our code gets
around to handling the event. Feed latency between publish and receipt can be
tens of minutes (`latency_seconds`), during which the streaming "current"
price can drift arbitrarily far from the publish-time price.

Resolution order:
1. **Historical bar lookup (primary):** fetch FMP 1-minute bars spanning
   `published_date − 5min` to `published_date + 1min`
   (`FmpDataFetcher.get_market_data(..., TimeFrame.MINUTE_1)`); take the last
   bar whose timestamp is `≤ published_date`, or the earliest bar returned if
   none qualify. Use that bar's `close`.
2. **Last streaming tick (fallback):** `_last_prices[symbol]`, if the
   historical lookup raised or returned no bars.
3. **Live FMP quote (fallback):** `FmpDataFetcher.get_last_price(symbol)`, if
   neither of the above produced a price.
4. If all three are empty, the watch is skipped and logged at `debug`.

**Why FMP only, not IBKR:** IBKR is wired into this codebase purely as a
push/streaming source (`IbkrWsDataFetcher`) — there is no historical-bar
retrieval method on it. FMP's `historical-chart/1min` endpoint is the only
as-of-time price source available.

**Known precision limit:** resolution is capped at 1-minute bars, so
`price_at_news` can be off by up to ~1 minute of price action from the exact
publish timestamp. This is a large improvement over using the receipt-time
tick (which can be off by the full feed latency — see §7) but is not
tick-exact.

---

## 6a. Move Confirmation (`_confirm_via_bar`)

**Why this exists:** in premarket / thin-liquidity windows, FMP's streaming
tick feed can emit a single stale or erroneous print — e.g. a live PYPL
alert fired at −17% off a tick reading exactly PYPL's *prior-day close*,
sandwiched between otherwise-normal trading a few minutes before and after,
with essentially no real volume behind the print. This is the same
underlying data-quality class of issue tracked in
[`docs/FmpAlertLatency.md`](FmpAlertLatency.md) for level-crossing alerts —
raw ticks are trusted with no freshness/sanity check.

**Mitigation:** a tick crossing the 2% threshold is only a *candidate*.
Before alerting, `_confirm_via_bar` re-fetches the latest completed FMP
1-minute bar for the symbol and recomputes the move against that bar's
`close`. The alert fires — and reports — using the *bar-confirmed* price,
never the raw tick. If bar data is unavailable, or the bar doesn't
independently confirm the threshold (i.e. the tick was a one-off glitch that
already reverted), nothing fires; the watch remains active and gets
re-checked on the next qualifying tick.

**Known gap:** because 1-minute bars are themselves built from the same
underlying feed, a bad print that happens to *become* a bar's `close` (as in
the PYPL case — the anomalous tick was also the last print of that minute)
can still pass confirmation. This mitigation catches single-tick glitches
that don't survive to a bar close; it does not catch a bad print that lands
exactly on a bar boundary. See §9.

---

## 6b. Concurrent-Tick Duplicate-Fire Guard

**Symptom observed:** a single SNDK news reaction alert was delivered 13
times in a row, all with byte-for-byte identical figures (move, elapsed,
price, timestamp) — meaning all 13 fired within the same instant, not over
time.

**Root cause:** `source: auto` runs IBKR, Finnhub, and FMP concurrently for
the same symbol (`docs/FmpAlertLatency.md`), each as its own asyncio task
emitting `QUOTE_UPDATE` independently. `_check_symbol`'s only original guard
was `watch.alerted`, which is set deep inside `_on_reaction` — *after* the
`await self._confirm_via_bar(...)` call (§6a). Because that confirmation
call yields control back to the event loop, several near-simultaneous ticks
from different source tasks can each observe `alerted == False`, each start
their own confirmation, and each independently fire once confirmed.

**Fix:** `PendingWatch.checking` is set synchronously — with no `await` in
between — the instant a candidate threshold breach is detected, before
`_confirm_via_bar` is awaited. Any concurrent call for the same watch that
runs during that window sees `checking == True` and skips it. The flag is
cleared in a `finally` block regardless of outcome, so a watch that didn't
end up firing (bad tick, unconfirmed) remains checkable on the next tick.

---

## 7. Design — Components

```
NewsMonitorService                — polls/streams news sources, emits
                                     NewsEvent.NEWS_PUBLISHED
        │
        ▼
NewsReactionAnalyzer
        │  - _on_news: resolve price_at_news (§6), open PendingWatch
        │  - _on_tick / _fmp_poll_loop: check active watches on each price update
        │  - _on_reaction: format + send alert, emit ScannerHit
        ▼
IEventBus  (ScannerEvent.SYMBOL_DETECTED)          Telegram
        ▼
SymbolAutoWatcher                                   (chat delivery)
  (adds symbol to key-level monitor)
```

---

## 8. Configuration (defaults)

| Parameter | Default | Effect |
|---|---|---|
| `_MOVE_THRESHOLD` | 2% | move magnitude required to fire |
| `_WATCH_WINDOW_MIN` | 60 min | window after publish during which a move counts |
| `_MAX_WATCHES` | 30 | FIFO cap on concurrent pending watches |
| `_POLL_INTERVAL_S` | 30 s | FMP poll cadence for symbols not on the live watchlist |
| `_CLEANUP_INTERVAL_S` | 300 s | stale/alerted watch purge cadence |
| `TV_CHART_ID` (env) | `3UGuuzJ4` | TradingView chart layout ID used in the alert link |

---

## 9. Known Limitations / Future Work

- Reference-price resolution is 1-minute-bar granularity, not tick-exact
  (§6). A move that both starts and fully completes within the same 1-minute
  bar as `published_date` could still be partially missed or double-counted.
- No sentiment/relevance filtering — any published article for a watched
  symbol opens a watch, so an alert can fire from a large coincidental move
  driven by something other than the article named.
- `elapsed_min` (§4 FR10) is wall-clock time since `published_date`; if the
  underlying publisher misreports `publishedDate` (clock skew, backfilled
  articles), this figure — and the reference price resolved from it — would
  both be wrong in the same direction.
- No de-duplication across near-simultaneous articles on the same symbol
  beyond the natural one-watch-per-article, one-alert-per-watch behavior —
  two unrelated articles minutes apart on the same name can each open a
  watch and each fire independently.
- Bar confirmation (§6a) does not catch a bad print that lands exactly on a
  1-minute bar's close by the time confirmation runs — it only filters ticks
  that don't survive to bar-close. A more robust fix (e.g. requiring the
  *next* bar to also confirm, or a minimum-volume floor on the confirming
  bar) is tracked as an open follow-up, same as the unresolved options in
  [`docs/FmpAlertLatency.md`](FmpAlertLatency.md).

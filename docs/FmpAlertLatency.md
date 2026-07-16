# Issue: Level-crossing alerts fire late / reference stale prices (FMP source)

**Status:** open — investigated, not yet fixed. Revisit and decide on fix approach.

## Symptom

TSLA "Break Above HOD" alert delivered at 12:30 ET:

```
TSLA — Break Above HOD
Level: $400.46  |  Price: $401.58  convincing
Zone: [$399.86 – $401.06]  |  ATR: 2.008
Dwell: 31s  |  Source: fmp
⏰ 12:30 ET
```

By 12:21 ET (per TradingView), TSLA had already rolled over hard — RSI down
to ~11 (deeply oversold), price already back down around $393–398. The alert
referenced a price/level the market had left several dollars and several
minutes earlier.

## Root cause chain

1. **`source: auto` runs FMP polling continuously in parallel** with
   IBKR/Finnhub, not just as a fallback —
   `src/services/price_monitor.py:100-104`. FMP
   (`FmpPriceService.stream`, `src/services/price_service.py:50-98`) is a
   **30s REST poll** (`poll_interval: 30`, `config/base.yaml:172`), not a
   websocket push like `ibkr_ws_data_fetcher.py` / `finnhub_ws_data_fetcher.py`.
   No code checks whether FMP's `timestamp` field is actually fresh vs. wall
   clock — freshness is assumed.

2. **`LevelTracker` dwell/confirm timers use the tick's own embedded
   timestamp**, not receipt time — `src/services/level_tracker.py:128-138`
   (comment: *"Use tick's own timestamp so replays and delayed feeds produce
   correct dwells."*). This is intentional for replay correctness, but means
   a stale FMP tick can still satisfy `break_confirm_seconds` (30s) against
   its own old internal clock, producing a "confirmed" break referencing an
   already-abandoned price.

3. **The only staleness check is "no tick at all for 180s"**
   (`_stale_monitor()`, `price_monitor.py:183-221`) — nothing inspects an
   individual tick's age before it reaches `LevelTracker` via
   `key_level_monitor_service.py:252-259` (`_on_tick`), which feeds every
   `QUOTE_UPDATE` from any source into the same trackers with no
   source-priority arbitration.

4. **The Telegram alert timestamp is wall-clock send-time, not price-time**
   — `src/interfaces/telegram/alert_subscriber.py:111-112,193`
   (`_et_time()` → `datetime.now(_ET)`), not `evt.timestamp` from the
   tracker. So `⏰ 12:30 ET` has no relation to when $400.46→$401.58
   actually happened — compounding the stale price with a misleading
   "current" timestamp.

**Net effect:** a delayed/stale FMP tick confirms a break using its own old
timestamp, and the delivered alert gets stamped with whatever time it
happened to fire — together producing an alert that looks both late and
wrong.

## Fix options (not yet decided)

1. Drop/flag FMP ticks whose embedded timestamp is more than a few seconds
   old vs. wall clock before they reach `LevelTracker`.
2. Stamp the Telegram alert with `evt.timestamp` (actual tick-time) instead
   of `datetime.now()`, so a stale alert is at least visibly labeled stale.
3. Source-priority arbitration: de-prioritize/suppress FMP for a symbol when
   IBKR or Finnhub is already live for it, since FMP is the only non-push
   source in the mix.

## Open questions

- Is FMP's plan/endpoint actually real-time, or delayed (some FMP tiers are
  15-min-delayed)? Nothing in config/code confirms this either way.
- Was there an IBKR/Finnhub hiccup for TSLA around 12:21–12:30 that caused
  more reliance on FMP that window, or does this reproduce even with both
  WS sources healthy? No logs from that window were checked yet.

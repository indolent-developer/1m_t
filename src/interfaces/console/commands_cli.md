# Trading Bot — Command Reference

Works identically in the local CLI and the Telegram bot.
The leading `/` is optional, commands are case-insensitive, symbols are
upper-cased. Every order-submitting command shows a **preview** (with the
inferred order type and USD→EUR conversion) and asks `Confirm? [y/N]` before
going live.

The core idea: **there are only two trade verbs.** `/buy` and `/sell` are
market orders; adding `@TRIGGER` turns them into a stop or limit. That single
change replaces the old `/bstop`, `/stopusd`, `/stopatr`, `/tp`, and `/sl`.

---

## Conventions

### SIZE
| Token   | Meaning                                | Example |
|---------|----------------------------------------|---------|
| `N`     | N shares                               | `120`   |
| `N%`    | N % of the current open position       | `30%`   |
| `all`   | the entire open position               | `all`   |
| `eN`    | N **euros** notional                   | `e3000` |
| `$N`    | N **US dollars** notional              | `$3000` |

- `%` and `all` need an existing position; new entries use shares or `eN`/`$N`.
- Notional is divided by the live ask (buys) / bid (sells); `$N` converts USD→EUR first.
- Euro is always typed `e` — the `€` sign is never used.

### @TRIGGER  (omit for a market order)
A trigger is either a **USD price** or an **ATR offset**. All prices are USD and
auto-converted to EUR.

| Form        | Meaning                                              | Example     |
|-------------|------------------------------------------------------|-------------|
| `@118`      | trigger at $118                                      | `@118`      |
| `@atrM`     | last close ± M × ATR, ATR computed on 1m             | `@atr1.5`   |
| `@atrM:TF`  | same, but ATR computed on timeframe TF               | `@atr2:5m`  |

In `@atrM:TF`, **`TF` is the timeframe the ATR is calculated on** — it does not
change the anchor. The trigger price is always `last_close ± M × ATR(TF)`.

**Stop vs limit is inferred** from the trigger relative to current price, and
shown in the preview:

| Command | Trigger vs price | Resolves to            | Old equivalent |
|---------|------------------|------------------------|----------------|
| `/buy`  | above market     | **stop** (breakout)    | `/bstop`       |
| `/buy`  | below market     | **limit** (pullback)   | —              |
| `/sell` | below market     | **stop** (protective)  | `/stopusd`     |
| `/sell` | above market     | **limit** (take-profit)| `/tp`          |

ATR triggers follow side: `/sell @atrM:TF` = `last_close − M×ATR(TF)` (stop below),
`/buy @atrM:TF` = `last_close + M×ATR(TF)` (stop above). `TF` defaults to `1m`.
Force the type by appending `stop` or `limit`, e.g. `/buy PLTR e3000 @118 limit`.

### TF (timeframe)
`1m  5m  15m  30m  1h  4h  1d`   (default `1m` for ATR triggers)

---

## Trading

### `/buy SYMBOL SIZE [@TRIGGER]`
```
/buy WOLF 500              market buy, 500 shares
/buy PLTR e3000            market buy, €3000 worth
/buy PLTR $3000 @118       stop entry — fires when price ≥ $118
/buy WOLF e2000 @2.10      limit entry — fires when price ≤ $2.10
/buy WOLF 300 @atr1:5m     stop entry — last close + 1 × ATR(5m)
```

### `/sell SYMBOL SIZE [@TRIGGER]`
```
/sell WOLF 30%             market sell 30%
/sell WOLF all             market sell everything
/sell WOLF all @2.50       stop-loss — fires when price ≤ $2.50
/sell WOLF 50% @4.00       take-profit — sells half when price ≥ $4.00
/sell WOLF all @atr1.5     stop — last close − 1.5 × ATR(1m)
/sell WOLF all @atr2:5m    stop — last close − 2 × ATR(5m)
```
A new `@TRIGGER` sell **replaces** any existing stop on that position — so this
is also how you move a stop-loss (the old `/sl`).

### `/close SYMBOL`
Market-close a position in full (by ID, so company-name holdings work too).
Shorthand for `/sell SYMBOL all` at market.
```
/close NFLX
```

### `/closeall [PCT%]`
No arg → flatten everything. With a percentage → trim every position.
```
/closeall          close all (100%)
/closeall 50%      trim every position by half
```

### `/cancel SYMBOL|ID|all`   (alias `/x`)
Cancel resting orders. A symbol cancels all open orders for it, an ID cancels
one, `all` cancels everything.
```
/x WOLF
/x all
/x 8841273
```

---

## Tools

### `/size SYMBOL STOP_USD RISK_EUR`
Position sizing by risk — how many shares so the loss to your stop equals
`RISK_EUR`, using the live ask as entry. Returns entry, stop (USD→EUR), risk per
share, suggested shares, and notional.
```
/size WOLF 2.30 200        risk €200 with a $2.30 stop
```

---

## Market data

### `/q` `/quote SYMBOL`
Bid, ask, last, mid, spread (+%), quote time.
```
/q PLTR
```
> Bare `/q` (no symbol) exits the session — pass a symbol to get a quote.

### `/ind SYMBOL [TF] [ext]`
ATR (+%), RSI, ADX(20), EMA 8, EMA 20, SuperTrend (+flip). Default TF `1d`;
add `ext` for extended hours.
```
/ind WOLF 5m
/ind WOLF 1m ext
```

### `/ind_port [TF]`   (alias `/indp`)
Portfolio-wide indicator table, default `1m` extended, sorted by ATR %.
```
/ind_port                 default
/ind_port 5m
/ind_port ignore NAME     skip a holding
/ind_port unignore NAME
/ind_port list
```

### `/scan pm|pre|vol|spikes|parabolic`
```
/scan pre        pre-market gappers
/scan pm         post-market movers
/scan vol        daily high relative volume (≥ 3×)
/scan spikes     intraday spikes
/scan parabolic  parabolic movers
```

---

## Account

### `/a` `/account`
Value, cash, margin used / free, leverage, currency.

### `/p` `/positions [SYMBOL]`
Open positions: qty, avg price, unrealized P&L. Optional symbol filter.

### `/o` `/orders`
Last 10 orders: symbol, side, qty, price (or `MKT`), status.

### `/pnl`
Open P&L — unrealized total, market value, position count.

---

## Session

### `/h` `/help`  — command list
### `/exit` `/quit`  — leave

---

## Migration from the old commands

| Old                          | New                          |
|------------------------------|------------------------------|
| `/b PLTR 100`                | `/buy PLTR 100`              |
| `/b WOLF 30%`                | `/buy WOLF 30%`              |
| `/bstop PLTR 118 3000`       | `/buy PLTR e3000 @118`       |
| `/s WOLF 50%`                | `/sell WOLF 50%`             |
| `/stopusd WOLF 2.50`         | `/sell WOLF all @2.50`       |
| `/stopusd WOLF 2.50 50%`     | `/sell WOLF 50% @2.50`       |
| `/stopatr WOLF 1.5`          | `/sell WOLF all @atr1.5`     |
| `/stopatr WOLF 2 5m 50%`     | `/sell WOLF 50% @atr2:5m`    |
| `/tp WOLF 4.00`              | `/sell WOLF all @4.00`       |
| `/sl WOLF 2.20`              | `/sell WOLF all @2.20`       |
| `/c NFLX`                    | `/close NFLX`                |

---

## Notes

- **Prices are USD everywhere** and auto-converted to EUR — including stop-losses.
  This removes the old `/sl` quirk where its price was raw EUR while everything
  else was USD.
- **Inference is confirmed, not silent:** the preview always states whether it
  resolved to a stop or a limit, so a mis-typed trigger is caught before submit.
- **Replacing a stop:** placing a new `@TRIGGER` sell on a position supersedes the
  prior one. (If you want this to *modify the broker's attached stop* rather than
  place a separate resting order, that's a one-line switch in the handler —
  same end result either way.)
- **Company-name positions:** holdings stored under a name (e.g. "Netflix" for
  NFLX) close fine by ID via `/close` and full `/closeall`; symbol-based sells
  resolve the ticker through the ISIN cache.
- **Confirmation:** every live order waits for `y`; anything else cancels.
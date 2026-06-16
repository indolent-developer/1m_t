# Telegram Trading Bot Interface

Multi-broker, session-aware Telegram bot for live trading control and monitoring.

---

## Architecture

```
interfaces/telegram/
├── bot.py              TelegramBot (legacy, single-broker)
│                       TradingBot  (multi-broker + session)
├── session.py          SessionContext, SessionManager
├── arg_parser.py       ParsedCommand, parse(), require_args()
├── commands.py         CommandHandler class + legacy cmd_* functions
├── formatters.py       Markdown formatters (legacy) + MarkdownV2 (v2_*)
├── notifier.py         TelegramNotifier — broker event → push message
└── __init__.py
```

### Data flow

```
Telegram message
    → TradingBot._register_commands (chat_id guard)
    → CommandHandler.<method>
        → arg_parser.parse()       extract --broker / --account flags
        → SessionManager.resolve() pick broker + account by priority
        → BaseBroker.<call>        execute action
        → formatters.v2_*()        format response
        → update.message.reply_text(MarkdownV2)

Broker event (ORDER_FILLED, EQUITY_FLOOR_HIT, …)
    → TelegramNotifier.on_*()
    → bot.send_message(Markdown)
```

---

## Broker Registry

`TradingBot` takes a `brokers: Dict[str, BaseBroker]` dict. Keys are arbitrary
strings used by `/use` and `--broker`:

```python
brokers = {
    "capital_live": CapitalBroker(live_config),
    "capital_demo": CapitalBroker(demo_config),
    "ibkr_live":    IBKRBroker(IBKRConfig(is_demo=False)),
    "ibkr_demo":    IBKRBroker(IBKRConfig(is_demo=True)),
    "etoro":        eToroBroker(etoro_config),
}

bot = TradingBot.from_env(brokers=brokers, default_broker="capital_live")
```

---

## Session Context

Each chat maintains a `SessionContext` (set via `/use`):

```
active_broker:  "capital_live"   # which broker is active
active_account: "DU123456"       # which account (empty = broker default)
```

### Resolution order (every command)

| Priority | Source | Example |
|----------|--------|---------|
| 1 (highest) | `--broker` / `--account` flag in command text | `/buy AAPL 10 --broker ibkr_demo` |
| 2 | `/use` session context | `/use ibkr_live DU123456` |
| 3 (lowest) | `default_broker` config | `TradingBot(..., default_broker="capital_live")` |

---

## Command Reference

### Context

| Command | Description |
|---------|-------------|
| `/use` | Show current broker + account |
| `/use capital_live` | Set active broker, clear account |
| `/use ibkr_live DU123456` | Set broker and account |
| `/context` | Show current context |

### Read (poll)

| Command | Description |
|---------|-------------|
| `/status` | Account equity, cash, margin |
| `/positions [SYMBOL]` | Open positions (optional symbol filter) |
| `/orders` | Pending orders |
| `/pnl` | Today's realised PnL, win rate |
| `/progress` | Compound tracker: equity vs target |
| `/risk` | Daily loss, equity floor, drawdown |

### Trade

| Command | Description |
|---------|-------------|
| `/buy SYMBOL QTY` | Market buy |
| `/sell SYMBOL QTY` | Market sell |
| `/close SYMBOL` | Close position |
| `/closeall` | Close all open positions |
| `/stop SYMBOL PRICE` | Update stop loss |

### Strategy control

| Command | Description |
|---------|-------------|
| `/halt [SYMBOL]` | Halt one or all strategies |
| `/resume [SYMBOL]` | Resume one or all strategies |

### Flags (any trade/read command)

```
--broker  NAME    override active broker for this command
--account ID      override active account for this command
```

Examples:
```
/buy AAPL 10 --broker ibkr_live
/buy AAPL 10 --broker ibkr_live --account DU123456
/status --broker capital_demo
/positions --broker etoro
```

---

## Security

Only messages from `TELEGRAM_CHAT_ID` are processed. All other senders are
silently ignored — no error response, no acknowledgement. Unknown commands
get a generic reply only to the authorised chat.

---

## Setup

### Environment

```bash
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=987654321        # get from @userinfobot
```

### Minimal wiring

```python
import asyncio
from interfaces.telegram.bot import TradingBot

bot = TradingBot.from_env(
    brokers={
        "capital_live": capital_broker,
        "ibkr_demo":    ibkr_broker,
    },
    default_broker="capital_live",
    risk_monitor=risk_mon,
    compound_tracker=tracker,
    strategies={"aapl_super": aapl_strategy},
)

asyncio.run(bot.run())          # blocking
# or: asyncio.create_task(bot.run())   # integrated
```

### Legacy single-broker mode

```python
from interfaces.telegram.bot import TelegramBot

bot = TelegramBot(token="...", chat_id="...")
bot.attach_broker(broker)       # subscribes notifier to broker events
await bot.run()
```

---

## Push Notifications

`TelegramNotifier` listens on the broker event bus and sends messages
automatically (no user action required):

| Event | Message |
|-------|---------|
| `ORDER_FILLED` | Fill confirmation with price and fees |
| `ORDER_REJECTED` | Rejection reason |
| `ORDER_CANCELLED` | Cancellation notice |
| `POSITION_OPENED` | New position summary |
| `POSITION_CLOSED` | Closed position with P&L |
| `EQUITY_FLOOR_HIT` | 🚨 Urgent — own equity below floor |
| `DAILY_LOSS_LIMIT` | 🚨 Urgent — go to cash |
| `CONNECTION_LOST` | Broker disconnect warning |
| `RECONNECTING` | Reconnect attempt status |

---

## Message Formatting

Two formatter sets coexist in `formatters.py`:

| Prefix | Mode | Used by |
|--------|------|---------|
| `fmt_*` | `Markdown` | `TelegramNotifier` (push events), legacy `TelegramBot` |
| `v2_*` | `MarkdownV2` | `CommandHandler` (all `/commands` via `TradingBot`) |

MarkdownV2 requires escaping `_ * [ ] ( ) ~ \` > # + - = | { } . !`. All
dynamic values pass through `_esc()` before embedding in templates.

---

## Optional Components

| Parameter | Used by | Notes |
|-----------|---------|-------|
| `risk_monitor` | `/pnl`, `/risk` | Must expose `.daily_pnl`, `.daily_trades`, `.win_rate`, `._daily_loss_limit` |
| `compound_tracker` | `/progress` | Must expose `.current_equity`, `.target_equity`, `.session_pnl`, etc. |
| `strategies` | `/halt`, `/resume` | Dict `{strategy_id: obj}`. Objects need `.halt()` / `.resume()` and optional `._symbol` |

If omitted, the relevant commands reply with a configuration error.

---

## Tests

```bash
python -m pytest tests/interfaces/telegram/test_telegram.py -v
```

Coverage:

- `arg_parser` — parse flags, positional extraction, require_args
- `SessionContext` / `SessionManager` — set, clear, describe, resolution priority
- `CommandHandler` — all commands with mocked brokers (happy path + error cases)
- `formatters` — legacy Markdown + MarkdownV2, escaping, all formatters
- `TelegramNotifier` — all event handlers, send failure resilience
- `TelegramBot.attach_broker` — event subscription count
- `TradingBot.from_env` — missing env var errors

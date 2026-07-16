# 1M — AI-Assisted Momentum Trading System

Algorithmic trading system targeting **$122,562 → $1,000,000** in 110 effective
trading days (May–Nov 2026). The approach: scan US equities for catalyst-driven
movers, validate the edge with AI (Claude / Grok / Gemini / Perplexity), monitor
live price action for key-level breaks, and execute 1–3 high-conviction setups
per day with strict risk guardrails.

---

## Strategy at a Glance

| | |
|---|---|
| **Capital** | $72,562 own + $50,000 loan = $122,562 effective |
| **Target** | $1,000,000 by Nov 2026 |
| **Required daily return** | 1.93% net (compounded) |
| **Instruments** | US equities — AI/tech, biotech, space/defense on volume spikes |
| **Max positions** | 3 concurrent |
| **Per-trade risk** | $363–$726 (0.5–1% own equity) |
| **Daily loss limit** | $2,000 |
| **Hard drawdown stop** | $20,000 total |
| **Own equity floor** | $55,000 (loan guardrail) |

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         run_live_monitor.py                         │
│                                                                     │
│  ┌──────────────┐  SYMBOL_DETECTED  ┌───────────────────────────┐  │
│  │   Scanners   │ ────────────────► │     SymbolAutoWatcher     │  │
│  │  pre-market  │                   │  (watchlist coordinator)  │  │
│  │  post-market │                   └───────────┬───────────────┘  │
│  │  volume      │                               │ subscribes        │
│  │  scalp       │                               ▼                  │
│  └──────────────┘              ┌────────────────────────────────┐  │
│                                │  PriceMonitor                  │  │
│                                │  IBKR WS / Finnhub WS / FMP   │  │
│                                └──────────────┬─────────────────┘  │
│                                               │ QUOTE_UPDATE       │
│                                               ▼                    │
│                                ┌────────────────────────────────┐  │
│                                │  KeyLevelMonitorService        │  │
│                                │  (S/R breaks, bounces,         │  │
│                                │   rejections, false breaks)    │  │
│                                └──────────────┬─────────────────┘  │
│                                               │ LevelEvent         │
│  ┌──────────────────────────┐                 ▼                    │
│  │  NewsMonitorService      │  ┌────────────────────────────────┐  │
│  │  FMP / Finnhub / Yahoo   │  │  TelegramAlertSubscriber       │  │
│  └──────────────────────────┘  │  (level + scanner alerts)     │  │
│  ┌──────────────────────────┐  └────────────────────────────────┘  │
│  │  NewsReactionAnalyzer    │                                       │
│  │  (LLM-graded reactions)  │                                       │
│  └──────────────────────────┘                                       │
└─────────────────────────────────────────────────────────────────────┘

           Event bus: LocalEventBus (single process)
                   or RedisEventBus (multi-process)
```

### Morning Routine (scheduler)

```
02:00 DE  post-market scan     — identify overnight movers (>15%, 5× rel-vol)
07:00 DE  Prompt 1             — Overnight Thesis Check (catalyst validation)
14:00 DE  Prompt 2             — Pre-Market Decision Run (entry plan)
16:10 DE  Prompt 3             — Opening Confirmation (live 5-min session data)
```

Each prompt is built by `MorningEnrichmentService`, enriched with 5-day OHLC,
VWAP, and opening-range data, then sent to Claude/Grok/Gemini/Perplexity via
structured JSON schemas. Responses are saved to `data/daily_morning/`.

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Price feed** | IBKR (ib_async, sub-second) · Finnhub WS · FMP REST |
| **Brokers** | Interactive Brokers · Scalable Capital · eToro · Capital.com |
| **AI / LLM** | Claude (Anthropic) · Grok (xAI) · Gemini (Google) · Perplexity |
| **Market data** | Finnhub · Financial Modeling Prep · Alpha Vantage · Yahoo Finance |
| **Notifications** | Telegram bot |
| **Cache / bus** | Redis (cache + optional event bus) |
| **Runtime** | Python 3.13 · asyncio · uv |

---

## Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) package manager
- Redis (`brew install redis` or Docker)
- IBKR Gateway in Docker (optional, for live price feed)

---

## Setup

### 1. Install dependencies

```bash
uv sync --dev
```

### 2. Configure secrets

```bash
cp .env.example .env
```

Edit `.env` — every key here is injected into the typed config at startup
(see `config/base.yaml` for the full mapping):

```dotenv
# Market data
FINNHUB_API_KEY=
FMP_API_KEY=
ALPHA_VANTAGE_API_KEY=

# Brokers (set whichever you use)
CAPITAL_API_KEY=
CAPITAL_USERNAME=
CAPITAL_PASSWORD=
ETORO_LIVE_PUBLIC_KEY=
ETORO_LIVE_PRIVATE_KEY=

# LLM (set whichever you use)
ANTHROPIC_API_KEY=
XAI_API_KEY=
GEMINI_API_KEY=
PERPLEXITY_API_KEY=

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TV_CHART_ID=          # TradingView chart layout ID for alert links (optional)

# Infrastructure
REDIS_URL=redis://localhost:6379
```

### 3. Start Redis

```bash
redis-server
# or: docker run -d -p 6379:6379 redis:alpine
```

### 4. Start IBKR Gateway (optional — needed for live price feed)

```bash
cd docker/ibkr_gateway
docker compose up -d
# Exposed: live 4001, paper 4002, VNC 5900
```

Set `IBKR_HOST` in `.env` (or `broker.ibkr.host` in `config/local.yaml`) to
enable IBKR as the price source. Without it, Finnhub WS is used, falling back
to FMP REST polling.

---

## Running

All entry points have a shell wrapper in `run_scripts/` that sets `PYTHONPATH`
and loads `.env` automatically.

### Live monitor (main process)

Starts the full pipeline: price feed, level detector, news monitor, scanners,
and Telegram alerts.

```bash
./run_scripts/run_live_monitor.sh

# Control which symbol sources are active:
./run_scripts/run_live_monitor.sh --list portfolio,watchlist,scanners  # default
./run_scripts/run_live_monitor.sh --list scanners    # scanners only, no restore
./run_scripts/run_live_monitor.sh --list watchlist   # restore yesterday's list only
```

### Morning routine scheduler

Always-on daemon that fires the three daily prompts on schedule. Sends a
Telegram notification on each completion or final failure.

```bash
./run_scripts/run_scheduler.sh
```

### Generate a morning prompt on demand

```bash
./run_scripts/run_prompt.sh              # defaults to prompt 2
./run_scripts/run_prompt.sh --prompt 1
./run_scripts/run_prompt.sh --prompt 3
./run_scripts/run_prompt.sh --prompt 2 --date 2026-06-17
```

Reads yesterday's scanner CSV from `data/daily_morning/`, enriches it with
5-day OHLC, and writes filled prompt text to `data/daily_morning/prompts/`.

### Account snapshot

Instant multi-broker position / P&L snapshot, saved to `data/snapshots/`.

```bash
./run_scripts/run_snapshot.sh
```

### Scanners (standalone)

```bash
./run_scripts/run_pre_market_scanner.sh
./run_scripts/run_pre_market_scalp_scanner.sh
./run_scripts/run_post_market_scanner.sh
```

### Local CLI (broker REPL)

Interactive console for account, position, and order commands without Telegram.

```bash
./run_scripts/run_local.sh
```

### Tests

```bash
uv run pytest
uv run pytest tests/services/test_price_monitor.py -v   # specific suite
```

---

## Configuration

All non-secret configuration lives in `config/base.yaml`. The loader merges
files in this order (last wins):

```
config/base.yaml
  → config/{RON2_ENV}.yaml      (production / staging)
    → config/local.yaml         (local dev, git-ignored)
      → .env                    (secrets only)
        → os.environ            (runtime overrides)
```

Set `RON2_ENV=production` to load `config/production.yaml`.

Key top-level sections:

| Section | Purpose |
|---|---|
| `engine` | Initial cash, spread, slippage, drawdown limits |
| `risk` | Daily loss limit, per-trade risk, equity floor |
| `broker` | Active broker + per-broker settings (host, demo flag, timeouts) |
| `data_apis` | Finnhub, FMP, Alpha Vantage — api_key injected from `.env` |
| `llm` | Default LLM + per-provider model, tokens, temperature |
| `services.price_monitor` | Source (`auto`/`ibkr`/`finnhub`/`fmp`), poll interval, stale thresholds |
| `telegram` | bot_token, chat_id, chart_id — injected from `.env` |
| `event_bus` | `local` (single process) or `redis` (multi-process) |
| `redis` | URL, session DB, market data DB, default TTL |
| `logging` | Log directory, file/console levels, rotation |

Typed access via `config_loader`:

```python
from core.config.config_loader import config_loader

app      = config_loader.load_app()           # full AppConfig
risk     = config_loader.load_section("risk") # RiskConfig
tg       = config_loader.load_telegram()      # TelegramConfig
apis     = config_loader.load_data_apis()     # DataApisConfig
llm      = config_loader.load_llm()           # LlmConfig
broker   = config_loader.load_broker("ibkr")  # IBKRBrokerConfig
```

### ServiceFactory

Wire services without touching `os.environ`:

```python
from core.config.config_loader import config_loader
from services.service_factory import ServiceFactory

factory = ServiceFactory(config_loader)

bus           = factory.event_bus()
redis         = factory.redis_cache()
price_monitor = factory.price_monitor(symbols=[], bus=bus)
telegram      = factory.telegram_alert_subscriber(bus=bus, exchange_source=watcher, cache=redis)
```

---

## Project Structure

```
config/
├── base.yaml                  # All defaults
├── _base_strategy.yaml        # Strategy execution defaults
└── trading/{SYMBOL}/          # Per-symbol strategy configs

src/
├── core/
│   ├── config/                # ConfigLoader, typed config models
│   ├── adapters/              # IEventBus interface, BaseSubscriber
│   ├── entities/              # Domain types (PriceTick, ScannerHit, LevelEvent, …)
│   └── utils/                 # Logging
├── adapters/
│   ├── brokers/               # Capital.com, IBKR, eToro, Scalable adapters
│   └── events/                # LocalEventBus, RedisEventBus
├── data_fetchers/             # Finnhub WS, IBKR WS, FMP, Yahoo, Alpha Vantage
├── infrastructure/
│   ├── cache/                 # RedisCache
│   ├── db/                    # asyncpg connection
│   └── gateways/
│       ├── llms/              # Anthropic, Grok, Gemini, Perplexity clients
│       └── market_data/       # Finnhub, FMP REST clients
├── services/
│   ├── price_monitor.py       # Tick feed → QUOTE_UPDATE events
│   ├── key_level_monitor_service.py  # S/R level break detection
│   ├── news_monitor_service.py       # News polling + dedup
│   ├── news_reaction_analyzer.py     # LLM-graded news reactions
│   ├── symbol_auto_watcher.py        # Scanner hit → watchlist coordinator
│   ├── morning_enrichment.py         # Prompt builder + data enrichment
│   ├── service_factory.py            # Typed service wiring
│   └── …                     # fundamentals, indicators, P&L, scheduler
├── scripts/
│   ├── run_live_monitor.py    # Main monitoring process
│   ├── run_scheduler.py       # Morning routine daemon
│   ├── run_prompt.py          # On-demand prompt generation
│   ├── run_snapshot.py        # Account snapshot
│   └── scanners/              # Pre-market, post-market, volume, scalp loops
├── interfaces/
│   ├── console/               # Local REPL (local_cli.py)
│   └── telegram/              # Bot, commands, alert subscriber
└── prompts/                   # Prompt templates (three-run morning routine)

run_scripts/                   # Shell wrappers (set PYTHONPATH + load .env)
docker/ibkr_gateway/           # IB Gateway Docker compose
data/
├── daily_morning/             # Scanner CSVs, prompt files, LLM responses
├── snapshots/                 # Broker snapshots
└── watched_symbols.json       # Live watchlist (persisted across restarts)
tests/                         # pytest suites (asyncio, all mocked)
```

---

## Daily Workflow

```
Evening (after US close)
  └─ Scheduler fires post-market scan → saves movers CSV

07:00 DE  Prompt 1 — Overnight Thesis Check
  └─ AI validates catalysts, flags gap-killers, rates durability

14:00 DE  Prompt 2 — Pre-Market Decision Run
  └─ AI builds entry plan, sizing, key levels, stop placements

16:10 DE  Prompt 3 — Opening Confirmation
  └─ AI confirms thesis against live 5-min session data + VWAP

During session
  └─ run_live_monitor watches for level breaks → Telegram alert
  └─ Execute 1–3 setups manually based on AI output

End of day
  └─ run_snapshot → review P&L
  └─ Journal every trade (no exceptions)
```

---

## Multi-Process Deployment

By default everything runs in a single process with `LocalEventBus`. To split
across processes, set `event_bus.backend: redis` in `config/production.yaml`.
The `RedisEventBus` fires local handlers with zero latency and publishes to
Redis for cross-process delivery.

Recommended split:

| Process | Services |
|---|---|
| `run_scanner` | All scanner loops → publishes `SYMBOL_DETECTED` |
| `run_monitor` | `PriceMonitor` + `KeyLevelMonitorService` + `TelegramAlertSubscriber` |
| `run_news` | `NewsMonitorService` + `NewsReactionAnalyzer` |

Each process calls `RedisEventBus.start_subscriber()` to receive events
published by the other processes. No code changes needed in the services.

# 1M — AI-Assisted Momentum Trading System

Algorithmic trading system targeting **$122,562 → $1,000,000** in 110 effective trading days (May–Nov 2026) using AI-edge catalyst momentum plays with strict risk management.

## Strategy Overview

- **Capital**: $72,562 own equity + $50,000 loan = $122,562 effective
- **Target**: $1,000,000 (~8.16x return) by Nov 2026
- **Daily compound rate needed**: 1.93% net
- **Approach**: Scan biggest movers → confirm news/catalyst edge via AI → enter 1–3 high-conviction options plays per day
- **Focus**: AI/tech, biotech, space/defense on volume spikes

### Risk Rules
| Rule | Limit |
|---|---|
| Per-trade max loss | $363–$726 (0.5–1% own equity) |
| Daily loss limit | $2,000 |
| Max open positions | 3 |
| Hard drawdown stop | $20,000 total |
| Own equity floor | $55,000 (loan guardrail) |

## Project Structure

```
src/
├── automation/          # AI analysis runner (run_ai_analysis.py)
├── core/
│   ├── adapters/        # LLM + event bus interfaces
│   ├── config/          # Config loader & Pydantic models
│   ├── entities/        # Domain entities (positions, orders, bars)
│   └── utils/           # Logging helpers, mapping
├── data_fetchers/       # Alpha Vantage, Finnhub, FMP fetchers
├── infrastructure/
│   ├── gateways/
│   │   ├── llms/        # Anthropic, Grok, Gemini, Perplexity clients
│   │   └── market_data/ # Finnhub, FMP market data clients
│   ├── cache/           # Redis cache layer
│   ├── db/              # PostgreSQL / asyncpg
│   └── repositories/    # Data access
├── interfaces/
│   ├── console/         # Local CLI (local_cli.py, super_ron.py)
│   ├── telegram/        # Telegram bot (bot.py, commands, notifier)
│   ├── api/             # REST API
│   └── web/             # Web interface
├── scripts/
│   ├── scanners/        # Pre-market, post-market, spikes, parabolic scanners
│   └── fetch_news.py
├── services/            # News, fundamentals, indicators services
├── adapters/brokers/    # Capital.com, Scalable broker adapters
├── backtest/            # Backtesting engine
├── optimizers/          # Strategy optimizers
└── research/            # Research scripts
config/
├── base.yaml            # All defaults (env, engine, risk, brokers, LLMs)
└── _base_strategy.yaml
prompts/
├── stock_mover_analysis.txt   # Main AI analysis prompt
└── earning_gap.txt
docs/
├── Master_Strategy_1M_Plan.md
└── Daily_Trade_Plan_Template.md
```

## Integrations

**Brokers**: Capital.com (primary), eToro, Scalable Capital, IBKR

**AI / LLM**: Claude (Anthropic), Grok (xAI), Gemini (Google), Perplexity

**Market Data**: Finnhub, Alpha Vantage, Financial Modeling Prep

**Notifications**: Telegram bot

**Storage**: PostgreSQL (positions/trades), Redis (session cache, event bus)

## Setup

```bash
# 1. Install dependencies (creates .venv automatically)
uv sync --dev

# 2. Install Playwright browsers
uv run playwright install chromium

# 3. Configure secrets
cp .env.example .env
# Edit .env with your API keys
```

### Required API Keys (`.env`)

```
CAPITAL_API_KEY / CAPITAL_USERNAME / CAPITAL_PASSWORD
FINNHUB_API_KEY
ALPHA_VANTAGE_API_KEY
FMP_API_KEY
DATABASE_URL=postgresql://user@localhost/trading
# LLM keys (set whichever you use):
ANTHROPIC_API_KEY
XAI_API_KEY
GEMINI_API_KEY
PERPLEXITY_API_KEY
```

## Usage

### Local CLI

```bash
./run_scripts/run_local.sh
```

### Telegram Bot

```bash
./run_scripts/run_telegram_bot.sh
```

### AI Stock Analysis

```bash
# Analyse specific tickers via Claude
uv run python src/automation/run_ai_analysis.py --tickers NVDA TSLA AAPL

# Use Grok instead
uv run python src/automation/run_ai_analysis.py --tickers NVDA --target grok

# Save output to data/ai_analysis/
uv run python src/automation/run_ai_analysis.py --tickers NVDA --save

# Pipe from scanner
uv run python src/scripts/scanners/run_post_market_scanner.py | \
    uv run python src/automation/run_ai_analysis.py --stdin --save
```

### Market Scanners

```bash
uv run --env-file .env python src/scripts/scanners/run_pre_market_scanner.py
uv run --env-file .env python src/scripts/scanners/run_post_market_scanner.py
uv run --env-file .env python src/scripts/scanners/run_spikes_scanner.py
uv run --env-file .env python src/scripts/scanners/run_parabolic_scanner.py
```

### Tests

```bash
uv run pytest
```

## Configuration

All non-secret configuration lives in `config/base.yaml`. Override by environment:

```
base.yaml → {env}.yaml → local.yaml → .env → os.environ
```

Set `RON2_ENV=production` (or `staging`) to load the matching override file.

Key config sections: `engine`, `risk`, `broker`, `llm`, `data_apis`, `event_bus`, `logging`, `redis`.

## Daily Routine

1. Run pre-market scanner → identify >15% movers + 5x relative volume
2. Run AI analysis on top candidates → get catalyst/edge summary
3. Execute 1–3 high-conviction setups (options, OTM/ATM weeklies)
4. Monitor via Telegram bot
5. Post-market journal + review

**No revenge trading. No random gambling. Journal every trade.**

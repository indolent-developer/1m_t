-- =============================================================
-- 1m trading platform — initial database schema
-- All 18 tables in one file. Run via apply_migrations() in connection.py.
-- =============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Migration tracking
CREATE TABLE IF NOT EXISTS _migrations (
    name       TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============================================================
-- BACKTEST
-- =============================================================

CREATE TABLE IF NOT EXISTS backtest_runs (
    id          UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol      VARCHAR(20)  NOT NULL,
    strategy    VARCHAR(100) NOT NULL,
    start_date  DATE         NOT NULL,
    end_date    DATE         NOT NULL,
    parameters  JSONB        NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_symbol   ON backtest_runs (symbol);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy ON backtest_runs (strategy);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_created  ON backtest_runs (created_at DESC);

CREATE TABLE IF NOT EXISTS backtest_results (
    id            UUID             PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id        UUID             NOT NULL REFERENCES backtest_runs (id) ON DELETE CASCADE,
    total_return  DOUBLE PRECISION,
    sharpe_ratio  DOUBLE PRECISION,
    max_drawdown  DOUBLE PRECISION,
    win_rate      DOUBLE PRECISION,
    total_trades  INTEGER,
    profit_factor DOUBLE PRECISION,
    metrics       JSONB            NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CONSTRAINT backtest_results_run_id_unique UNIQUE (run_id)
);

CREATE INDEX IF NOT EXISTS idx_backtest_results_run ON backtest_results (run_id);

-- Public trades table (backtest + live)
CREATE TABLE IF NOT EXISTS trades (
    id               UUID             PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id           UUID             REFERENCES backtest_runs (id) ON DELETE SET NULL,
    source           VARCHAR(20)      NOT NULL DEFAULT 'backtest',  -- backtest | live | paper
    symbol           VARCHAR(20)      NOT NULL,
    side             VARCHAR(10)      NOT NULL,
    quantity         DOUBLE PRECISION NOT NULL,
    entry_price      DOUBLE PRECISION,
    exit_price       DOUBLE PRECISION,
    entry_time       TIMESTAMPTZ,
    exit_time        TIMESTAMPTZ,
    pnl              DOUBLE PRECISION,
    pnl_pct          DOUBLE PRECISION,
    fees             DOUBLE PRECISION DEFAULT 0,
    exit_reason      VARCHAR(100),
    strategy         VARCHAR(100),
    broker           VARCHAR(50),
    strategy_version VARCHAR(50),
    metadata         JSONB            NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ      NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol     ON trades (symbol);
CREATE INDEX IF NOT EXISTS idx_trades_source     ON trades (source);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades (entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_trades_run        ON trades (run_id);
CREATE INDEX IF NOT EXISTS idx_trades_strategy   ON trades (strategy);
CREATE INDEX IF NOT EXISTS idx_trades_broker     ON trades (broker);

-- =============================================================
-- STRATEGY MONITORING
-- =============================================================

CREATE TABLE IF NOT EXISTS strategy_snapshots (
    id            BIGSERIAL    PRIMARY KEY,
    source        VARCHAR(20)  NOT NULL,
    run_id        UUID,
    symbol        VARCHAR(20)  NOT NULL,
    strategy      VARCHAR(50)  NOT NULL DEFAULT 'st2',
    ts            TIMESTAMPTZ  NOT NULL,
    price         DOUBLE PRECISION,
    volume        DOUBLE PRECISION,
    hod           DOUBLE PRECISION,
    lod           DOUBLE PRECISION,
    position_size INTEGER,
    trade_side    VARCHAR(10),
    entry_price   DOUBLE PRECISION,
    pnl           DOUBLE PRECISION,
    pnl_pct       DOUBLE PRECISION,
    initial_stop  DOUBLE PRECISION,
    trailing_stop DOUBLE PRECISION,
    trade_high    DOUBLE PRECISION,
    trade_low     DOUBLE PRECISION,
    partial_taken BOOLEAN      DEFAULT FALSE,
    decision      VARCHAR(30),
    reason        TEXT,
    regime        VARCHAR(50),
    order_id      TEXT,
    broker        VARCHAR(50),
    metadata      JSONB        NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (source, symbol, strategy, ts)
);

CREATE INDEX IF NOT EXISTS idx_ss_sym_ts   ON strategy_snapshots (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ss_source   ON strategy_snapshots (source);
CREATE INDEX IF NOT EXISTS idx_ss_decision ON strategy_snapshots (decision) WHERE decision != 'HOLD';
CREATE INDEX IF NOT EXISTS idx_ss_run      ON strategy_snapshots (run_id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ss_order_id ON strategy_snapshots (order_id) WHERE order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ss_broker   ON strategy_snapshots (broker);

CREATE TABLE IF NOT EXISTS strategy_signals (
    id                   BIGSERIAL    PRIMARY KEY,
    source               VARCHAR(20)  NOT NULL,
    run_id               VARCHAR(50),
    symbol               VARCHAR(20)  NOT NULL,
    strategy             VARCHAR(50)  NOT NULL DEFAULT 'st2',
    ts                   TIMESTAMPTZ  NOT NULL,
    timeframe            VARCHAR(10)  NOT NULL,
    broker               VARCHAR(50),
    atr                  DOUBLE PRECISION,
    adx                  DOUBLE PRECISION,
    rsi                  DOUBLE PRECISION,
    vwap                 DOUBLE PRECISION,
    rvol                 DOUBLE PRECISION,
    rvat                 DOUBLE PRECISION,
    supertrend_value     DOUBLE PRECISION,
    supertrend_dir       SMALLINT,
    s_r_resistance       DOUBLE PRECISION,
    s_r_support          DOUBLE PRECISION,
    sig_long_bullish     BOOLEAN,
    sig_long_bearish     BOOLEAN,
    sig_long_direction   SMALLINT,
    sig_long_supertrend  DOUBLE PRECISION,
    sig_long_close       DOUBLE PRECISION,
    sig_long_entry_price DOUBLE PRECISION,
    sig_long_exit_price  DOUBLE PRECISION,
    sig_long_crossed     BOOLEAN,
    sig_short_bullish    BOOLEAN,
    sig_short_bearish    BOOLEAN,
    sig_short_direction  SMALLINT,
    sig_short_supertrend DOUBLE PRECISION,
    sig_short_close      DOUBLE PRECISION,
    sig_short_entry_price DOUBLE PRECISION,
    sig_short_exit_price  DOUBLE PRECISION,
    sig_short_crossed    BOOLEAN,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (source, run_id, symbol, strategy, ts, timeframe, broker)
);

CREATE INDEX IF NOT EXISTS idx_sg_sym_tf_ts ON strategy_signals (symbol, timeframe, ts DESC);
CREATE INDEX IF NOT EXISTS idx_sg_run       ON strategy_signals (run_id) WHERE run_id IS NOT NULL;

-- =============================================================
-- DEAL SCHEMA  (live trading)
-- =============================================================

CREATE SCHEMA IF NOT EXISTS deal;

CREATE TABLE IF NOT EXISTS deal.accounts (
    account_id   SERIAL      PRIMARY KEY,
    broker       VARCHAR(50) NOT NULL,
    account_ref  VARCHAR(100),
    label        VARCHAR(100),
    currency     VARCHAR(10) NOT NULL DEFAULT 'USD',
    account_type VARCHAR(10) NOT NULL DEFAULT 'LIVE'
                 CHECK (account_type IN ('LIVE', 'DEMO', 'PAPER')),
    is_active    BOOLEAN     NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_deal_accounts_broker ON deal.accounts (broker);
CREATE INDEX IF NOT EXISTS idx_deal_accounts_type   ON deal.accounts (account_type);

CREATE TABLE IF NOT EXISTS deal.instruments (
    instrument_id SERIAL       PRIMARY KEY,
    symbol        VARCHAR(20)  NOT NULL,
    name          VARCHAR(200),
    asset_type    VARCHAR(30),
    currency      VARCHAR(10)  NOT NULL DEFAULT 'USD',
    exchange      VARCHAR(50),
    broker_symbols JSONB       NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (symbol, exchange)
);

CREATE INDEX IF NOT EXISTS idx_deal_instruments_symbol ON deal.instruments (symbol);

CREATE TABLE IF NOT EXISTS deal.deals (
    deal_id              BIGSERIAL    PRIMARY KEY,
    external_deal_ref    VARCHAR(100) UNIQUE,
    account_id           INT          NOT NULL REFERENCES deal.accounts    (account_id),
    instrument_id        INT          NOT NULL REFERENCES deal.instruments (instrument_id),
    type                 VARCHAR(30)  NOT NULL,
    status               VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
    net_quantity_before  NUMERIC(18,8),
    net_quantity_after   NUMERIC(18,8),
    avg_price_before     NUMERIC(18,8),
    avg_price_after      NUMERIC(18,8),
    realized_pnl         NUMERIC(18,8) NOT NULL DEFAULT 0,
    deal_time            TIMESTAMPTZ  NOT NULL,
    raw_confirms         JSONB        NOT NULL DEFAULT '{}',
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_deal_deals_account    ON deal.deals (account_id);
CREATE INDEX IF NOT EXISTS idx_deal_deals_instrument ON deal.deals (instrument_id);
CREATE INDEX IF NOT EXISTS idx_deal_deals_time       ON deal.deals (deal_time DESC);
CREATE INDEX IF NOT EXISTS idx_deal_deals_status     ON deal.deals (status);

CREATE TABLE IF NOT EXISTS deal.orders (
    order_id          BIGSERIAL    PRIMARY KEY,
    external_order_id VARCHAR(100),
    account_id        INT          NOT NULL REFERENCES deal.accounts (account_id),
    deal_id           BIGINT       REFERENCES deal.deals (deal_id),
    type              VARCHAR(20)  NOT NULL,
    side              VARCHAR(4)   NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity          NUMERIC(18,8) NOT NULL,
    limit_price       NUMERIC(18,8),
    stop_price        NUMERIC(18,8),
    status            VARCHAR(30)  NOT NULL DEFAULT 'PENDING',
    submitted_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    raw_request       JSONB        NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_deal_orders_account ON deal.orders (account_id);
CREATE INDEX IF NOT EXISTS idx_deal_orders_deal    ON deal.orders (deal_id);
CREATE INDEX IF NOT EXISTS idx_deal_orders_status  ON deal.orders (status);
CREATE INDEX IF NOT EXISTS idx_deal_orders_time    ON deal.orders (submitted_at DESC);

CREATE TABLE IF NOT EXISTS deal.trades (
    trade_id          BIGSERIAL    PRIMARY KEY,
    external_trade_id VARCHAR(100) UNIQUE,
    order_id          BIGINT       NOT NULL REFERENCES deal.orders (order_id),
    deal_id           BIGINT       NOT NULL REFERENCES deal.deals  (deal_id),
    side              VARCHAR(4)   NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity          NUMERIC(18,8) NOT NULL,
    price             NUMERIC(18,8) NOT NULL,
    commission        NUMERIC(18,8) NOT NULL DEFAULT 0,
    trade_time        TIMESTAMPTZ  NOT NULL,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_deal_trades_order ON deal.trades (order_id);
CREATE INDEX IF NOT EXISTS idx_deal_trades_deal  ON deal.trades (deal_id);
CREATE INDEX IF NOT EXISTS idx_deal_trades_time  ON deal.trades (trade_time DESC);

CREATE TABLE IF NOT EXISTS deal.positions (
    position_id     BIGSERIAL    PRIMARY KEY,
    account_id      INT          NOT NULL REFERENCES deal.accounts    (account_id),
    instrument_id   INT          NOT NULL REFERENCES deal.instruments (instrument_id),
    version         INT          NOT NULL DEFAULT 1,
    is_active       BOOLEAN      NOT NULL DEFAULT true,
    net_quantity    NUMERIC(18,8) NOT NULL DEFAULT 0,
    avg_entry_price NUMERIC(18,8),
    realized_pnl    NUMERIC(18,8) NOT NULL DEFAULT 0,
    unrealized_pnl  NUMERIC(18,8) NOT NULL DEFAULT 0,
    stop_loss       NUMERIC(18,8),
    take_profit     NUMERIC(18,8),
    opened_at       TIMESTAMPTZ,
    last_updated    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    current_deal_id BIGINT       REFERENCES deal.deals (deal_id),
    UNIQUE (account_id, instrument_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_deal_one_active_position
    ON deal.positions (account_id, instrument_id)
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_deal_positions_account    ON deal.positions (account_id);
CREATE INDEX IF NOT EXISTS idx_deal_positions_instrument ON deal.positions (instrument_id);
CREATE INDEX IF NOT EXISTS idx_deal_positions_active     ON deal.positions (is_active)
    WHERE is_active = true;

-- =============================================================
-- LIVE STATE & PORTFOLIO
-- =============================================================

CREATE TABLE IF NOT EXISTS live_state (
    key        TEXT        PRIMARY KEY,
    value      JSONB       NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_live_state_key_prefix ON live_state (key text_pattern_ops);

CREATE TABLE IF NOT EXISTS portfolio_items (
    id         UUID             PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol     VARCHAR(20)      NOT NULL,
    name       VARCHAR(200),
    quantity   DOUBLE PRECISION NOT NULL DEFAULT 0,
    leverage   DOUBLE PRECISION NOT NULL DEFAULT 1,
    buy_price  DOUBLE PRECISION,
    sell_price DOUBLE PRECISION,
    side       VARCHAR(10)      NOT NULL DEFAULT 'long',
    broker     VARCHAR(50),
    notes      TEXT,
    -- execution tracking
    order_price      DOUBLE PRECISION,
    order_date       TIMESTAMPTZ,
    execution_price  DOUBLE PRECISION,
    execution_date   TIMESTAMPTZ,
    close_date       TIMESTAMPTZ,
    stop_price       DOUBLE PRECISION,
    invested_amount  DOUBLE PRECISION,
    pnl_amount       DOUBLE PRECISION,
    pnl_pct          DOUBLE PRECISION,
    currency         VARCHAR(10)  DEFAULT 'USD',
    trading_venue    VARCHAR(100),
    transaction_ref  VARCHAR(100),
    asset_type       VARCHAR(30),
    broker_position_id VARCHAR(100),
    take_profit      DOUBLE PRECISION,
    margin           DOUBLE PRECISION,
    exposure         DOUBLE PRECISION,
    overnight_fees   DOUBLE PRECISION DEFAULT 0,
    -- ibkr fields
    unrealized_pnl   DOUBLE PRECISION,
    realized_pnl     DOUBLE PRECISION,
    daily_pnl        DOUBLE PRECISION,
    market_value     DOUBLE PRECISION,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_portfolio_symbol ON portfolio_items (symbol);
CREATE INDEX IF NOT EXISTS idx_portfolio_broker ON portfolio_items (broker);

CREATE TABLE IF NOT EXISTS stock_notes (
    id         UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol     TEXT        NOT NULL,
    note_text  TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stock_notes_symbol ON stock_notes (symbol, created_at DESC);

-- =============================================================
-- ANALYTICS (new tables)
-- =============================================================

CREATE TABLE IF NOT EXISTS scanner_results (
    id         BIGSERIAL   PRIMARY KEY,
    scanner    TEXT        NOT NULL,
    symbol     TEXT        NOT NULL,
    name       TEXT,
    price      NUMERIC,
    chg_pct    NUMERIC,
    chg_1m_pct NUMERIC,
    relvol     NUMERIC,
    market_cap NUMERIC,
    rsi        NUMERIC,
    sector     TEXT,
    scanned_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scanner_results_scanner ON scanner_results (scanner, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_scanner_results_symbol  ON scanner_results (symbol, scanned_at DESC);

CREATE TABLE IF NOT EXISTS equity_log (
    id             BIGSERIAL   PRIMARY KEY,
    account_id     INT         NOT NULL REFERENCES deal.accounts (account_id),
    broker_id      TEXT        NOT NULL,
    total_value    NUMERIC     NOT NULL,
    cash           NUMERIC,
    unrealized_pnl NUMERIC,
    logged_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_equity_log_account ON equity_log (account_id, logged_at DESC);

-- =============================================================
-- OPTIMISATION
-- =============================================================

CREATE SCHEMA IF NOT EXISTS optuna;

CREATE TABLE IF NOT EXISTS optuna.optimization_runs (
    id          UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol      VARCHAR(20)  NOT NULL,
    strategy    VARCHAR(100) NOT NULL,
    best_params JSONB        NOT NULL DEFAULT '{}',
    best_value  DOUBLE PRECISION,
    n_trials    INTEGER,
    metadata    JSONB        NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_opt_runs_symbol   ON optuna.optimization_runs (symbol);
CREATE INDEX IF NOT EXISTS idx_opt_runs_strategy ON optuna.optimization_runs (strategy);

CREATE TABLE IF NOT EXISTS opt_trials (
    id                      BIGSERIAL     PRIMARY KEY,
    created_at              TIMESTAMPTZ   NOT NULL DEFAULT now(),
    batch_id                TEXT          NOT NULL,
    study_name              TEXT          NOT NULL,
    trial_number            INTEGER       NOT NULL,
    symbol                  TEXT          NOT NULL,
    strategy                TEXT          NOT NULL DEFAULT 'st_fast',
    data_source             TEXT          NOT NULL,
    direction               TEXT          NOT NULL,
    start_date              DATE          NOT NULL,
    end_date                DATE          NOT NULL,
    initial_cash            NUMERIC(18,4) NOT NULL,
    final_cash              NUMERIC(18,4),
    net_pnl                 NUMERIC(18,4),
    total_trades            INTEGER,
    win_rate                NUMERIC(8,4),
    sharpe_ratio            NUMERIC(8,4),
    max_drawdown_pct        NUMERIC(8,4),
    profit_factor           NUMERIC(8,4),
    avg_trade_duration_mins NUMERIC(10,2),
    objective_names         TEXT[],
    objective_values        DOUBLE PRECISION[],
    duration_secs           NUMERIC(10,2),
    is_error                BOOLEAN       NOT NULL DEFAULT FALSE,
    error_text              TEXT,
    params_json             JSONB,
    config_json             JSONB,
    UNIQUE (study_name, trial_number)
);

CREATE INDEX IF NOT EXISTS idx_opt_trials_symbol_batch ON opt_trials (symbol, batch_id);
CREATE INDEX IF NOT EXISTS idx_opt_trials_study        ON opt_trials (study_name);
CREATE INDEX IF NOT EXISTS idx_opt_trials_params       ON opt_trials USING gin (params_json);

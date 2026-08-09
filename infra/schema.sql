-- MarketShark Trader schema.
--
-- Two kinds of table here and the distinction matters:
--
--   * Time-series tables (bars, option_quotes) are Timescale hypertables. They
--     grow without bound and are almost always queried by a time range on one
--     symbol, which is exactly what hypertable chunking is for.
--   * Everything else is ordinary Postgres.
--
-- Saved strategies store the *legs*, not the computed analytics. Analytics are
-- a pure function of legs plus a market snapshot, so persisting them would just
-- create a second source of truth that goes stale. Recompute on read.

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- reference

CREATE TABLE IF NOT EXISTS instruments (
    symbol            TEXT PRIMARY KEY,
    name              TEXT,
    exchange          TEXT,
    sector            TEXT,
    industry          TEXT,
    market_cap        NUMERIC,
    shares_outstanding NUMERIC,
    is_optionable     BOOLEAN DEFAULT TRUE,
    dividend_yield    NUMERIC,
    updated_at        TIMESTAMPTZ DEFAULT now()
);

-- ------------------------------------------------------------- time series

CREATE TABLE IF NOT EXISTS bars (
    symbol    TEXT        NOT NULL,
    timespan  TEXT        NOT NULL DEFAULT 'day',
    ts        TIMESTAMPTZ NOT NULL,
    open      NUMERIC     NOT NULL,
    high      NUMERIC     NOT NULL,
    low       NUMERIC     NOT NULL,
    close     NUMERIC     NOT NULL,
    volume    NUMERIC,
    source    TEXT,
    PRIMARY KEY (symbol, timespan, ts)
);
SELECT create_hypertable('bars', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS bars_symbol_ts ON bars (symbol, ts DESC);

CREATE TABLE IF NOT EXISTS option_quotes (
    contract_symbol TEXT        NOT NULL,
    underlying      TEXT        NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    expiration      DATE        NOT NULL,
    strike          NUMERIC     NOT NULL,
    right           TEXT        NOT NULL CHECK (right IN ('call', 'put')),
    bid             NUMERIC,
    ask             NUMERIC,
    last            NUMERIC,
    volume          NUMERIC,
    open_interest   NUMERIC,
    implied_vol     NUMERIC,
    delta           NUMERIC,
    gamma           NUMERIC,
    theta           NUMERIC,
    vega            NUMERIC,
    underlying_price NUMERIC,
    source          TEXT,
    PRIMARY KEY (contract_symbol, ts)
);
SELECT create_hypertable('option_quotes', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS oq_underlying_exp ON option_quotes (underlying, expiration, ts DESC);

-- Daily ATM implied vol, so IV rank has real history behind it instead of
-- being inferred from whatever the chain happens to quote today.
--
-- One row per symbol per day, enforced. Re-running the snapshot job or
-- restarting the service must not double-weight a day, or every IV rank
-- computed afterwards is quietly distorted.
CREATE TABLE IF NOT EXISTS iv_history (
    id           BIGSERIAL PRIMARY KEY,
    symbol       TEXT        NOT NULL,
    observed_on  DATE        NOT NULL,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    atm_iv_30d   NUMERIC     NOT NULL,
    realized_20d NUMERIC,
    spot         NUMERIC,
    source       TEXT,
    CONSTRAINT uq_iv_symbol_day UNIQUE (symbol, observed_on)
);
CREATE INDEX IF NOT EXISTS ix_iv_symbol_date ON iv_history (symbol, observed_on);

-- Cached earnings dates. The good feeds are metered (Benzinga is a paid add-on,
-- Webull fundamentals is 60/min) and a date changes at most a few times a
-- quarter, so the database is the primary read path.
--
-- date_status is preserved rather than flattened: a projected date can move by
-- a week, and an expiry chosen to "avoid earnings" against a projected date is
-- a false sense of safety.
CREATE TABLE IF NOT EXISTS earnings_events (
    id               BIGSERIAL PRIMARY KEY,
    symbol           TEXT NOT NULL,
    event_date       DATE NOT NULL,
    session          TEXT NOT NULL DEFAULT 'unknown'
                     CHECK (session IN ('bmo', 'amc', 'dmh', 'unknown')),
    date_status      TEXT NOT NULL DEFAULT 'projected'
                     CHECK (date_status IN ('confirmed', 'projected')),
    fiscal_period    TEXT,
    fiscal_year      INTEGER,
    eps_estimate     NUMERIC,
    eps_actual       NUMERIC,
    revenue_estimate NUMERIC,
    revenue_actual   NUMERIC,
    source           TEXT,
    is_synthetic     BOOLEAN DEFAULT FALSE,
    fetched_at       TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_earnings_symbol_date UNIQUE (symbol, event_date)
);
CREATE INDEX IF NOT EXISTS ix_earnings_symbol ON earnings_events (symbol, event_date);
CREATE INDEX IF NOT EXISTS ix_earnings_date ON earnings_events (event_date);

-- ------------------------------------------------------------------ users

CREATE TABLE IF NOT EXISTS users (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email      TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS watchlists (
    id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    name    TEXT NOT NULL DEFAULT 'Default',
    symbols TEXT[] NOT NULL DEFAULT '{}'
);

-- ------------------------------------------------------- saved strategies

CREATE TABLE IF NOT EXISTS saved_strategies (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID REFERENCES users(id) ON DELETE CASCADE,
    symbol       TEXT NOT NULL,
    template_id  TEXT,
    name         TEXT NOT NULL,
    params       JSONB NOT NULL DEFAULT '{}',
    legs         JSONB NOT NULL,
    entry_spot   NUMERIC,
    notes        TEXT,
    created_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS saved_user ON saved_strategies (user_id, created_at DESC);

-- ------------------------------------------------------------- positions

CREATE TABLE IF NOT EXISTS positions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    symbol      TEXT NOT NULL,
    name        TEXT NOT NULL,
    legs        JSONB NOT NULL,
    opened_at   DATE NOT NULL DEFAULT CURRENT_DATE,
    closed_at   DATE,
    entry_debit NUMERIC,
    exit_credit NUMERIC,
    broker      TEXT,
    broker_order_id TEXT,
    notes       TEXT,
    -- What the engine predicted at entry, frozen at open and never rewritten.
    -- This is the input to the calibration report: comparing a stored
    -- prediction to a realised outcome is the only way to find out whether the
    -- probability engine is honest. A prediction that gets updated as evidence
    -- arrives is not a prediction, so this column is write-once by convention.
    expectation JSONB
);
CREATE INDEX IF NOT EXISTS positions_open ON positions (user_id) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS positions_paper_closed ON positions (broker, closed_at)
    WHERE broker = 'paper' AND closed_at IS NOT NULL;

-- Point-in-time marks, so the equity curve is recorded rather than
-- reconstructed from assumptions after the fact.
CREATE TABLE IF NOT EXISTS position_marks (
    position_id UUID        NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL,
    underlying  NUMERIC,
    mark_value  NUMERIC,
    unrealized  NUMERIC,
    delta       NUMERIC,
    gamma       NUMERIC,
    theta       NUMERIC,
    vega        NUMERIC,
    PRIMARY KEY (position_id, ts)
);
SELECT create_hypertable('position_marks', 'ts', if_not_exists => TRUE);

-- -------------------------------------------------------------- backtests

-- Rules, metrics, trades and the equity curve are JSON: written once, read
-- whole. There is no query that wants "all trades across all runs where..."
-- that a report would not answer better, so normalizing would buy a join and
-- nothing else.
--
-- quote_source is a first-class column because it is the single most important
-- fact about a result. A modelled run and a historical run are not comparable,
-- and six months later nobody remembers which was which.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id               UUID REFERENCES users(id) ON DELETE CASCADE,
    symbol                TEXT NOT NULL,
    template_id           TEXT NOT NULL,
    label                 TEXT DEFAULT '',
    rules                 JSONB NOT NULL DEFAULT '{}',
    metrics               JSONB NOT NULL DEFAULT '{}',
    trades                JSONB NOT NULL DEFAULT '[]',
    equity_curve          JSONB NOT NULL DEFAULT '[]',
    start_date            TEXT,
    end_date              TEXT,
    quote_source          TEXT NOT NULL DEFAULT 'modelled',
    is_historical_pricing BOOLEAN NOT NULL DEFAULT FALSE,
    created_at            TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS backtest_recent ON backtest_runs (symbol, created_at DESC);

-- ---------------------------------------------------------------- agents

CREATE TABLE IF NOT EXISTS agent_runs (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id   TEXT NOT NULL,
    started_at TIMESTAMPTZ DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status     TEXT NOT NULL DEFAULT 'running',
    input      JSONB,
    output     JSONB,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS agent_runs_recent ON agent_runs (agent_id, started_at DESC);

-- Every proposed order lands here first. Nothing reaches a broker without a
-- row in this table showing the risk gate approved it -- an audit trail that
-- exists whether or not anyone is watching.
CREATE TABLE IF NOT EXISTS order_proposals (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id      TEXT,
    user_id       UUID REFERENCES users(id) ON DELETE SET NULL,
    symbol        TEXT NOT NULL,
    payload       JSONB NOT NULL,
    risk_verdict  TEXT NOT NULL DEFAULT 'pending'
                  CHECK (risk_verdict IN ('pending', 'approved', 'rejected')),
    risk_reason   TEXT,
    submitted_at  TIMESTAMPTZ,
    broker_order_id TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS proposals_pending ON order_proposals (created_at DESC)
    WHERE risk_verdict = 'pending';

-- ------------------------------------------------------------------- news

CREATE TABLE IF NOT EXISTS news_items (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    published    TIMESTAMPTZ NOT NULL,
    publisher    TEXT,
    summary      TEXT,
    image_url    TEXT,
    tickers      TEXT[],
    sentiment    TEXT,
    media_type   TEXT DEFAULT 'article'
);
CREATE INDEX IF NOT EXISTS news_recent ON news_items (published DESC);
CREATE INDEX IF NOT EXISTS news_tickers ON news_items USING GIN (tickers);

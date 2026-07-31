-- Polymarket Smart Money Tracker · Phase 1 schema
-- Run this against your PostgreSQL before first collection.
-- Usage: psql -U postgres -d polymarket_smart_money -f smart_money/migrations/001_init.sql

BEGIN;

-- =============================================================================
-- 1. Traders
-- =============================================================================
CREATE TABLE IF NOT EXISTS smart_money_traders (
    wallet                          VARCHAR(42)     PRIMARY KEY,
    username                        VARCHAR(255),
    pseudonym                       VARCHAR(255),
    profile_image                   TEXT,
    x_username                      VARCHAR(255),
    verified                        BOOLEAN         NOT NULL DEFAULT FALSE,
    tracked                         BOOLEAN         NOT NULL DEFAULT TRUE,
    first_seen_at                   TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    last_seen_at                    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    last_active_at                  TIMESTAMPTZ,
    last_collected_at               TIMESTAMPTZ
);

-- =============================================================================
-- 2. Leaderboard snapshots
-- =============================================================================
CREATE TABLE IF NOT EXISTS smart_money_leaderboard_entries (
    id                              SERIAL          PRIMARY KEY,
    collected_at                     TIMESTAMPTZ     NOT NULL,
    category                         VARCHAR(32)     NOT NULL,
    time_period                      VARCHAR(16)     NOT NULL,
    rank                            INTEGER         NOT NULL,
    wallet                          VARCHAR(42)     NOT NULL REFERENCES smart_money_traders(wallet) ON DELETE CASCADE,
    pnl                             NUMERIC(24,8)   NOT NULL DEFAULT 0,
    volume                          NUMERIC(24,8)   NOT NULL DEFAULT 0,
    raw                             JSONB           NOT NULL DEFAULT '{}',
    CONSTRAINT uq_lb_snapshot_wallet UNIQUE (collected_at, category, time_period, wallet)
);
CREATE INDEX IF NOT EXISTS ix_smart_money_leaderboard_latest
    ON smart_money_leaderboard_entries (category, time_period, collected_at, rank);

-- =============================================================================
-- 3. Markets (from Gamma)
-- =============================================================================
CREATE TABLE IF NOT EXISTS smart_money_markets (
    condition_id                     VARCHAR(66)     PRIMARY KEY,
    gamma_id                        VARCHAR(64),
    question                        TEXT            NOT NULL DEFAULT '',
    slug                            VARCHAR(512),
    event_slug                      VARCHAR(512),
    category                        VARCHAR(128),
    start_time                      TIMESTAMPTZ,
    end_time                        TIMESTAMPTZ,
    volume                          NUMERIC(24,8),
    liquidity                       NUMERIC(24,8),
    active                          BOOLEAN,
    closed                          BOOLEAN,
    token_yes                       VARCHAR(128),
    token_no                        VARCHAR(128),
    outcomes                        JSONB           NOT NULL DEFAULT '[]',
    outcome_prices                  JSONB           NOT NULL DEFAULT '[]',
    raw                             JSONB           NOT NULL DEFAULT '{}',
    updated_at                       TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_smart_money_markets_end_time   ON smart_money_markets (end_time);
CREATE INDEX IF NOT EXISTS ix_smart_money_markets_category   ON smart_money_markets (category);

-- =============================================================================
-- 4. Trades (fingerprint = pk, upsert-safe)
-- =============================================================================
CREATE TABLE IF NOT EXISTS smart_money_trades (
    fingerprint                     VARCHAR(64)     PRIMARY KEY,
    wallet                          VARCHAR(42)     NOT NULL REFERENCES smart_money_traders(wallet) ON DELETE CASCADE,
    condition_id                    VARCHAR(66)     NOT NULL REFERENCES smart_money_markets(condition_id) ON DELETE CASCADE,
    token_id                       VARCHAR(128),
    transaction_hash                VARCHAR(128),
    side                           VARCHAR(8)      NOT NULL,
    outcome                        VARCHAR(255)    NOT NULL DEFAULT '',
    outcome_index                  INTEGER,
    price                          NUMERIC(18,10)  NOT NULL,
    size                           NUMERIC(24,8)   NOT NULL,
    amount                         NUMERIC(24,8)   NOT NULL,
    traded_at                      TIMESTAMPTZ     NOT NULL,
    title                          TEXT,
    slug                           VARCHAR(512),
    event_slug                     VARCHAR(512),
    raw                            JSONB           NOT NULL DEFAULT '{}',
    collected_at                    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_smart_money_trades_wallet_time  ON smart_money_trades (wallet, traded_at);
CREATE INDEX IF NOT EXISTS ix_smart_money_trades_market_time  ON smart_money_trades (condition_id, traded_at);
CREATE INDEX IF NOT EXISTS ix_smart_money_trades_recent       ON smart_money_trades (traded_at, side);

-- =============================================================================
-- 5. Current positions (full-replace per wallet)
-- =============================================================================
CREATE TABLE IF NOT EXISTS smart_money_current_positions (
    wallet                          VARCHAR(42)     NOT NULL REFERENCES smart_money_traders(wallet) ON DELETE CASCADE,
    token_id                       VARCHAR(128)    NOT NULL,
    condition_id                    VARCHAR(66)     NOT NULL REFERENCES smart_money_markets(condition_id) ON DELETE CASCADE,
    outcome                        VARCHAR(255)    NOT NULL DEFAULT '',
    outcome_index                  INTEGER,
    size                           NUMERIC(24,8)  NOT NULL,
    avg_price                      NUMERIC(18,10)  NOT NULL,
    current_price                  NUMERIC(18,10)  NOT NULL,
    initial_value                  NUMERIC(24,8)   NOT NULL DEFAULT 0,
    current_value                  NUMERIC(24,8)   NOT NULL DEFAULT 0,
    cash_pnl                       NUMERIC(24,8)   NOT NULL DEFAULT 0,
    realized_pnl                   NUMERIC(24,8)   NOT NULL DEFAULT 0,
    percent_pnl                    NUMERIC(18,10)  NOT NULL DEFAULT 0,
    total_bought                   NUMERIC(24,8)   NOT NULL DEFAULT 0,
    title                          TEXT,
    slug                           VARCHAR(512),
    event_slug                     VARCHAR(512),
    end_time                       TIMESTAMPTZ,
    first_observed_at               TIMESTAMPTZ     NOT NULL,
    observed_at                    TIMESTAMPTZ     NOT NULL,
    raw                            JSONB           NOT NULL DEFAULT '{}',
    PRIMARY KEY (wallet, token_id)
);
CREATE INDEX IF NOT EXISTS ix_smart_money_positions_market     ON smart_money_current_positions (condition_id, outcome);
CREATE INDEX IF NOT EXISTS ix_smart_money_positions_observed   ON smart_money_current_positions (observed_at);

-- =============================================================================
-- 6. Position snapshots (append-only history)
-- =============================================================================
CREATE TABLE IF NOT EXISTS smart_money_position_snapshots (
    fingerprint                     VARCHAR(64)     PRIMARY KEY,
    observed_at                    TIMESTAMPTZ     NOT NULL,
    wallet                          VARCHAR(42)    NOT NULL REFERENCES smart_money_traders(wallet) ON DELETE CASCADE,
    condition_id                    VARCHAR(66)    NOT NULL REFERENCES smart_money_markets(condition_id) ON DELETE CASCADE,
    token_id                       VARCHAR(128)    NOT NULL,
    outcome                        VARCHAR(255)    NOT NULL DEFAULT '',
    size                           NUMERIC(24,8)   NOT NULL,
    avg_price                      NUMERIC(18,10)  NOT NULL,
    current_price                  NUMERIC(18,10) NOT NULL,
    current_value                  NUMERIC(24,8)  NOT NULL,
    cash_pnl                       NUMERIC(24,8)  NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_smart_money_position_history ON smart_money_position_snapshots (wallet, observed_at);

-- =============================================================================
-- 7. Closed positions (90-day realised-PnL window)
-- =============================================================================
CREATE TABLE IF NOT EXISTS smart_money_closed_positions (
    fingerprint                     VARCHAR(64)     PRIMARY KEY,
    wallet                          VARCHAR(42)    NOT NULL REFERENCES smart_money_traders(wallet) ON DELETE CASCADE,
    condition_id                    VARCHAR(66)    NOT NULL REFERENCES smart_money_markets(condition_id) ON DELETE CASCADE,
    token_id                       VARCHAR(128)    NOT NULL,
    outcome                        VARCHAR(255)    NOT NULL DEFAULT '',
    avg_price                      NUMERIC(18,10) NOT NULL,
    total_bought                   NUMERIC(24,8)  NOT NULL DEFAULT 0,
    realized_pnl                    NUMERIC(24,8) NOT NULL DEFAULT 0,
    current_price                  NUMERIC(18,10) NOT NULL DEFAULT 0,
    closed_at                      TIMESTAMPTZ    NOT NULL,
    title                          TEXT,
    slug                           VARCHAR(512),
    event_slug                     VARCHAR(512),
    end_time                       TIMESTAMPTZ,
    raw                            JSONB          NOT NULL DEFAULT '{}',
    collected_at                    TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_smart_money_closed_wallet_time ON smart_money_closed_positions (wallet, closed_at);
CREATE INDEX IF NOT EXISTS ix_smart_money_closed_pnl_time    ON smart_money_closed_positions (closed_at, realized_pnl);

-- =============================================================================
-- 8. Collection run history
-- =============================================================================
CREATE TABLE IF NOT EXISTS smart_money_collection_runs (
    id                              SERIAL          PRIMARY KEY,
    job_name                         VARCHAR(64)     NOT NULL,
    status                          VARCHAR(16)     NOT NULL,
    started_at                       TIMESTAMPTZ     NOT NULL,
    finished_at                      TIMESTAMPTZ,
    rows_seen                       INTEGER         NOT NULL DEFAULT 0,
    rows_written                    INTEGER         NOT NULL DEFAULT 0,
    error                           TEXT,
    details                         JSONB           NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_smart_money_runs_job_time ON smart_money_collection_runs (job_name, started_at);

COMMIT;

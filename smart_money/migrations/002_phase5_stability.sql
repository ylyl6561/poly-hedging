-- Phase 5: stability-driven follow-list selection
-- Run this to add the new window-score columns and the manual-follow table.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. smart_money_window_scores — add stability metrics columns
-- ---------------------------------------------------------------------------
ALTER TABLE smart_money_window_scores
    ADD COLUMN IF NOT EXISTS profit_days           INTEGER  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS loss_days             INTEGER  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS longest_win_streak    INTEGER  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_drawdown_pct      DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS daily_pnl_stddev      DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS earliest_trade_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS period_days           DOUBLE PRECISION NOT NULL DEFAULT 0;

-- ---------------------------------------------------------------------------
-- 2. smart_money_follow_list — add source/note for manual entries
-- ---------------------------------------------------------------------------
ALTER TABLE smart_money_follow_list
    ADD COLUMN IF NOT EXISTS source   VARCHAR(16) NOT NULL DEFAULT 'auto',
    ADD COLUMN IF NOT EXISTS note     TEXT;

-- ---------------------------------------------------------------------------
-- 3. smart_money_manual_follow — operator-curated overrides
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS smart_money_manual_follow (
    wallet        VARCHAR(42)  PRIMARY KEY,
    username      VARCHAR(255),
    note          TEXT,
    added_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_manual_follow_added_at
    ON smart_money_manual_follow (added_at);

COMMIT;
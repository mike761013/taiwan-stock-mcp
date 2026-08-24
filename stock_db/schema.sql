CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS securities (
    symbol VARCHAR(16) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    market VARCHAR(16) NOT NULL,
    industry VARCHAR(100),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS daily_bars (
    symbol VARCHAR(16) NOT NULL REFERENCES securities(symbol)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    trade_date DATE NOT NULL,
    open NUMERIC(14,4),
    high NUMERIC(14,4),
    low NUMERIC(14,4),
    close NUMERIC(14,4),
    volume BIGINT,
    turnover BIGINT,
    change_percent NUMERIC(10,4),
    source VARCHAR(32) NOT NULL DEFAULT 'unknown',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, trade_date),
    CHECK (volume IS NULL OR volume >= 0),
    CHECK (turnover IS NULL OR turnover >= 0)
);

CREATE TABLE IF NOT EXISTS daily_indicators (
    symbol VARCHAR(16) NOT NULL,
    trade_date DATE NOT NULL,
    ma5 NUMERIC(14,4),
    ma10 NUMERIC(14,4),
    ma20 NUMERIC(14,4),
    ma60 NUMERIC(14,4),
    volume_ma5 NUMERIC(20,2),
    volume_ma20 NUMERIC(20,2),
    bollinger_mid NUMERIC(14,4),
    bollinger_upper NUMERIC(14,4),
    bollinger_lower NUMERIC(14,4),
    volume_ratio NUMERIC(12,4),
    volatility_20 NUMERIC(12,6),
    large_volume_low NUMERIC(14,4),
    technical_score NUMERIC(8,4),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, trade_date),
    FOREIGN KEY (symbol, trade_date)
        REFERENCES daily_bars(symbol, trade_date) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS radar_runs (
    id BIGSERIAL PRIMARY KEY,
    strategy VARCHAR(32) NOT NULL,
    run_date DATE NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status VARCHAR(20) NOT NULL DEFAULT 'running',
    universe_count INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    configuration JSONB NOT NULL DEFAULT '{}'::JSONB,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS radar_candidates (
    radar_run_id BIGINT NOT NULL REFERENCES radar_runs(id) ON DELETE CASCADE,
    symbol VARCHAR(16) NOT NULL REFERENCES securities(symbol) ON DELETE RESTRICT,
    rank INTEGER,
    total_score NUMERIC(8,4),
    technical_score NUMERIC(8,4),
    chip_score NUMERIC(8,4),
    theme_score NUMERIC(8,4),
    fundamental_score NUMERIC(8,4),
    risk_score NUMERIC(8,4),
    reasons JSONB NOT NULL DEFAULT '[]'::JSONB,
    snapshot JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (radar_run_id, symbol)
);

CREATE TABLE IF NOT EXISTS signal_performance (
    radar_run_id BIGINT NOT NULL,
    symbol VARCHAR(16) NOT NULL,
    entry_date DATE NOT NULL,
    entry_close NUMERIC(14,4),
    return_d1 NUMERIC(10,4),
    return_d3 NUMERIC(10,4),
    return_d5 NUMERIC(10,4),
    return_d10 NUMERIC(10,4),
    return_d20 NUMERIC(10,4),
    max_favorable_percent NUMERIC(10,4),
    max_adverse_percent NUMERIC(10,4),
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (radar_run_id, symbol),
    FOREIGN KEY (radar_run_id, symbol)
        REFERENCES radar_candidates(radar_run_id, symbol) ON DELETE CASCADE
);

-- V12.1 execution-aware performance.  The legacy signal_performance table is
-- intentionally retained as a close-to-close baseline.  This table records
-- whether the published low-catch / confirmation plan would actually fill.
CREATE TABLE IF NOT EXISTS signal_execution_performance (
    radar_run_id BIGINT NOT NULL,
    symbol VARCHAR(16) NOT NULL,
    strategy VARCHAR(32) NOT NULL,
    signal_date DATE NOT NULL,
    execution_status VARCHAR(24) NOT NULL,
    status_reason TEXT,
    aggressive_fill_date DATE,
    aggressive_fill_price NUMERIC(14,4),
    aggressive_fill_percent NUMERIC(8,4),
    confirmation_fill_date DATE,
    confirmation_fill_price NUMERIC(14,4),
    confirmation_fill_percent NUMERIC(8,4),
    entry_date DATE,
    weighted_entry_price NUMERIC(14,4),
    filled_position_percent NUMERIC(8,4),
    exit_date DATE,
    exit_price NUMERIC(14,4),
    exit_reason VARCHAR(32),
    return_d1 NUMERIC(10,4),
    return_d3 NUMERIC(10,4),
    return_d5 NUMERIC(10,4),
    return_d10 NUMERIC(10,4),
    return_d20 NUMERIC(10,4),
    max_favorable_percent NUMERIC(10,4),
    max_adverse_percent NUMERIC(10,4),
    evaluated_through DATE,
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (radar_run_id, symbol),
    FOREIGN KEY (radar_run_id, symbol)
        REFERENCES radar_candidates(radar_run_id, symbol) ON DELETE CASCADE
);
ALTER TABLE signal_execution_performance
    ADD COLUMN IF NOT EXISTS label_version VARCHAR(32),
    ADD COLUMN IF NOT EXISTS accuracy_engine VARCHAR(64),
    ADD COLUMN IF NOT EXISTS action_code VARCHAR(40),
    ADD COLUMN IF NOT EXISTS market_regime VARCHAR(24),
    ADD COLUMN IF NOT EXISTS industry VARCHAR(80),
    ADD COLUMN IF NOT EXISTS factor_confidence NUMERIC(10,4);

CREATE TABLE IF NOT EXISTS database_jobs (
    id BIGSERIAL PRIMARY KEY,
    job_type VARCHAR(50) NOT NULL,
    trade_date DATE,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    processed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_daily_bars_date ON daily_bars(trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_bars_symbol_date ON daily_bars(symbol, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_indicators_date ON daily_indicators(trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_indicators_screen
    ON daily_indicators(trade_date DESC, technical_score DESC, volume_ratio DESC);
CREATE INDEX IF NOT EXISTS idx_radar_runs_date_strategy
    ON radar_runs(run_date DESC, strategy);
CREATE INDEX IF NOT EXISTS idx_radar_candidates_score
    ON radar_candidates(radar_run_id, total_score DESC);
CREATE INDEX IF NOT EXISTS idx_signal_execution_strategy
    ON signal_execution_performance(strategy, execution_status, signal_date);
CREATE INDEX IF NOT EXISTS idx_database_jobs_status
    ON database_jobs(status, created_at DESC);

-- V12.3 compact seven-factor storage. Raw intraday ticks are intentionally
-- not retained so a 1 GB Render database remains practical.
CREATE TABLE IF NOT EXISTS monthly_revenue (
    symbol VARCHAR(16) NOT NULL,
    revenue_month DATE NOT NULL,
    revenue NUMERIC(22,2),
    monthly_change_percent NUMERIC(22,4),
    yearly_change_percent NUMERIC(22,4),
    yearly_acceleration_percent NUMERIC(22,4),
    source VARCHAR(80),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(symbol, revenue_month)
);
CREATE TABLE IF NOT EXISTS security_theme_tags (
    symbol VARCHAR(16) NOT NULL,
    theme VARCHAR(80) NOT NULL,
    source VARCHAR(40) NOT NULL DEFAULT 'manual',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(symbol, theme)
);
CREATE TABLE IF NOT EXISTS intraday_daily_features (
    symbol VARCHAR(16) NOT NULL,
    trade_date DATE NOT NULL,
    last_price NUMERIC(14,4),
    day_change_percent NUMERIC(10,4),
    close_position NUMERIC(10,4),
    volume_ratio NUMERIC(10,4),
    bid_ask_imbalance NUMERIC(10,4),
    score NUMERIC(10,4),
    source VARCHAR(80),
    snapshot JSONB NOT NULL DEFAULT '{}'::JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(symbol, trade_date)
);
CREATE TABLE IF NOT EXISTS daily_factor_snapshots (
    symbol VARCHAR(16) NOT NULL,
    trade_date DATE NOT NULL,
    chip_score NUMERIC(10,4),
    fundamental_score NUMERIC(10,4),
    theme_score NUMERIC(10,4),
    sector_score NUMERIC(10,4),
    intraday_score NUMERIC(10,4),
    data_confidence NUMERIC(10,4) NOT NULL DEFAULT 0,
    missing_factors JSONB NOT NULL DEFAULT '[]'::JSONB,
    features JSONB NOT NULL DEFAULT '{}'::JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_monthly_revenue_month
    ON monthly_revenue(revenue_month DESC, symbol);
CREATE INDEX IF NOT EXISTS idx_factor_snapshot_date
    ON daily_factor_snapshots(trade_date DESC, data_confidence DESC);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema=current_schema()
          AND table_name='monthly_revenue'
          AND column_name IN (
              'monthly_change_percent',
              'yearly_change_percent',
              'yearly_acceleration_percent'
          )
          AND COALESCE(numeric_precision, 0) < 22
    ) THEN
        ALTER TABLE monthly_revenue
            ALTER COLUMN monthly_change_percent TYPE NUMERIC(22,4),
            ALTER COLUMN yearly_change_percent TYPE NUMERIC(22,4),
            ALTER COLUMN yearly_acceleration_percent TYPE NUMERIC(22,4);
    END IF;
END $$;

INSERT INTO schema_versions(version, description)
VALUES (1, 'V10 complete PostgreSQL schema')
ON CONFLICT (version) DO NOTHING;

INSERT INTO schema_versions(version, description)
VALUES (123, 'V12.3 seven-factor radar and compact feature store')
ON CONFLICT (version) DO NOTHING;

INSERT INTO schema_versions(version, description)
VALUES (1231, 'V12.3.1 revenue, theme, intraday and final-threshold fixes')
ON CONFLICT (version) DO NOTHING;

INSERT INTO schema_versions(version, description)
VALUES (1232, 'V12.3.1 monthly revenue extreme growth precision hotfix')
ON CONFLICT (version) DO NOTHING;

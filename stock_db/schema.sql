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
CREATE INDEX IF NOT EXISTS idx_database_jobs_status
    ON database_jobs(status, created_at DESC);

INSERT INTO schema_versions(version, description)
VALUES (1, 'V10 complete PostgreSQL schema')
ON CONFLICT (version) DO NOTHING;

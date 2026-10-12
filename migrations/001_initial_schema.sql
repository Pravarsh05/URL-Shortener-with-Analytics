-- Initial Schema Migration for URL Shortener with Analytics

-- 1. Links table
CREATE TABLE IF NOT EXISTS links (
    id           BIGSERIAL PRIMARY KEY,
    code         VARCHAR(16) UNIQUE NOT NULL,   -- Short code or custom alias
    long_url     TEXT NOT NULL,
    is_custom    BOOLEAN DEFAULT FALSE,
    user_id      BIGINT,                        -- Nullable for anonymous links
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ,                   -- NULL = never expires
    is_active    BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_links_user ON links(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_links_expiry ON links(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_links_active ON links(is_active) WHERE is_active = TRUE;

-- 2. Raw Clicks table (Partitioned by Range on timestamp 'ts')
CREATE TABLE IF NOT EXISTS clicks (
    id         BIGSERIAL,
    link_id    BIGINT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    ip_hash    CHAR(64),
    country    VARCHAR(2) DEFAULT 'XX',
    device     VARCHAR(16) DEFAULT 'desktop',
    browser    VARCHAR(32) DEFAULT 'Other',
    referrer   TEXT
) PARTITION BY RANGE (ts);

CREATE INDEX IF NOT EXISTS idx_clicks_link_ts ON clicks (link_id, ts DESC);

-- Default partition so raw event insertion never fails when month rolls over
CREATE TABLE IF NOT EXISTS clicks_default PARTITION OF clicks DEFAULT;

-- Concrete monthly partitions for current and upcoming months
CREATE TABLE IF NOT EXISTS clicks_2026_09 PARTITION OF clicks
    FOR VALUES FROM ('2026-09-01 00:00:00+00') TO ('2026-10-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS clicks_2026_10 PARTITION OF clicks
    FOR VALUES FROM ('2026-10-01 00:00:00+00') TO ('2026-11-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS clicks_2026_11 PARTITION OF clicks
    FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00');

-- 3. Click Daily Rollups for fast dashboard queries
CREATE TABLE IF NOT EXISTS click_daily (
    link_id          BIGINT NOT NULL,
    day              DATE NOT NULL,
    clicks           INT NOT NULL DEFAULT 0,
    unique_visitors  INT NOT NULL DEFAULT 0,
    PRIMARY KEY (link_id, day)
);

CREATE INDEX IF NOT EXISTS idx_click_daily_link_day ON click_daily(link_id, day DESC);

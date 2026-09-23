-- TWAP service schema. Timestamps are microsecond epoch (BIGINT); prices are
-- integers; money/averages are never stored as floating point.

CREATE TABLE IF NOT EXISTS sources (
    name         TEXT PRIMARY KEY,
    priority     INTEGER NOT NULL DEFAULT 0,
    -- Secret key (base64) used for HMAC-SHA256 request authentication.
    secret_key   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS samples (
    symbol       TEXT NOT NULL,
    ts_us        BIGINT NOT NULL,
    source       TEXT NOT NULL REFERENCES sources(name),
    price        BIGINT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, ts_us, source)
);
CREATE INDEX IF NOT EXISTS samples_symbol_ts_idx
    ON samples (symbol, ts_us);

-- Replay protection for signed ingest requests.
CREATE TABLE IF NOT EXISTS used_nonces (
    source       TEXT NOT NULL,
    nonce        TEXT NOT NULL,
    ts_us        BIGINT NOT NULL,
    seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, nonce)
);
CREATE INDEX IF NOT EXISTS used_nonces_ts_idx ON used_nonces (ts_us);

-- Materialized window versions. Every recompute that changes the canonical
-- content inserts a new row; reads return the highest version. Recompute at a
-- completed window with identical content inserts nothing.
CREATE TABLE IF NOT EXISTS window_versions (
    symbol         TEXT NOT NULL,
    window_start_us BIGINT NOT NULL,
    window_sec     BIGINT NOT NULL,
    version        INTEGER NOT NULL,
    integral       NUMERIC NOT NULL,          -- price*usec, exact integer
    covered_usec   BIGINT NOT NULL,
    window_usec    BIGINT NOT NULL,
    twap_exact     TEXT NOT NULL,             -- "num/den", exact rational
    twap6          TEXT NOT NULL,             -- 6-digit fixed-point TWAP
    coverage6      TEXT NOT NULL,
    stale          BOOLEAN NOT NULL,
    last_sample_us BIGINT,
    sources        TEXT[] NOT NULL DEFAULT '{}',
    conflicts      TEXT[] NOT NULL DEFAULT '{}',
    content_hash   TEXT NOT NULL,
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, window_start_us, version)
);
CREATE INDEX IF NOT EXISTS window_versions_latest_idx
    ON window_versions (symbol, window_start_us, version DESC);

-- Applied-migration bookkeeping.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations(version) VALUES (1)
ON CONFLICT DO NOTHING;

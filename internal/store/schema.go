package store

// schemaSQL is applied idempotently on startup.
const schemaSQL = `
CREATE TABLE IF NOT EXISTS threshold_versions (
    version      BIGSERIAL PRIMARY KEY,
    spec         JSONB NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS releases (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    version             TEXT NOT NULL,
    state               TEXT NOT NULL,
    stage               TEXT NOT NULL,
    stage_weight        DOUBLE PRECISION NOT NULL,
    generation          BIGINT NOT NULL DEFAULT 0,
    observation_ms      BIGINT NOT NULL,
    min_samples         BIGINT NOT NULL,
    threshold_version   INTEGER NOT NULL,
    threshold_snapshot  JSONB NOT NULL,
    metric_url          TEXT NOT NULL,
    scenario            TEXT NOT NULL,
    stage_entered_at    TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS release_events (
    seq          BIGSERIAL PRIMARY KEY,
    release_id   TEXT NOT NULL REFERENCES releases(id),
    generation   BIGINT NOT NULL,
    type         TEXT NOT NULL,
    command      TEXT,
    from_stage   TEXT,
    to_stage     TEXT,
    verdict      JSONB,
    detail       TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS release_events_release_idx ON release_events(release_id, seq);

-- Harden older databases: detail must never be NULL (default ''). Backfill
-- existing rows first, then enforce the constraint. This is a no-op on fresh
-- databases created with the NOT NULL column above.
UPDATE release_events SET detail = '' WHERE detail IS NULL;
ALTER TABLE release_events ALTER COLUMN detail SET DEFAULT '';
ALTER TABLE release_events ALTER COLUMN detail SET NOT NULL;

CREATE TABLE IF NOT EXISTS observations (
    id             BIGSERIAL PRIMARY KEY,
    release_id     TEXT NOT NULL REFERENCES releases(id),
    stage          TEXT NOT NULL,
    generation     BIGINT NOT NULL,
    window_start   TIMESTAMPTZ NOT NULL,
    window_end     TIMESTAMPTZ NOT NULL,
    superseded     BOOLEAN NOT NULL DEFAULT false,
    verdict        JSONB NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS observations_release_idx ON observations(release_id, id);

CREATE TABLE IF NOT EXISTS used_nonces (
    digest      TEXT PRIMARY KEY,
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS used_nonces_expiry_idx ON used_nonces(expires_at);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key_digest        TEXT PRIMARY KEY,
    response_status   INTEGER NOT NULL,
    response_body     TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
`

-- 0001_init.sql — append-only schema for the revocable credential index.
-- Synthetic test data only; private keys are stored solely to make the
-- local-signing demo work.

-- Single global sequence: every state-changing append consumes one value,
-- and that value is the row's snapshot number.
CREATE SEQUENCE IF NOT EXISTS vc_snapshot_seq AS BIGINT START WITH 1;

CREATE TABLE IF NOT EXISTS issuers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    snapshot    BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS issuer_keys (
    id           TEXT PRIMARY KEY,
    issuer_id    TEXT NOT NULL REFERENCES issuers(id),
    seq          INTEGER NOT NULL,
    public_key   TEXT NOT NULL,
    private_key  TEXT NOT NULL,
    algorithm    TEXT NOT NULL DEFAULT 'Ed25519',
    valid_from   TIMESTAMPTZ NOT NULL,
    retired_at   TIMESTAMPTZ,
    snapshot     BIGINT NOT NULL,
    UNIQUE (issuer_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_issuer_keys_open
    ON issuer_keys (issuer_id) WHERE retired_at IS NULL;

CREATE TABLE IF NOT EXISTS credentials (
    id            TEXT PRIMARY KEY,
    issuer_id     TEXT NOT NULL REFERENCES issuers(id),
    subject       TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    not_before    TIMESTAMPTZ NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    content_hash  TEXT NOT NULL,
    content_json  BYTEA NOT NULL,
    payload_json  BYTEA NOT NULL,
    signature     BYTEA NOT NULL,
    key_id        TEXT NOT NULL REFERENCES issuer_keys(id),
    issued_at     TIMESTAMPTZ NOT NULL,
    snapshot      BIGINT NOT NULL,
    CONSTRAINT credentials_time_window CHECK (expires_at > not_before)
);
CREATE INDEX IF NOT EXISTS idx_credentials_issuer ON credentials (issuer_id);

CREATE TABLE IF NOT EXISTS revocations (
    id            TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL REFERENCES credentials(id),
    reason        TEXT NOT NULL,
    effective_at  TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL,
    snapshot      BIGINT NOT NULL
);
-- At most one revocation event per credential: the index is the database
-- guarantee behind the "exactly one winner under concurrent revoke" test.
CREATE UNIQUE INDEX IF NOT EXISTS idx_revocations_one_per_credential
    ON revocations (credential_id);
CREATE INDEX IF NOT EXISTS idx_revocations_effective
    ON revocations (effective_at);

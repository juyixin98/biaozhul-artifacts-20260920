package store

// schema is applied idempotently per schema. Tests create isolated
// schemas over the same database, so enum existence is checked against the
// *current* schema (current_schema()) rather than relying on the
// duplicate_object exception alone, which fires for same-named types in
// other schemas too.
const schema = `
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type t
                   JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'channel_status' AND n.nspname = current_schema()) THEN
        CREATE TYPE channel_status AS ENUM ('open', 'frozen');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type t
                   JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'message_status' AND n.nspname = current_schema()) THEN
        CREATE TYPE message_status AS ENUM ('candidate', 'confirmed', 'executed', 'cancelled');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type t
                   JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'header_status' AND n.nspname = current_schema()) THEN
        CREATE TYPE header_status AS ENUM ('active', 'revoked');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS chains (
    id              TEXT PRIMARY KEY,
    validator_pub   BYTEA NOT NULL,
    confirmations   BIGINT NOT NULL CHECK (confirmations >= 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS headers (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chain_id        TEXT NOT NULL REFERENCES chains(id),
    height          BIGINT NOT NULL CHECK (height >= 1),
    block_hash      BYTEA NOT NULL,
    parent_hash     BYTEA NOT NULL,
    msg_root        BYTEA NOT NULL,
    block_time      BIGINT NOT NULL,
    status          header_status NOT NULL DEFAULT 'active',
    canonical       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chain_id, block_hash)
);

-- At most one canonical active header per chain/height: a competing header
-- for the same height is a fork and must be handled explicitly (tip
-- revocation + replacement, or, post-finality, a security alert).
CREATE UNIQUE INDEX IF NOT EXISTS headers_canonical_tip
    ON headers (chain_id, height) WHERE canonical AND status = 'active';

CREATE TABLE IF NOT EXISTS channels (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chain_id        TEXT NOT NULL REFERENCES chains(id),
    channel_id      TEXT NOT NULL,
    status          channel_status NOT NULL DEFAULT 'open',
    next_nonce      BIGINT NOT NULL DEFAULT 1 CHECK (next_nonce >= 1),
    frozen_reason   TEXT,
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chain_id, channel_id)
);

CREATE TABLE IF NOT EXISTS channel_senders (
    chain_id        TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    sender_pub      BYTEA NOT NULL,
    PRIMARY KEY (chain_id, channel_id, sender_pub),
    FOREIGN KEY (chain_id, channel_id) REFERENCES channels(chain_id, channel_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chain_id        TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    nonce           BIGINT NOT NULL CHECK (nonce >= 1),
    payload_hash    BYTEA NOT NULL,
    block_hash      BYTEA NOT NULL,
    sender_pub      BYTEA NOT NULL,
    sender_sig      BYTEA NOT NULL,
    status          message_status NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (chain_id, channel_id) REFERENCES channels(chain_id, channel_id),
    UNIQUE (chain_id, block_hash, channel_id, nonce)
);

-- At most one *live* (non-cancelled) message per (chain, channel, nonce).
-- A pre-finality reorg cancels the old row, after which the replacement can
-- be inserted.
CREATE UNIQUE INDEX IF NOT EXISTS messages_one_live_per_key
    ON messages (chain_id, channel_id, nonce)
    WHERE status <> 'cancelled';

CREATE TABLE IF NOT EXISTS deliveries (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chain_id        TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    nonce           BIGINT NOT NULL,
    block_hash      BYTEA NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 1,
    delivered_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chain_id, channel_id, nonce)
);

CREATE TABLE IF NOT EXISTS revoked_blocks (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chain_id        TEXT NOT NULL,
    height          BIGINT NOT NULL,
    block_hash      BYTEA NOT NULL,
    replaced_by     BYTEA,
    evidence_sig    BYTEA NOT NULL,
    revoked_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alerts (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    severity        TEXT NOT NULL CHECK (severity IN ('critical','warning','info')),
    kind            TEXT NOT NULL,
    chain_id        TEXT NOT NULL,
    channel_id      TEXT NOT NULL DEFAULT '',
    message         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations(version) VALUES (1)
ON CONFLICT DO NOTHING;
`

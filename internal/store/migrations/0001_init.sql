-- Cross-chain message inbox schema.
--
-- Message identity is (source_chain, channel, sequence). A body digest is
-- carried alongside and compared on duplicate keys: equal digest means a
-- harmless re-delivery; different digest on the same key is an equivocation
-- (conflicting evidence) that freezes the channel.

CREATE TABLE IF NOT EXISTS chains (
    name        TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS channels (
    source_chain    TEXT NOT NULL REFERENCES chains(name),
    channel_id      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',       -- active | frozen
    frozen_reason   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    frozen_at       TIMESTAMPTZ,
    PRIMARY KEY (source_chain, channel_id)
);

CREATE TABLE IF NOT EXISTS blocks (
    source_chain    TEXT NOT NULL REFERENCES chains(name),
    height          BIGINT NOT NULL CHECK (height > 0),
    hash            TEXT NOT NULL,
    parent_hash     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'proposed',     -- proposed | final | revoked
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalized_at    TIMESTAMPTZ,
    PRIMARY KEY (source_chain, height, hash),
    UNIQUE (source_chain, hash)
);

-- At most one *live* (proposed or final) block at any height per chain.
-- Revoked reorged-out blocks sit beside the winner, so the index is partial.
CREATE UNIQUE INDEX IF NOT EXISTS ux_blocks_live_height
    ON blocks (source_chain, height)
    WHERE status <> 'revoked';

CREATE TABLE IF NOT EXISTS messages (
    source_chain    TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    sequence        BIGINT NOT NULL CHECK (sequence >= 0),
    block_hash      TEXT NOT NULL,
    body_digest     TEXT NOT NULL,
    body            JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    -- pending | executed | cancelled | execute_failed
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    executed_at     TIMESTAMPTZ,
    failure_reason  TEXT,
    PRIMARY KEY (source_chain, channel_id, sequence),
    FOREIGN KEY (source_chain, channel_id)
        REFERENCES channels (source_chain, channel_id),
    FOREIGN KEY (source_chain, block_hash)
        REFERENCES blocks (source_chain, hash)
);

CREATE INDEX IF NOT EXISTS ix_messages_ready
    ON messages (source_chain, channel_id, sequence)
    WHERE status = 'pending';

-- Conflicting evidence: a second, differently-digested payload observed for
-- a message key. Nothing is silently overwritten; every conflicting copy is
-- preserved here and surfaced through /alerts.
CREATE TABLE IF NOT EXISTS message_evidence (
    id              BIGSERIAL PRIMARY KEY,
    source_chain    TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    sequence        BIGINT NOT NULL,
    block_hash      TEXT NOT NULL,
    body_digest     TEXT NOT NULL,
    body            JSONB NOT NULL,
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    existing_status TEXT NOT NULL,
    UNIQUE (source_chain, channel_id, sequence, body_digest)
);

CREATE TABLE IF NOT EXISTS alerts (
    id              BIGSERIAL PRIMARY KEY,
    source_chain    TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    -- equivocation_executed | equivocation_pending | execution_failed
    sequence        BIGINT,
    body_digest     TEXT,
    detail          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_alerts_channel
    ON alerts (source_chain, channel_id, created_at DESC);

-- Application state mutated by executed messages. Real computation lands
-- here: transfer messages credit/debit balances in the SAME database
-- transaction that flips the message to 'executed', so a crash at any point
-- either commits both or neither (exactly-once execution).
CREATE TABLE IF NOT EXISTS accounts (
    address         TEXT PRIMARY KEY,
    balance         BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS executions (
    source_chain    TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    sequence        BIGINT NOT NULL,
    body_digest     TEXT NOT NULL,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_chain, channel_id, sequence)
);

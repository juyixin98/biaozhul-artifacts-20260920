-- Fork ledger indexer schema.
--
-- All blocks are kept in blocks (content-addressed by hash). Canonical chain
-- membership and the spendable ledger live in separate tables that a single
-- reorg transaction rewrites together with the cursor, so readers never see a
-- half-reorganized state.

CREATE TABLE IF NOT EXISTS blocks (
    hash              TEXT PRIMARY KEY,
    parent_hash       TEXT NOT NULL,
    height            BIGINT NOT NULL,
    transactions      JSONB NOT NULL,       -- canonical [{from,to,amount}]
    preimage          BYTEA NOT NULL,       -- canonical JSON bytes hashed
    received_seq      BIGINT NOT NULL,      -- global delivery order (cursor)
    status            TEXT NOT NULL,        -- staged | connected | invalid
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT blocks_status_check CHECK (status IN ('staged', 'connected', 'invalid'))
);
CREATE INDEX IF NOT EXISTS idx_blocks_parent     ON blocks (parent_hash);
CREATE INDEX IF NOT EXISTS idx_blocks_height     ON blocks (height);
CREATE INDEX IF NOT EXISTS idx_blocks_status     ON blocks (status);
-- "Who has already referenced this hash as parent" (delivered children).
CREATE INDEX IF NOT EXISTS idx_blocks_pk_parent  ON blocks (hash, parent_hash);

-- The canonical chain, exactly one row per canonical block.
CREATE TABLE IF NOT EXISTS canonical_blocks (
    height      BIGINT PRIMARY KEY,
    hash        TEXT NOT NULL UNIQUE REFERENCES blocks(hash)
);
CREATE INDEX IF NOT EXISTS idx_canonical_hash ON canonical_blocks (hash);

-- Spendable balances on the canonical chain. Amounts are stored as canonical
-- decimal strings to preserve arbitrary integer precision (big.Int).
CREATE TABLE IF NOT EXISTS balances (
    address TEXT PRIMARY KEY,
    amount  TEXT NOT NULL
);

-- Canonical transactions, the materialized transfer index. Negative index
-- values in a reorg delete: this table is rewritten per reorg transaction.
CREATE TABLE IF NOT EXISTS canonical_transactions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY,
    block_hash  TEXT NOT NULL,
    height      BIGINT NOT NULL,
    tx_index    INTEGER NOT NULL,   -- index within the block
    from_addr   TEXT NOT NULL,      -- '' = mint
    to_addr     TEXT NOT NULL,      -- '' = burn
    amount      TEXT NOT NULL,
    PRIMARY KEY (block_hash, tx_index)
);
CREATE INDEX IF NOT EXISTS idx_ctx_from   ON canonical_transactions (from_addr, height, tx_index);
CREATE INDEX IF NOT EXISTS idx_ctx_to     ON canonical_transactions (to_addr, height, tx_index);
CREATE INDEX IF NOT EXISTS idx_ctx_height ON canonical_transactions (height);

-- Single-row ledger state: canonical head and the durable ingestion cursor.
CREATE TABLE IF NOT EXISTS chain_state (
    id             INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    head_hash      TEXT,
    ingest_seq     BIGINT NOT NULL DEFAULT 0,  -- number of deliveries durably ingested
    stream_offset  BIGINT NOT NULL DEFAULT 0   -- bytes consumed from the block stream
);
INSERT INTO chain_state (id) VALUES (1) ON CONFLICT DO NOTHING;

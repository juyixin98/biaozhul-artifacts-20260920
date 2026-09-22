-- DAMS schema: per-organization append-only audit chain.
-- entry_hash = sha256(seq || "\n" || prev_hash || "\n" || sha256(canonical JSON of payload))
-- The first entry of a chain uses prev_hash = ZERO_HASH (64 zero hex chars).
CREATE TABLE audit_entries (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id              BIGINT NOT NULL REFERENCES organizations(id),
    seq                 BIGINT NOT NULL,
    entry_type          TEXT NOT NULL,           -- rule.create | rule.update | alert.update | ...
    actor_api_key_id    BIGINT REFERENCES api_keys(id),
    actor_label         TEXT NOT NULL DEFAULT '',
    payload             JSONB NOT NULL,
    prev_hash           TEXT NOT NULL,
    entry_hash          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, seq),
    UNIQUE (org_id, entry_hash)
);
CREATE INDEX idx_audit_org_time ON audit_entries(org_id, seq);

-- Per-org chain head. Appenders take pg_advisory_xact_lock(chain_id) inside the
-- same transaction that inserts the entry, so concurrent appends serialize and
-- the chain can never fork.
CREATE TABLE audit_chain_state (
    org_id              BIGINT PRIMARY KEY REFERENCES organizations(id),
    head_seq            BIGINT NOT NULL DEFAULT 0,
    head_hash           TEXT NOT NULL DEFAULT repeat('0', 64),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

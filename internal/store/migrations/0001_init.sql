-- 0001_init.sql: local domain lifecycle engine schema.
--
-- Money is everywhere stored as integer cents (BIGINT). Timestamps are
-- timestamptz. All lifecycle deadlines are normal columns advanced by Go code,
-- which keeps state transitions explicit and restart-safe.

CREATE TABLE resellers (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    -- Sum of available + held is the reseller's total credited balance.
    balance_cents   BIGINT NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),
    held_cents      BIGINT NOT NULL DEFAULT 0 CHECK (held_cents >= 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE customers (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reseller_id  BIGINT NOT NULL REFERENCES resellers(id),
    name         TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (reseller_id, name)
);
CREATE INDEX idx_customers_reseller ON customers(reseller_id);

-- Authentication principals. API tokens are stored only as SHA-256 hex
-- digests; the plaintext is shown once at creation time.
CREATE TABLE principals (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    role            TEXT NOT NULL CHECK (role IN ('admin','reseller','customer')),
    reseller_id     BIGINT REFERENCES resellers(id),
    customer_id     BIGINT REFERENCES customers(id),
    username        TEXT NOT NULL UNIQUE,
    token_hash      TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (role = 'admin'    AND reseller_id IS NULL AND customer_id IS NULL) OR
        (role = 'reseller' AND reseller_id IS NOT NULL AND customer_id IS NULL) OR
        (role = 'customer' AND customer_id IS NOT NULL)
    )
);

CREATE TABLE price_rules (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tld             TEXT NOT NULL,
    register_cents  BIGINT NOT NULL CHECK (register_cents >= 0),
    renew_cents     BIGINT NOT NULL CHECK (renew_cents >= 0),
    -- Flat redemption fee charged when restoring an expired/redeemable name.
    -- A restore always also extends the name by one year at renew_cents.
    restore_cents   BIGINT NOT NULL DEFAULT 0 CHECK (restore_cents >= 0),
    transfer_cents  BIGINT NOT NULL CHECK (transfer_cents >= 0),
    -- Only the latest row per TLD is current; superseded rows are kept for
    -- audit. Price changes never touch amounts already snapshotted onto
    -- billing_transactions.
    superseded      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX idx_price_rules_current ON price_rules(tld) WHERE NOT superseded;

CREATE TABLE domains (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            TEXT NOT NULL,
    tld             TEXT NOT NULL,
    -- Canonical FQDN uniqueness. After pending_delete completes the row is
    -- deleted, so the name can be registered again later.
    canonical_name  TEXT NOT NULL UNIQUE,
    customer_id     BIGINT NOT NULL REFERENCES customers(id),
    reseller_id     BIGINT NOT NULL REFERENCES resellers(id),
    -- Statuses: registered, expired, redeemable, pending_delete, transferring.
    -- "available" is the absence of a row.
    status          TEXT NOT NULL CHECK (status IN
                        ('registered','expired','redeemable','pending_delete','transferring')),
    expires_at          TIMESTAMPTZ NOT NULL,
    expired_at          TIMESTAMPTZ,  -- deadline registered -> expired
    redeemable_at       TIMESTAMPTZ,  -- deadline expired -> redeemable
    pending_delete_at   TIMESTAMPTZ,  -- deadline redeemable -> pending_delete
    purge_at            TIMESTAMPTZ,  -- deadline pending_delete -> released
    -- Auth code ciphertext (AES-256-GCM, base64). Never logged.
    auth_cipher     TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_domains_customer ON domains(customer_id);
CREATE INDEX idx_domains_reseller ON domains(reseller_id);
CREATE INDEX idx_domains_status_expiry ON domains(status, expired_at);
CREATE INDEX idx_domains_purge ON domains(purge_at);

CREATE TABLE transfers (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    domain_id           BIGINT NOT NULL, -- no FK: rows outlive domain purge
    domain_name         TEXT NOT NULL,
    from_reseller_id    BIGINT NOT NULL REFERENCES resellers(id),
    from_customer_id    BIGINT NOT NULL REFERENCES customers(id),
    to_reseller_id      BIGINT NOT NULL REFERENCES resellers(id),
    to_customer_id      BIGINT NOT NULL REFERENCES customers(id),
    -- pending_approval -> seller_approved -> completed
    --                   -> rejected | canceled | failed(timeout)
    state               TEXT NOT NULL CHECK (state IN
                            ('pending_approval','seller_approved','completed',
                             'rejected','canceled','failed')),
    -- Fee frozen from the price table at request time (cents). A later price
    -- adjustment never changes this amount.
    transfer_cents      BIGINT NOT NULL CHECK (transfer_cents >= 0),
    requested_at        TIMESTAMPTZ NOT NULL,
    approved_at         TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    -- Losing reseller must approve/reject before this instant; otherwise the
    -- worker fails the transfer and releases the frozen fee.
    approval_deadline   TIMESTAMPTZ NOT NULL,
    -- Completion becomes eligible this long after seller approval (the
    -- simulated 5-day registry wait).
    approve_until       TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ NOT NULL, -- hard end of the live transfer
    created_tx_id       BIGINT,
    captured_tx_id      BIGINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_transfers_domain ON transfers(domain_id);
CREATE INDEX idx_transfers_state ON transfers(state, expires_at);
CREATE INDEX idx_transfers_approve ON transfers(state, approve_until);
-- Belt-and-braces: at most one live transfer per domain.
CREATE UNIQUE INDEX idx_transfers_one_live ON transfers(domain_id)
    WHERE state IN ('pending_approval','seller_approved');

-- Append-only ledger postings. A billing_transaction describes one logical
-- money movement; its double-checked entries in ledger_entries sum to zero.
CREATE TABLE billing_transactions (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reseller_id     BIGINT NOT NULL REFERENCES resellers(id),
    kind            TEXT NOT NULL CHECK (kind IN
                        ('topup','register','renew','restore','transfer_freeze',
                         'transfer_capture','transfer_release')),
    amount_cents    BIGINT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'completed'
                        CHECK (status IN ('completed','failed')),
    domain_id       BIGINT,
    transfer_id     BIGINT,
    years           INT,
    idempotency_key TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Idempotency keys are unique per (reseller, kind): retries return the
-- original transaction instead of charging again.
CREATE UNIQUE INDEX idx_billing_idem ON billing_transactions
    (reseller_id, kind, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_billing_reseller ON billing_transactions(reseller_id, created_at);

CREATE TABLE ledger_entries (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tx_id       BIGINT NOT NULL REFERENCES billing_transactions(id),
    reseller_id BIGINT NOT NULL REFERENCES resellers(id),
    -- available_delta / held_delta move money between the two pools or in/out
    -- of the reseller account; after_balance is an audit snapshot.
    available_delta BIGINT NOT NULL,
    held_delta      BIGINT NOT NULL,
    after_available BIGINT NOT NULL,
    after_held      BIGINT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_ledger_tx ON ledger_entries(tx_id);
CREATE INDEX idx_ledger_reseller ON ledger_entries(reseller_id, id);

-- Append-only lifecycle audit log. Never updated or deleted.
CREATE TABLE domain_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    domain_id   BIGINT NOT NULL,
    domain_name TEXT NOT NULL,
    event_type  TEXT NOT NULL, -- registered, renewed, restored, ...
    from_status TEXT,
    to_status   TEXT,
    amount_cents BIGINT,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_events_domain ON domain_events(domain_id, id);
CREATE INDEX idx_events_type ON domain_events(event_type, id);

-- Default prices for the demo TLDs (US$12.00 / etc., integer cents).
INSERT INTO price_rules (tld, register_cents, renew_cents, restore_cents, transfer_cents) VALUES
    ('com', 1200, 1200, 20000, 1200),
    ('net', 1100, 1100, 20000, 1100),
    ('org', 1000, 1000, 20000, 1000);

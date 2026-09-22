-- 0001_init.sql — schema + local development seed data.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id          uuid PRIMARY KEY,
    api_key     text UNIQUE NOT NULL,
    role        text NOT NULL CHECK (role IN ('admin', 'reseller', 'customer')),
    name        text NOT NULL,
    reseller_id uuid REFERENCES users (id),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Append-only credit ledger. Amounts are integer cents, always positive;
-- `kind` gives the direction. Balance = sum(credit) - sum(charge).
CREATE TABLE ledger_entries (
    id              bigserial PRIMARY KEY,
    reseller_id     uuid NOT NULL REFERENCES users (id),
    idempotency_key text UNIQUE NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('credit', 'charge')),
    amount_cents    bigint NOT NULL CHECK (amount_cents > 0),
    memo            text NOT NULL DEFAULT '',
    domain_name     text,
    transfer_id     uuid,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ledger_entries_reseller ON ledger_entries (reseller_id, id);

-- Frozen credits for in-flight transfers.
CREATE TABLE credit_holds (
    id              uuid PRIMARY KEY,
    reseller_id     uuid NOT NULL REFERENCES users (id),
    amount_cents    bigint NOT NULL CHECK (amount_cents > 0),
    status          text NOT NULL CHECK (status IN ('open', 'released', 'captured')),
    idempotency_key text UNIQUE NOT NULL,
    transfer_id     uuid,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX credit_holds_open ON credit_holds (reseller_id) WHERE status = 'open';

-- Price history: rows are never updated, new prices are appended with a
-- later effective_from. tld '*' is the wildcard fallback.
CREATE TABLE prices (
    id             bigserial PRIMARY KEY,
    tld            text NOT NULL,
    action         text NOT NULL CHECK (action IN ('register', 'renew', 'transfer', 'redeem')),
    amount_cents   bigint NOT NULL CHECK (amount_cents >= 0),
    effective_from timestamptz NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX prices_lookup ON prices (tld, action, effective_from DESC);

-- A name absent from this table is `available`. The UNIQUE(name) constraint
-- is what makes concurrent registration safe.
CREATE TABLE domains (
    id            uuid PRIMARY KEY,
    name          text UNIQUE NOT NULL, -- normalized: lowercased, no trailing dot, punycode
    owner_id      uuid NOT NULL REFERENCES users (id),
    status        text NOT NULL CHECK (status IN ('registered', 'transferring', 'expired', 'redemption', 'pending_delete')),
    expires_at    timestamptz NOT NULL,
    auth_code_enc bytea,                -- AES-256-GCM encrypted 16-char transfer code
    version       bigint NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX domains_status_expiry ON domains (status, expires_at);

CREATE TABLE transfers (
    id                uuid PRIMARY KEY,
    domain_id         uuid NOT NULL REFERENCES domains (id),
    from_owner_id     uuid NOT NULL REFERENCES users (id),
    to_owner_id       uuid NOT NULL REFERENCES users (id),
    state             text NOT NULL CHECK (state IN ('pending_approval', 'approved', 'completed', 'cancelled', 'failed')),
    price_cents       bigint NOT NULL, -- fixed at acceptance; later price changes do not apply
    hold_id           uuid NOT NULL REFERENCES credit_holds (id),
    idempotency_key   text UNIQUE NOT NULL,
    approval_deadline timestamptz NOT NULL,
    completes_at      timestamptz,
    cancel_reason     text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
-- At most one active transfer per domain.
CREATE UNIQUE INDEX transfers_active ON transfers (domain_id) WHERE state IN ('pending_approval', 'approved');
CREATE INDEX transfers_state_due ON transfers (state, completes_at);

CREATE TABLE idempotency_keys (
    key         text PRIMARY KEY,
    user_id     uuid NOT NULL,
    operation   text NOT NULL,
    status_code int NOT NULL,
    response    jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE domain_events (
    id          bigserial PRIMARY KEY,
    domain_id   uuid,
    domain_name text NOT NULL,
    event       text NOT NULL,
    detail      jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX domain_events_name ON domain_events (domain_name, id);

-- ---------------------------------------------------------------------------
-- Seed data for local development. API keys are intentionally simple.
-- ---------------------------------------------------------------------------

INSERT INTO users (id, api_key, role, name, reseller_id) VALUES
    ('11111111-1111-1111-1111-111111111111', 'admin-key',    'admin',    'Admin',    NULL),
    ('22222222-2222-2222-2222-222222222222', 'reseller-a-key', 'reseller', 'Reseller A', NULL),
    ('33333333-3333-3333-3333-333333333333', 'reseller-b-key', 'reseller', 'Reseller B', NULL),
    ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'alice-key',    'customer',  'Alice',    '22222222-2222-2222-2222-222222222222'),
    ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'bob-key',      'customer',  'Bob',      '22222222-2222-2222-2222-222222222222'),
    ('cccccccc-cccc-cccc-cccc-cccccccccccc', 'carol-key',    'customer',  'Carol',    '33333333-3333-3333-3333-333333333333');

-- Prices in integer cents.
INSERT INTO prices (tld, action, amount_cents, effective_from) VALUES
    ('com', 'register', 1200, '2020-01-01'),
    ('com', 'renew',    1200, '2020-01-01'),
    ('com', 'transfer', 1200, '2020-01-01'),
    ('com', 'redeem',   9000, '2020-01-01'),
    ('*',   'register', 1000, '2020-01-01'),
    ('*',   'renew',    1000, '2020-01-01'),
    ('*',   'transfer', 1000, '2020-01-01'),
    ('*',   'redeem',   8000, '2020-01-01');

-- Starting credit: 5000.00 per reseller.
INSERT INTO ledger_entries (reseller_id, idempotency_key, kind, amount_cents, memo) VALUES
    ('22222222-2222-2222-2222-222222222222', 'seed:credit:reseller-a', 'credit', 500000, 'seed credit'),
    ('33333333-3333-3333-3333-333333333333', 'seed:credit:reseller-b', 'credit', 500000, 'seed credit');

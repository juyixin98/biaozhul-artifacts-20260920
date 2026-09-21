-- 0001_init.sql: ClearSettle core schema.
-- All money amounts are integer cents (BIGINT), never NULL, never negative unless noted.

-- Merchants ----------------------------------------------------------------
CREATE TABLE merchants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    fee_bps         INTEGER NOT NULL DEFAULT 290 CHECK (fee_bps >= 0 AND fee_bps <= 10000),
    fee_fixed_cents BIGINT  NOT NULL DEFAULT 30 CHECK (fee_fixed_cents >= 0),
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- API keys, one of three roles:
--   admin    = manage merchants / all-merchant actions (reconciliation, corrections)
--   operator = create money-moving transactions, but only for the owned merchant
--   auditor  = read-only
CREATE TABLE api_keys (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash       TEXT NOT NULL UNIQUE,
    key_prefix     TEXT NOT NULL,               -- first 8 chars for masked display
    merchant_id    UUID REFERENCES merchants(id), -- NULL for platform-level admins
    role           TEXT NOT NULL CHECK (role IN ('admin','operator','auditor')),
    label          TEXT NOT NULL DEFAULT '',
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((role = 'admin') = (merchant_id IS NULL))
);

-- Chart of accounts. Internal account codes are global (single platform):
--   gateway_cash, fee_revenue, unsettled, payable
-- Each merchant additionally gets one suspense (equity-like correction) account.
CREATE TABLE accounts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id UUID REFERENCES merchants(id),  -- NULL for internal accounts
    code        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('asset','liability','equity','revenue')),
    description TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (merchant_id, code)
);

-- Double-entry ledger. Rows are append-only: UPDATE/DELETE are forbidden by a
-- trigger. Corrections must be posted as a new reversing entry pair.
-- amount_cents is signed: debit positive for asset/expense increases;
-- the application always posts balanced pairs (sum of amounts per tx = 0).
CREATE TABLE ledger_entries (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tx_type       TEXT NOT NULL CHECK (tx_type IN ('capture','refund','settlement','correction')),
    ref_type      TEXT NOT NULL CHECK (ref_type IN ('payment','refund','batch','manual')),
    ref_id        UUID NOT NULL,
    account_id    UUID NOT NULL REFERENCES accounts(id),
    amount_cents  BIGINT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_ledger_account ON ledger_entries (account_id, id);
CREATE INDEX idx_ledger_ref ON ledger_entries (ref_type, ref_id);

CREATE OR REPLACE FUNCTION ledger_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger_entries is append-only; post a reversing correction entry instead';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_ledger_no_update BEFORE UPDATE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_immutable();
CREATE TRIGGER trg_ledger_no_delete BEFORE DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_immutable();

-- Payments -----------------------------------------------------------------
-- status: authorized -> captured -> settled -> refunded (fully)
--                  \\-> voided ; captured/settled can also become refunded (partial)
CREATE TABLE transactions (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id           UUID NOT NULL REFERENCES merchants(id),
    status                TEXT NOT NULL DEFAULT 'authorized'
                          CHECK (status IN ('authorized','captured','partially_refunded',
                                            'refunded','voided','settled')),
    amount_cents          BIGINT NOT NULL CHECK (amount_cents > 0),
    captured_cents        BIGINT NOT NULL DEFAULT 0,
    fee_cents             BIGINT NOT NULL DEFAULT 0,  -- fee on the captured amount
    refunded_cents        BIGINT NOT NULL DEFAULT 0,
    refunded_fee_cents    BIGINT NOT NULL DEFAULT 0,  -- cumulative proportional fee released
    settled_cents         BIGINT NOT NULL DEFAULT 0,  -- net cash already settled out
    currency              TEXT NOT NULL DEFAULT 'USD',
    card_last4            TEXT NOT NULL,
    auth_expires_at       TIMESTAMPTZ NOT NULL,
    settled_at            TIMESTAMPTZ,
    refund_deadline       TIMESTAMPTZ,                 -- set when settled (90 days)
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    captured_at           TIMESTAMPTZ
);
CREATE INDEX idx_tx_merchant_created ON transactions (merchant_id, created_at);
CREATE INDEX idx_tx_status ON transactions (status);

CREATE TABLE refunds (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id            UUID NOT NULL REFERENCES transactions(id),
    merchant_id           UUID NOT NULL REFERENCES merchants(id),
    amount_cents          BIGINT NOT NULL CHECK (amount_cents > 0),
    fee_refund_cents      BIGINT NOT NULL DEFAULT 0, -- proportional fee released this refund
    net_cents             BIGINT NOT NULL DEFAULT 0, -- amount - fee_refund, moved out of payable
    reason                TEXT NOT NULL DEFAULT '',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_refund_payment ON refunds (payment_id);

-- Settlement ---------------------------------------------------------------
CREATE TABLE settlement_batches (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id   UUID NOT NULL REFERENCES merchants(id),
    batch_date    DATE NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done')),
    total_cents   BIGINT NOT NULL DEFAULT 0, -- gross captured cash settled by this batch
    fee_cents     BIGINT NOT NULL DEFAULT 0, -- fees withheld by this batch
    net_cents     BIGINT NOT NULL DEFAULT 0, -- cash moved payable -> gateway_cash (clawback-aware)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ,
    UNIQUE (merchant_id, batch_date)
);
CREATE INDEX idx_batch_status ON settlement_batches (status);

CREATE TABLE settlement_items (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id              UUID NOT NULL REFERENCES settlement_batches(id),
    payment_id            UUID NOT NULL UNIQUE, -- a payment is settled by at most one batch
    gross_cents           BIGINT NOT NULL,
    fee_cents             BIGINT NOT NULL,
    net_cents             BIGINT NOT NULL
);

-- Idempotency --------------------------------------------------------------
CREATE TABLE idempotency_keys (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key              TEXT NOT NULL,
    merchant_id      UUID NOT NULL,
    endpoint         TEXT NOT NULL,             -- route pattern, e.g. /v1/payments
    request_hash     TEXT NOT NULL,             -- canonical SHA-256 of request params
    resource_id      UUID,                      -- payment/refund/batch that was created
    response_status  INTEGER NOT NULL,
    response_body    JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (key, merchant_id, endpoint)
);

-- Reconciliation -----------------------------------------------------------
-- Simulated gateway statement rows: what the (fake) acquirer reports. Daily
-- reconciliation compares these against our internal ledger and payments.
CREATE TABLE gateway_statements (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    merchant_id   UUID NOT NULL REFERENCES merchants(id),
    ref_id        UUID NOT NULL,           -- payment id for captures, refund id for refunds
    kind          TEXT NOT NULL CHECK (kind IN ('capture','refund')),
    amount_cents  BIGINT NOT NULL,
    stmt_date     DATE NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (merchant_id, ref_id, kind)
);
CREATE INDEX idx_stmt_merchant_date ON gateway_statements (merchant_id, stmt_date);

CREATE TABLE reconciliation_runs (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id        UUID NOT NULL REFERENCES merchants(id),
    run_date           DATE NOT NULL,
    status             TEXT NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running','done')),
    batch_id           UUID REFERENCES settlement_batches(id),
    captured_cents     BIGINT NOT NULL DEFAULT 0,
    refunded_cents     BIGINT NOT NULL DEFAULT 0,
    fees_cents         BIGINT NOT NULL DEFAULT 0,
    settled_cents      BIGINT NOT NULL DEFAULT 0,
    gateway_cash_cents BIGINT NOT NULL DEFAULT 0, -- ledger sum of gateway account
    payable_cents      BIGINT NOT NULL DEFAULT 0, -- ledger sum of payable account
    expected_cash_cents BIGINT NOT NULL DEFAULT 0,-- captured - refunded
    diff_cents         BIGINT NOT NULL DEFAULT 0,-- expected - gateway balance
    discrepancies_count INTEGER NOT NULL DEFAULT 0,
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at       TIMESTAMPTZ,
    UNIQUE (merchant_id, run_date)
);

CREATE TABLE discrepancies (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id               UUID NOT NULL REFERENCES reconciliation_runs(id),
    kind                 TEXT NOT NULL CHECK (kind IN ('missing_payment','missing_refund',
                                                       'missing_statement','amount_mismatch',
                                                       'ledger_balance')),
    tx_id                UUID,
    expected_cents       BIGINT NOT NULL,
    actual_cents         BIGINT NOT NULL,
    diff_cents           BIGINT NOT NULL,
    detail               TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_discrepancy_run ON discrepancies (run_id);

-- Audit --------------------------------------------------------------------
CREATE TABLE audit_events (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_key_id UUID REFERENCES api_keys(id),
    actor_role   TEXT NOT NULL,
    merchant_id  UUID,
    action       TEXT NOT NULL,
    target_type  TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_merchant ON audit_events (merchant_id, created_at);

-- Payment lifecycle. Status is the explicit state machine:
--   authorized -> captured | voided
--   captured   -> partially_refunded -> refunded
--   captured/partially_refunded -> settled (settlement does not change refunds)
-- voided/refunded are terminal for *new* captures/refunds; settled payments can
-- still be refunded within 90 days of settlement (posting negative payable then).
CREATE TABLE payments (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id        UUID NOT NULL REFERENCES merchants(id),
    idempotency_key    TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN (
                           'authorized','captured','partially_refunded',
                           'refunded','voided','settled')),
    authorized_amount  BIGINT NOT NULL CHECK (authorized_amount > 0),
    captured_amount    BIGINT NOT NULL DEFAULT 0 CHECK (captured_amount >= 0),
    fee_bps            INTEGER NOT NULL,
    fee_fixed          BIGINT NOT NULL,
    fee_amount         BIGINT NOT NULL DEFAULT 0,          -- fee on captured total
    refunded_amount    BIGINT NOT NULL DEFAULT 0 CHECK (refunded_amount >= 0),
    refunded_fee       BIGINT NOT NULL DEFAULT 0,          -- fee returned with refunds
    currency           TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    external_ref       TEXT,                               -- simulated channel reference
    authorized_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    captured_at        TIMESTAMPTZ,
    voided_at          TIMESTAMPTZ,
    expires_at         TIMESTAMPTZ NOT NULL,               -- authorize + 24h
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_payments_idem UNIQUE (merchant_id, idempotency_key),
    CONSTRAINT chk_captured_le_auth CHECK (captured_amount <= authorized_amount),
    CONSTRAINT chk_refunded_le_captured CHECK (refunded_amount <= captured_amount),
    CONSTRAINT chk_refunded_fee_le_fee CHECK (refunded_fee <= fee_amount)
);

CREATE TRIGGER trg_payments_updated BEFORE UPDATE ON payments
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_payments_merchant_created ON payments(merchant_id, created_at);
CREATE INDEX idx_payments_settle ON payments(merchant_id, captured_at)
    WHERE status IN ('captured','partially_refunded');

CREATE TABLE refunds (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id      UUID NOT NULL REFERENCES payments(id),
    merchant_id     UUID NOT NULL REFERENCES merchants(id),
    idempotency_key TEXT NOT NULL,
    amount          BIGINT NOT NULL CHECK (amount > 0),
    fee_refund      BIGINT NOT NULL DEFAULT 0 CHECK (fee_refund >= 0),
    reason          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_refunds_idem UNIQUE (merchant_id, idempotency_key),
    CONSTRAINT uq_refund_per_payment_key UNIQUE (payment_id, idempotency_key)
);

CREATE INDEX idx_refunds_payment ON refunds(payment_id);
CREATE INDEX idx_refunds_day ON refunds(merchant_id, created_at);

-- Idempotency ledger: every merchant-scoped write stores request fingerprint +
-- replay response. Unique merchant+key is the concurrency race winner.
CREATE TABLE idempotent_requests (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id    UUID NOT NULL REFERENCES merchants(id),
    idem_key       TEXT NOT NULL,
    route          TEXT NOT NULL,
    request_hash   TEXT NOT NULL,           -- sha256 of normalized request body
    status_code    INTEGER NOT NULL,
    response_body  JSONB NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_idem UNIQUE (merchant_id, idem_key)
);

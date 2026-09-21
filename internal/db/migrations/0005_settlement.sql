-- Settlement batches: at most one completed batch per merchant per business day.
-- The (merchant_id, batch_date) unique constraint makes the worker resumable:
-- re-running after a crash skips merchants/dates already completed.
-- A batch settles exactly the payments captured on batch_date (UTC). Post-batch
-- refunds are NOT retro-edited into old batches: they post negative payable and
-- net into a later batch's payout.
CREATE TABLE settlement_batches (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id      UUID NOT NULL REFERENCES merchants(id),
    batch_date       DATE NOT NULL,             -- captured_at calendar day (UTC)
    status           TEXT NOT NULL DEFAULT 'processing'
                     CHECK (status IN ('processing','completed','failed')),
    gross_captured   BIGINT NOT NULL DEFAULT 0, -- sum captured_amount of the day
    total_fees       BIGINT NOT NULL DEFAULT 0, -- fees charged on those captures
    total_refunds    BIGINT NOT NULL DEFAULT 0, -- refunds already applied pre-batch
    refunded_fees    BIGINT NOT NULL DEFAULT 0,
    net_amount       BIGINT NOT NULL DEFAULT 0, -- gross - refunds - fees + refunded fees
    payment_count    INTEGER NOT NULL DEFAULT 0,
    locked_by        TEXT,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ,
    CONSTRAINT uq_settlement_merchant_day UNIQUE (merchant_id, batch_date)
);

-- A payment belongs to at most one batch. MarkPaymentSettled only matches rows
-- with settlement_batch_id IS NULL, and the row-level FOR UPDATE lock taken by
-- ListUnsettledCapturedPayments prevents two workers assigning the same payment.
ALTER TABLE payments ADD COLUMN settlement_batch_id UUID
    REFERENCES settlement_batches(id);

CREATE INDEX idx_payments_batch ON payments(settlement_batch_id)
    WHERE settlement_batch_id IS NOT NULL;

-- Simulated acquirer/channel view of the world. Written in the SAME transaction
-- as the internal posting, but treated as an external source of truth during
-- reconciliation (deliberately independent table). Tests/ops may insert a
-- correction row to emulate a channel discrepancy.
CREATE TABLE channel_events (
    id              BIGSERIAL PRIMARY KEY,
    merchant_id     UUID NOT NULL REFERENCES merchants(id),
    event_date      DATE NOT NULL,             -- event time calendar day (UTC)
    event_type      TEXT NOT NULL CHECK (event_type IN ('capture','refund')),
    payment_id      UUID NOT NULL,
    refund_id       UUID,
    gross_amount    BIGINT NOT NULL,           -- capture or refund gross, > 0
    fee_delta       BIGINT NOT NULL,           -- fee charged (>0 capture), fee returned (<0 on refund)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_channel_events_day ON channel_events(merchant_id, event_date);
CREATE UNIQUE INDEX uq_channel_capture ON channel_events(payment_id)
    WHERE event_type = 'capture';
CREATE UNIQUE INDEX uq_channel_refund ON channel_events(refund_id)
    WHERE event_type = 'refund';

-- Reconciliation runs: one completed run per merchant per day is the normal
-- case; uniqueness makes the job idempotent across restarts.
CREATE TABLE reconciliation_runs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id   UUID NOT NULL REFERENCES merchants(id),
    run_date      DATE NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running','completed','failed')),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ,
    detail_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_recon_merchant_day UNIQUE (merchant_id, run_date)
);

CREATE TABLE reconciliation_items (
    id           BIGSERIAL PRIMARY KEY,
    run_id       UUID NOT NULL REFERENCES reconciliation_runs(id),
    check_name   TEXT NOT NULL,               -- capture_totals | refund_totals | ledger_balance
    expected     BIGINT NOT NULL,             -- internal ledger / payments view
    actual       BIGINT NOT NULL,             -- channel view
    difference   BIGINT NOT NULL,             -- actual - expected
    severity     TEXT NOT NULL CHECK (severity IN ('match','discrepancy')),
    detail       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_recon_item UNIQUE (run_id, check_name)
);

CREATE INDEX idx_recon_items_run ON reconciliation_items(run_id);

-- Advisory lock namespaces (pg_advisory_xact_lock) used by workers:
--   settlement:    hashtext('settle:<merchant_id>:<date>')
--   reconciliation: hashtext('recon:<merchant_id>:<date>')
-- Documented here so the constants in Go and SQL never drift apart.

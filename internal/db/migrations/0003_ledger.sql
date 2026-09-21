-- Append-only charted double-entry ledger.
-- Rows are never UPDATEd or DELETEd; corrections are new reversing entries.
-- Sign convention: every posting touches two (or more) accounts with equal and
-- opposite signed amounts; SUM(amount) over any ref_id is always 0.
CREATE TABLE ledger_accounts (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code       TEXT NOT NULL UNIQUE,          -- e.g. PAYABLE:<merchant>, CASH, FEE_REVENUE
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('asset','liability','revenue')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ledger_entries (
    id          BIGSERIAL PRIMARY KEY,        -- append order == id order
    account_id  UUID NOT NULL REFERENCES ledger_accounts(id),
    ref_type    TEXT NOT NULL,                -- payment | refund | settlement | correction
    ref_id      UUID NOT NULL,
    amount      BIGINT NOT NULL,              -- signed cents
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ledger_entries_account ON ledger_entries(account_id, id);
CREATE INDEX idx_ledger_entries_ref ON ledger_entries(ref_type, ref_id);
-- Each side of a posting is unique, which also makes posting idempotent within a tx.
CREATE UNIQUE INDEX uq_ledger_posting
    ON ledger_entries(ref_type, ref_id, account_id);

-- Historical entries are immutable: any attempted modification is rejected.
-- Corrections must be booked as new reversing entries (ref_type='correction').
CREATE OR REPLACE FUNCTION ledger_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger_entries are append-only; book a reversing entry instead (op=%)', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_ledger_no_update BEFORE UPDATE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation();
CREATE TRIGGER trg_ledger_no_delete BEFORE DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation();

-- Every business posting must balance to zero.
CREATE OR REPLACE FUNCTION ledger_balanced() RETURNS trigger AS $$
DECLARE
    total BIGINT;
BEGIN
    SELECT COALESCE(SUM(amount), 0) INTO total
      FROM ledger_entries WHERE ref_type = NEW.ref_type AND ref_id = NEW.ref_id;
    IF total <> 0 THEN
        RAISE EXCEPTION 'unbalanced ledger posting %/%: sum=%', NEW.ref_type, NEW.ref_id, total;
    END IF;
    RETURN NULL; -- statement-level-ish guard runs per row; balance evaluated against committed intent
END;
$$ LANGUAGE plpgsql;

-- The trigger below runs AFTER each row; at that point the *whole* posting for
-- this ref has already been inserted inside the caller transaction. Because the
-- last insert sees every sibling row, the sum must be 0 when the tx commits.
CREATE CONSTRAINT TRIGGER trg_ledger_balanced
    AFTER INSERT ON ledger_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_balanced();

-- Operational views. Balances are materialized on demand from immutable entries.
CREATE VIEW v_account_balances AS
SELECT a.id, a.code, a.kind, COALESCE(SUM(e.amount), 0) AS balance, MAX(e.id) AS last_entry_id
FROM ledger_accounts a
LEFT JOIN ledger_entries e ON e.account_id = a.id
GROUP BY a.id, a.code, a.kind;

-- Every posting group must net to zero; the reconciliation "ledger_balance"
-- check uses this (and so does the ledger self-check in tests).
CREATE VIEW v_unbalanced_postings AS
SELECT ref_type, ref_id, SUM(amount) AS net
FROM ledger_entries
GROUP BY ref_type, ref_id
HAVING SUM(amount) <> 0;

-- Per-merchant daily internal capture totals, dated by captured_at (UTC).
CREATE VIEW v_internal_daily_captures AS
SELECT merchant_id,
       (captured_at AT TIME ZONE 'UTC')::date AS day,
       SUM(captured_amount)  AS capture_gross,
       SUM(fee_amount)       AS fee_charged,
       COUNT(*)              AS capture_count
FROM payments
WHERE captured_at IS NOT NULL
GROUP BY merchant_id, (captured_at AT TIME ZONE 'UTC')::date;

CREATE VIEW v_internal_daily_refunds AS
SELECT r.merchant_id,
       (r.created_at AT TIME ZONE 'UTC')::date AS day,
       SUM(r.amount)      AS refund_gross,
       SUM(r.fee_refund)  AS fee_refunded,
       COUNT(*)           AS refund_count
FROM refunds r
GROUP BY r.merchant_id, (r.created_at AT TIME ZONE 'UTC')::date;

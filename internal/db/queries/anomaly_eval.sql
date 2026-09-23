-- Account existence dates (created_at timestamptz truncated to date).
-- An account that did not yet exist for part of the baseline window has
-- genuinely insufficient history, distinct from an existing account that
-- simply spent zero on those days.
-- name: AccountCreatedDates :many
SELECT id AS account_id, created_at::date AS created_date
FROM accounts
WHERE id = ANY($1::uuid[]);

-- name: InsertAnomalyEvaluation :exec
INSERT INTO anomaly_evaluations
    (run_id, account_id, currency, cost_date, actual_amount,
     baseline_mean, baseline_std, threshold, status)
SELECT UNNEST($1::text[])::uuid, UNNEST($2::text[])::uuid, UNNEST($3::text[])::char(3),
       UNNEST($4::text[])::date, UNNEST($5::text[])::numeric,
       NULLIF(UNNEST($6::text[]), '')::numeric,
       NULLIF(UNNEST($7::text[]), '')::numeric,
       NULLIF(UNNEST($8::text[]), '')::numeric,
       UNNEST($9::text[]);


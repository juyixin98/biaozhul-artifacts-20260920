package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/shopspring/decimal"

	"costlens/internal/csvparse"
)

// ImportConflict describes one (key, different content) clash. The whole
// batch is rejected; Locations lists every file line involved in that key
// (existing rows carry line 0 and ExistingAmount is populated).
type ImportConflict struct {
	Key             string `json:"key"`
	ResourceID      string `json:"resource_id"`
	Date            string `json:"date"`
	Currency        string `json:"currency,omitempty"`
	ExistingAmount  string `json:"existing_amount,omitempty"`
	ExistingService string `json:"existing_service,omitempty"`
	Lines           []int  `json:"lines"`
}

type ImportResult struct {
	BatchID       int64           `json:"batch_id"`
	TotalRows     int             `json:"total_rows"`
	InsertedRows  int             `json:"inserted_rows"`
	DuplicateRows int             `json:"duplicate_rows"`
	Conflict      *ImportConflict `json:"-"`
	NewAlerts     []int64         `json:"new_alert_ids,omitempty"`
}

// ConflictError wraps import-time conflicts for the HTTP layer (HTTP 409).
type ConflictError struct{ Conflicts []ImportConflict }

func (e *ConflictError) Error() string {
	return fmt.Sprintf("%d conflicting row(s)", len(e.Conflicts))
}

// Import validates that accountExt exists and actor may import into its org,
// then runs the entire batch in one transaction under the advisory lock.
// Parse errors (422) are expected before calling; here only DB-level
// conflicts (409) arise.
func (s *Service) Import(ctx context.Context, accountExt, filename string,
	actorID int64, rows []csvparse.Row) (*ImportResult, error) {

	account, err := s.getAccountByExternal(ctx, accountExt)
	if err != nil {
		return nil, err
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)

	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, advisoryLockID); err != nil {
		return nil, err
	}

	// 1. Stage all rows in a TEMP table scoped to this transaction.
	if _, err := tx.Exec(ctx, `
		CREATE TEMP TABLE stage_rows (
			line        INT NOT NULL,
			resource_id TEXT NOT NULL,
			service     TEXT NOT NULL,
			usage_date  DATE NOT NULL,
			currency    CHAR(3) NOT NULL,
			amount      NUMERIC(20,6) NOT NULL,
			content_hash TEXT NOT NULL
		) ON COMMIT DROP`); err != nil {
		return nil, err
	}

	staged := make([][]any, len(rows))
	for i, r := range rows {
		staged[i] = []any{
			r.Line, r.ResourceID, r.Service, pgDate(r.Date),
			r.Currency, r.Amount, contentHash(r),
		}
	}
	if _, err := tx.CopyFrom(ctx,
		pgx.Identifier{"stage_rows"},
		[]string{"line", "resource_id", "service", "usage_date", "currency", "amount", "content_hash"},
		pgx.CopyFromRows(staged),
	); err != nil {
		return nil, err
	}

	// 2. Intra-file conflicts: same key, more than one distinct payload.
	conflicts, err := intraFileConflicts(ctx, tx)
	if err != nil {
		return nil, err
	}
	if len(conflicts) > 0 {
		return nil, &ConflictError{Conflicts: conflicts}
	}

	// 3. Conflicts against already-imported rows.
	existing, err := conflictsWithExisting(ctx, tx, account.ID)
	if err != nil {
		return nil, err
	}
	if len(existing) > 0 {
		return nil, &ConflictError{Conflicts: existing}
	}

	// 4. Create the batch row (will be rolled back together with everything
	//    on any later error).
	var batchID int64
	var actorRef any
	if actorID != 0 {
		actorRef = actorID
	}
	err = tx.QueryRow(ctx, `
		INSERT INTO import_batches
		    (account_id, filename, total_rows, inserted_rows, duplicate_rows,
		     status, created_by)
		VALUES ($1,$2,$3,0,0,'completed',$4)
		RETURNING id`,
		account.ID, filename, len(rows), actorRef).Scan(&batchID)
	if err != nil {
		return nil, err
	}

	// 5. Insert only key-missing rows; count exact duplicates.
	var inserted, duplicate int64
	err = tx.QueryRow(ctx, `
		WITH picked AS (
			SELECT DISTINCT ON (resource_id, usage_date)
			       line, resource_id, service, usage_date, currency,
			       amount, content_hash
			FROM stage_rows
			ORDER BY resource_id, usage_date, line
		), dup AS (
			SELECT p.* FROM picked p
			JOIN billing_records b
			  ON b.account_id = $1
			 AND b.resource_id = p.resource_id
			 AND b.usage_date = p.usage_date
			 AND b.content_hash = p.content_hash
		), ins AS (
			INSERT INTO billing_records
			    (account_id, resource_id, service, usage_date, currency,
			     amount, content_hash, batch_id)
			SELECT $1, p.resource_id, p.service, p.usage_date, p.currency,
			       p.amount, p.content_hash, $2
			FROM picked p
			WHERE NOT EXISTS (
			    SELECT 1 FROM billing_records b
			    WHERE b.account_id = $1
			      AND b.resource_id = p.resource_id
			      AND b.usage_date = p.usage_date)
			RETURNING 1
		)
		SELECT
		  (SELECT count(*) FROM ins),
		  (SELECT count(*) FROM stage_rows) - (SELECT count(*) FROM ins)`,
		account.ID, batchID).
		Scan(&inserted, &duplicate)
	if err != nil {
		return nil, err
	}
	if _, err := tx.Exec(ctx,
		`UPDATE import_batches SET inserted_rows=$2, duplicate_rows=$3 WHERE id=$1`,
		batchID, inserted, duplicate); err != nil {
		return nil, err
	}

	// 6. Recompute summaries only for affected slices.
	if inserted > 0 {
		if err := recomputeSummaries(ctx, tx, account.ID); err != nil {
			return nil, err
		}
		// 7. Budget alerts for affected cost-center months.
		if err := evaluateBudgets(ctx, tx, account.ID, batchID); err != nil {
			return nil, err
		}
		// 8. Anomaly re-evaluation for affected dates (+30 day tail).
		if err := evaluateAnomalies(ctx, tx, account.ID, batchID); err != nil {
			return nil, err
		}
	}

	if err := tx.Commit(ctx); err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "40001" {
			return nil, fmt.Errorf("commit serialization failure: %w", err)
		}
		return nil, err
	}
	return &ImportResult{
		BatchID: batchID, TotalRows: len(rows),
		InsertedRows: int(inserted), DuplicateRows: int(duplicate),
	}, nil
}

func contentHash(r csvparse.Row) string {
	sum := sha256.Sum256([]byte(strings.Join([]string{
		r.ResourceID, r.Service, r.Date.Format("2006-01-02"),
		r.Currency, r.Amount.String(),
	}, "|")))
	return hex.EncodeToString(sum[:])
}

func intraFileConflicts(ctx context.Context, tx pgx.Tx) ([]ImportConflict, error) {
	rows, err := tx.Query(ctx, `
		SELECT resource_id, usage_date::text,
		       min(service), min(currency)::text, min(amount)::text,
		       count(DISTINCT content_hash),
		       array_agg(line ORDER BY line)
		FROM stage_rows
		GROUP BY resource_id, usage_date
		HAVING count(DISTINCT content_hash) > 1`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ImportConflict
	for rows.Next() {
		var c ImportConflict
		var service, currency, amount string
		var nDistinct int
		if err := rows.Scan(&c.ResourceID, &c.Date, &service, &currency, &amount,
			&nDistinct, &c.Lines); err != nil {
			return nil, err
		}
		c.Currency = strings.TrimSpace(currency)
		c.Key = c.ResourceID + "@" + c.Date
		out = append(out, c)
	}
	return out, rows.Err()
}

func conflictsWithExisting(ctx context.Context, tx pgx.Tx, accountID int64) ([]ImportConflict, error) {
	rows, err := tx.Query(ctx, `
		SELECT s.resource_id, s.usage_date::text, s.currency::text,
		       b.service, b.amount::text,
		       array_agg(DISTINCT s.line ORDER BY s.line)
		FROM (SELECT DISTINCT resource_id, usage_date, currency, service,
		             amount, content_hash, line FROM stage_rows) s
		JOIN billing_records b
		  ON b.account_id = $1
		 AND b.resource_id = s.resource_id
		 AND b.usage_date = s.usage_date
		 AND b.content_hash <> s.content_hash
		GROUP BY s.resource_id, s.usage_date, s.currency, b.service, b.amount
		ORDER BY s.usage_date, s.resource_id`, accountID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ImportConflict
	for rows.Next() {
		c := ImportConflict{}
		var currency, existingService, existingAmount string
		if err := rows.Scan(&c.ResourceID, &c.Date, &currency,
			&existingService, &existingAmount, &c.Lines); err != nil {
			return nil, err
		}
		c.Currency = strings.TrimSpace(currency)
		c.ExistingService = existingService
		c.ExistingAmount = existingAmount
		c.Key = c.ResourceID + "@" + c.Date
		out = append(out, c)
	}
	return out, rows.Err()
}

// recomputeSummaries recomputes account day/month and cost-center day/month
// totals for exactly the (currency, date/month) slices touched by staged rows.
// It is idempotent: recomputing from billing_records yields the same result
// whether invoked incrementally or during a full rebuild.
func recomputeSummaries(ctx context.Context, tx pgx.Tx, accountID int64) error {
	if _, err := tx.Exec(ctx, `
		INSERT INTO daily_summaries
		    (scope, account_id, cost_center_id, usage_date, currency,
		     total_amount, record_count, updated_at)
		SELECT 'account', $1, 0, r.usage_date, r.currency,
		       round(sum(r.amount), 6), count(*), now()
		FROM billing_records r
		WHERE r.account_id = $1
		  AND (r.usage_date, r.currency) IN (
		        SELECT DISTINCT usage_date, currency FROM stage_rows)
		GROUP BY r.usage_date, r.currency
		ON CONFLICT (scope, account_id, cost_center_id, usage_date, currency)
		DO UPDATE SET total_amount = EXCLUDED.total_amount,
		              record_count = EXCLUDED.record_count,
		              updated_at = now()`, accountID); err != nil {
		return err
	}

	if _, err := tx.Exec(ctx, `
		INSERT INTO monthly_summaries
		    (scope, account_id, cost_center_id, month, currency,
		     total_amount, record_count, updated_at)
		SELECT 'account', $1, 0, date_trunc('month', r.usage_date)::date,
		       r.currency, round(sum(r.amount), 6), count(*), now()
		FROM billing_records r
		WHERE r.account_id = $1
		  AND (date_trunc('month', r.usage_date)::date, r.currency) IN (
		        SELECT DISTINCT date_trunc('month', usage_date)::date, currency
		        FROM stage_rows)
		GROUP BY date_trunc('month', r.usage_date)::date, r.currency
		ON CONFLICT (scope, account_id, cost_center_id, month, currency)
		DO UPDATE SET total_amount = EXCLUDED.total_amount,
		              record_count = EXCLUDED.record_count,
		              updated_at = now()`, accountID); err != nil {
		return err
	}

	// Cost-center scope spans every account in the cost center, so recompute
	// the whole (cc, date/month, currency) slice, not just this account.
	if _, err := tx.Exec(ctx, `
		INSERT INTO daily_summaries
		    (scope, account_id, cost_center_id, usage_date, currency,
		     total_amount, record_count, updated_at)
		SELECT 'cost_center', 0, a.cost_center_id,
		       r.usage_date, r.currency,
		       round(sum(r.amount), 6), count(*), now()
		FROM billing_records r
		JOIN accounts a ON a.id = r.account_id
		WHERE a.cost_center_id = (SELECT cost_center_id FROM accounts WHERE id=$1)
		  AND (r.usage_date, r.currency) IN (
		        SELECT DISTINCT usage_date, currency FROM stage_rows)
		GROUP BY a.cost_center_id, r.usage_date, r.currency
		ON CONFLICT (scope, account_id, cost_center_id, usage_date, currency)
		DO UPDATE SET total_amount = EXCLUDED.total_amount,
		              record_count = EXCLUDED.record_count,
		              updated_at = now()`, accountID); err != nil {
		return err
	}

	_, err := tx.Exec(ctx, `
		INSERT INTO monthly_summaries
		    (scope, account_id, cost_center_id, month, currency,
		     total_amount, record_count, updated_at)
		SELECT 'cost_center', 0, a.cost_center_id,
		       date_trunc('month', r.usage_date)::date, r.currency,
		       round(sum(r.amount), 6), count(*), now()
		FROM billing_records r
		JOIN accounts a ON a.id = r.account_id
		WHERE a.cost_center_id = (SELECT cost_center_id FROM accounts WHERE id=$1)
		  AND (date_trunc('month', r.usage_date)::date, r.currency) IN (
		        SELECT DISTINCT date_trunc('month', usage_date)::date, currency
		        FROM stage_rows)
		GROUP BY a.cost_center_id, date_trunc('month', r.usage_date)::date, r.currency
		ON CONFLICT (scope, account_id, cost_center_id, month, currency)
		DO UPDATE SET total_amount = EXCLUDED.total_amount,
		              record_count = EXCLUDED.record_count,
		              updated_at = now()`, accountID)
	return err
}

// dayTotalsZeroFilled returns every day in [start,end] for one
// (account, currency) with zero spend days included, as date -> amount.
func dayTotalsZeroFilled(ctx context.Context, q pgx.Tx,
	accountID int64, currency string, start, end time.Time) (map[string]decimal.Decimal, error) {
	rows, err := q.Query(ctx, `
		SELECT d::date::text, COALESCE(round(t.s, 6), 0)::text
		FROM generate_series($2::date, $3::date, interval '1 day') AS d
		LEFT JOIN (
		    SELECT usage_date, sum(amount) AS s
		    FROM billing_records
		    WHERE account_id = $1 AND currency::text = $4
		    GROUP BY usage_date
		) t ON t.usage_date = d
		ORDER BY d`, accountID, pgDate(start), pgDate(end), currency)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make(map[string]decimal.Decimal)
	for rows.Next() {
		var day, amt string
		if err := rows.Scan(&day, &amt); err != nil {
			return nil, err
		}
		out[day], _ = decimal.NewFromString(amt)
	}
	return out, rows.Err()
}

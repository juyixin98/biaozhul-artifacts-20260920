// Package recon reconciles internal books against the simulated channel_events
// feed per merchant per UTC day. A discrepancy of more than 5 cents (in either
// direction) on any check produces a 'discrepancy' reconciliation item and a
// non-zero discrepancy count in the run summary.
//
// Idempotency/resume: the unique (merchant_id, run_date) constraint means at
// most one run exists per pair; re-running returns the existing completed run
// without creating duplicate items.
package recon

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/clearsettle/clearsettle/internal/audit"
	"github.com/clearsettle/clearsettle/internal/store"
)

// DiscrepancyThreshold is the inclusive tolerance: |diff| <= 5 is a match.
const DiscrepancyThreshold int64 = 5

type Service struct {
	pool *pgxpool.Pool
	q    *store.Queries
}

func New(pool *pgxpool.Pool) *Service { return &Service{pool: pool, q: store.New(pool)} }

type RunSummary struct {
	RunID         uuid.UUID `json:"run_id"`
	MerchantID    uuid.UUID `json:"merchant_id"`
	Date          string    `json:"date"`
	Status        string    `json:"status"`
	Checks        int       `json:"checks"`
	Discrepancies int       `json:"discrepancies"`
	Reused        bool      `json:"reused"`
	Items         []Item    `json:"items,omitempty"`
}

type Item struct {
	Check      string `json:"check"`
	Expected   int64  `json:"expected"`
	Actual     int64  `json:"actual"`
	Difference int64  `json:"difference"`
	Severity   string `json:"severity"`
	Detail     string `json:"detail,omitempty"`
}

// Run reconciles one merchant/day. Safe to call concurrently and repeatedly.
func (s *Service) Run(ctx context.Context, merchantID uuid.UUID, day time.Time, actor audit.Entry) (RunSummary, error) {
	day = day.UTC().Truncate(24 * time.Hour)
	var summary RunSummary
	summary.MerchantID = merchantID
	summary.Date = day.Format("2006-01-02")

	err := pgx.BeginTxFunc(ctx, s.pool, pgx.TxOptions{}, func(tx pgx.Tx) error {
		q := store.New(tx)

		var lockKey int64
		if err := tx.QueryRow(ctx, `SELECT hashtext($1)::bigint`,
			fmt.Sprintf("recon:%s:%s", merchantID, day.Format("2006-01-02"))).Scan(&lockKey); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, lockKey); err != nil {
			return err
		}

		// Create the run row; if it exists we either reuse (completed) or take
		// over (running, i.e. a crashed previous attempt).
		run, err := q.UpsertReconRun(ctx, store.UpsertReconRunParams{
			MerchantID: merchantID, RunDate: dateArg(day),
		})
		var zero uuid.UUID
		if err != nil || run.ID == zero {
			existing, gerr := q.GetReconRunForUpdate(ctx, store.GetReconRunForUpdateParams{
				MerchantID: merchantID, RunDate: dateArg(day),
			})
			if gerr != nil {
				return gerr
			}
			if existing.Status == "completed" {
				items, lerr := q.ListReconItems(ctx, existing.ID)
				if lerr != nil {
					return lerr
				}
				summary.RunID = existing.ID
				summary.Status = "completed"
				summary.Reused = true
				summary.Checks = len(items)
				for _, it := range items {
					if it.Severity == "discrepancy" {
						summary.Discrepancies++
					}
				}
				summary.Items = convertItems(items)
				return nil
			}
			run = existing // takeover of a crashed 'running' row
			// Discard any partially-written items from the crashed attempt.
			if err := q.DeleteReconItems(ctx, run.ID); err != nil {
				return err
			}
		}
		summary.RunID = run.ID

		items, err := s.computeChecks(ctx, q, merchantID, day)
		if err != nil {
			_ = q.FailReconRun(ctx, run.ID)
			return err
		}
		for _, it := range items {
			if err := q.InsertReconItem(ctx, store.InsertReconItemParams{
				RunID:      run.ID,
				CheckName:  it.Check,
				Expected:   it.Expected,
				Actual:     it.Actual,
				Difference: it.Difference,
				Severity:   it.Severity,
				Detail:     nullText(it.Detail),
			}); err != nil {
				return err
			}
			summary.Checks++
			if it.Severity == "discrepancy" {
				summary.Discrepancies++
			}
		}
		summary.Items = items

		detail, _ := json.Marshal(map[string]any{
			"checks": summary.Checks, "discrepancies": summary.Discrepancies,
		})
		completed, err := q.CompleteReconRun(ctx, store.CompleteReconRunParams{
			ID: run.ID, MerchantID: merchantID, DetailSummary: detail,
		})
		if err != nil {
			return err
		}
		summary.Status = completed.Status

		actor.MerchantID = &merchantID
		actor.Action = "reconciliation.run"
		actor.TargetType = "reconciliation_run"
		actor.TargetID = run.ID.String()
		actor.Detail = map[string]any{
			"date": summary.Date, "checks": summary.Checks,
			"discrepancies": summary.Discrepancies,
		}
		return audit.Write(ctx, q, actor)
	})
	return summary, err
}

func (s *Service) computeChecks(ctx context.Context, q store.Querier, merchantID uuid.UUID, day time.Time) ([]Item, error) {
	d := dateArg(day)
	intCap, err := q.InternalCaptureTotals(ctx, store.InternalCaptureTotalsParams{MerchantID: merchantID, Day: d})
	if err != nil {
		return nil, err
	}
	chCap, err := q.ChannelCaptureTotals(ctx, store.ChannelCaptureTotalsParams{MerchantID: merchantID, EventDate: d})
	if err != nil {
		return nil, err
	}
	intRef, err := q.InternalRefundTotals(ctx, store.InternalRefundTotalsParams{MerchantID: merchantID, Day: d})
	if err != nil {
		return nil, err
	}
	chRef, err := q.ChannelRefundTotals(ctx, store.ChannelRefundTotalsParams{MerchantID: merchantID, EventDate: d})
	if err != nil {
		return nil, err
	}
	// The ledger self-check is internal-only: no posting group may be unbalanced.
	unbalanced, err := q.UnbalancedPostingCount(ctx)
	if err != nil {
		return nil, err
	}

	// Channel refund events record fee_delta as a negative number (a reversal
	// of the capture fee); internal fee_refund is stored positive. Compare
	// magnitudes by negating the channel figure.
	chRefFee := -chRef.Fees
	items := []Item{
		diffItem("capture_gross", intCap.Gross, chCap.Gross,
			fmt.Sprintf("internal captures=%d channel captures=%d", intCap.Gross, chCap.Gross)),
		diffItem("capture_fees", intCap.Fees, chCap.Fees,
			fmt.Sprintf("internal fees=%d channel fees=%d", intCap.Fees, chCap.Fees)),
		diffItem("refund_gross", intRef.Gross, chRef.Gross,
			fmt.Sprintf("internal refunds=%d channel refunds=%d", intRef.Gross, chRef.Gross)),
		diffItem("refund_fees", intRef.Fees, chRefFee,
			fmt.Sprintf("internal fee refunds=%d channel fee refunds=%d", intRef.Fees, chRefFee)),
		{
			Check: "ledger_balance", Expected: 0, Actual: int64(unbalanced),
			Difference: int64(unbalanced),
			Severity:   severity(int64(unbalanced), 0),
			Detail:     fmt.Sprintf("unbalanced posting groups: %d", unbalanced),
		},
	}
	return items, nil
}

// diffItem compares internal (expected) to channel (actual); |actual-expected|
// greater than 5 cents is a discrepancy.
func diffItem(check string, expected, actual int64, detail string) Item {
	diff := actual - expected
	return Item{
		Check: check, Expected: expected, Actual: actual, Difference: diff,
		Severity: severity(diff, DiscrepancyThreshold), Detail: detail,
	}
}

// severity returns 'match' when |diff| <= threshold.
func severity(diff, threshold int64) string {
	if diff < 0 {
		diff = -diff
	}
	if diff > threshold {
		return "discrepancy"
	}
	return "match"
}

func convertItems(rows []store.ReconciliationItem) []Item {
	out := make([]Item, 0, len(rows))
	for _, r := range rows {
		out = append(out, Item{
			Check: r.CheckName, Expected: r.Expected, Actual: r.Actual,
			Difference: r.Difference, Severity: r.Severity, Detail: r.Detail.String,
		})
	}
	return out
}

// RunSweep reconciles every merchant that had activity on the given day.
func (s *Service) RunSweep(ctx context.Context, day time.Time, actor audit.Entry) ([]RunSummary, error) {
	d := dateArg(day.UTC())
	ids := map[uuid.UUID]bool{}
	caps, err := s.q.MerchantsWithCaptureOn(ctx, d)
	if err != nil {
		return nil, err
	}
	for _, id := range caps {
		ids[id] = true
	}
	refs, err := s.q.MerchantsWithRefundOn(ctx, d)
	if err != nil {
		return nil, err
	}
	for _, id := range refs {
		ids[id] = true
	}
	var out []RunSummary
	for id := range ids {
		r, err := s.Run(ctx, id, day, actor)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return out, err
		}
		out = append(out, r)
	}
	return out, nil
}

func dateArg(t time.Time) pgtype.Date {
	t = t.UTC()
	return pgtype.Date{Time: time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC), Valid: true}
}

func nullText(s string) pgtype.Text {
	if s == "" {
		return pgtype.Text{Valid: false}
	}
	return pgtype.Text{String: s, Valid: true}
}

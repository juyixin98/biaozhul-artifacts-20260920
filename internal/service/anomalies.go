package service

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/shopspring/decimal"

	"costlens/internal/decimalx"
)

// Anomaly semantics (documented contract):
//
//   - Baseline for day D = the 30 COMPLETE calendar days D-30 .. D-1.
//     "Complete" means strictly earlier than today (the DB current date);
//     the current day is never part of any baseline or evaluated.
//   - Zero-spend days are real observations: missing billing rows for a day
//     contribute 0, so a baseline is never built only from positive days.
//   - Threshold = mean + 2 * population standard deviation (sigma, not s).
//   - Status definitions:
//       anomalous                - actual > threshold
//       normal                   - actual <= threshold (variance > 0)
//       zero_variance_below      - variance == 0 AND actual == mean
//                                  (equal-spend day is not an anomaly)
//       insufficient_history     - fewer than 30 calendar days exist between
//                                  the account's first record and D
//     variance == 0 AND actual > mean is anomalous (a spike above an
//     otherwise perfectly flat period).
//
// Evaluations are append-only and versioned per (account, date, currency).
// Late data that changes a baseline produces a NEW version; old versions and
// any alerts derived from them are retained.

const baselineDays = 30

type evalResult struct {
	status    string
	actual    decimal.Decimal
	mean      decimal.Decimal // zero/invalid when insufficient
	std       decimal.Decimal
	threshold decimal.Decimal
	start     time.Time
	end       time.Time
	days      int
	hasStats  bool
}

// evaluateAnomalies re-evaluates, for each distinct currency in the batch,
// every complete day from the account's earliest record date through the
// latest day whose baseline can have moved (min(staged date) + 30), capped
// at yesterday. Starting at the earliest record date — exactly like a full
// rebuild — keeps incremental and rebuilt evaluations identical (early days
// are stored as insufficient_history). writeEvalVersion appends a version
// only when the result changed, so unchanged days are not rewritten.
func evaluateAnomalies(ctx context.Context, tx pgx.Tx, accountID, batchID int64) error {
	var minDate, today time.Time
	if err := tx.QueryRow(ctx, `
		SELECT min(usage_date), CURRENT_DATE FROM stage_rows`).Scan(&minDate, &today); err != nil {
		return err
	}
	tailEnd := minDate.AddDate(0, 0, baselineDays)
	yesterday := today.AddDate(0, 0, -1)
	if tailEnd.After(yesterday) {
		tailEnd = yesterday
	}

	rows, err := tx.Query(ctx,
		`SELECT DISTINCT currency::text FROM stage_rows ORDER BY 1`)
	if err != nil {
		return err
	}
	var currencies []string
	for rows.Next() {
		var c string
		if err := rows.Scan(&c); err != nil {
			rows.Close()
			return err
		}
		currencies = append(currencies, strings.TrimSpace(c))
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}

	for _, ccy := range currencies {
		var earliest time.Time
		if err := tx.QueryRow(ctx, `
			SELECT min(usage_date) FROM billing_records
			WHERE account_id=$1 AND currency::text=$2`,
			accountID, ccy).Scan(&earliest); err != nil {
			return err
		}
		if tailEnd.Before(earliest) {
			continue
		}
		if err := evaluateAccountCurrencyRange(ctx, tx, accountID, ccy,
			earliest, tailEnd, batchID); err != nil {
			return err
		}
	}
	return nil
}

// evaluateAccountCurrencyRange evaluates complete days in [from,to] for one
// account/currency. If the 30-day window extends before the account's first
// billing day, those days are treated as zero spend.
func evaluateAccountCurrencyRange(ctx context.Context, tx pgx.Tx,
	accountID int64, currency string, from, to time.Time, batchID int64) error {
	if to.Before(from) {
		return nil
	}
	// Fetch zero-filled totals for [from-30, to]: every needed baseline and
	// target value is in that range.
	first := from.AddDate(0, 0, -baselineDays)
	totals, err := dayTotalsZeroFilled(ctx, tx, accountID, currency, first, to)
	if err != nil {
		return err
	}

	// "from" already is the account's earliest record date for the currency
	// (caller guarantees it); keep the query as a defensive cross-check.
	var earliestRecord *time.Time
	var earliest time.Time
	err = tx.QueryRow(ctx, `
		SELECT min(usage_date) FROM billing_records
		WHERE account_id=$1 AND currency::text=$2`,
		accountID, currency).Scan(&earliestRecord)
	if err != nil {
		return err
	}
	if earliestRecord != nil {
		earliest = *earliestRecord
	}

	for d := from; !d.After(to); d = d.AddDate(0, 0, 1) {
		res := computeEval(totals, d, earliest, earliestRecord != nil)
		if err := writeEvalVersion(ctx, tx, accountID, currency, d, res, batchID); err != nil {
			return err
		}
	}
	return nil
}

func computeEval(totals map[string]decimal.Decimal, day, earliest time.Time, hasRecord bool) evalResult {
	actual := totals[day.Format("2006-01-02")]
	r := evalResult{status: "normal", actual: actual}

	if !hasRecord || day.Sub(earliest).Hours() < 24*baselineDays {
		r.status = "insufficient_history"
		return r
	}

	start := day.AddDate(0, 0, -baselineDays)
	end := day.AddDate(0, 0, -1)
	sum := decimal.Zero
	vals := make([]decimal.Decimal, 0, baselineDays)
	for bd := start; !bd.After(end); bd = bd.AddDate(0, 0, 1) {
		v := totals[bd.Format("2006-01-02")] // zero-filled
		vals = append(vals, v)
		sum = sum.Add(v)
	}
	n := decimal.NewFromInt(baselineDays)
	mean := sum.DivRound(n, 8)
	varS := decimal.Zero
	for _, v := range vals {
		diff := v.Sub(mean)
		varS = varS.Add(diff.Mul(diff))
	}
	// Population variance; round variance to 12 places before sqrt to absorb
	// the mean-rounding noise so a constant series gives exactly 0.
	variance := varS.DivRound(n, 12)
	std := decimal.Zero
	if variance.IsPositive() {
		std = decimalx.Sqrt(variance, 8)
	}
	threshold := mean.Add(std.Mul(decimal.NewFromInt(2)))
	r.mean, r.std, r.threshold = mean, std, threshold
	r.start, r.end, r.days, r.hasStats = start, end, baselineDays, true

	switch {
	case variance.IsZero() && actual.Equal(mean):
		r.status = "zero_variance_below"
	case actual.GreaterThan(threshold):
		r.status = "anomalous"
	default:
		r.status = "normal"
	}
	return r
}

// writeEvalVersion appends a new version only when the result differs from
// the currently stored latest version. Import/rebuild repeated without data
// change therefore produces no extra rows.
func writeEvalVersion(ctx context.Context, tx pgx.Tx,
	accountID int64, currency string, day time.Time, res evalResult,
	batchID int64) error {

	var latestStatus, latestActual string
	var latestMean, latestStd, latestThreshold *string
	var latestDays int32
	err := tx.QueryRow(ctx, `
		SELECT status, actual_amount::text, baseline_mean::text,
		       baseline_std::text, threshold_amount::text, baseline_days
		FROM anomaly_evaluations
		WHERE account_id=$1 AND usage_date=$2 AND currency::text=$3
		ORDER BY version DESC LIMIT 1`,
		accountID, pgDate(day), currency).
		Scan(&latestStatus, &latestActual, &latestMean, &latestStd,
			&latestThreshold, &latestDays)
	same := false
	if err == nil {
		same = latestStatus == res.status &&
			latestActual == res.actual.String() &&
			nullableEqual(latestMean, res, true) &&
			nullableEqual(latestStd, res, false) &&
			latestDays == int32(res.days)
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return err
	}
	if same {
		return nil
	}

	var version int32
	if err := tx.QueryRow(ctx, `
		SELECT COALESCE(max(version),0)+1
		FROM anomaly_evaluations
		WHERE account_id=$1 AND usage_date=$2 AND currency::text=$3`,
		accountID, pgDate(day), currency).Scan(&version); err != nil {
		return err
	}

	var mean, std, threshold, start, end interface{}
	if res.hasStats {
		mean = decimalx.ToNumeric(res.mean)
		std = decimalx.ToNumeric(res.std)
		threshold = decimalx.ToNumeric(res.threshold)
		start = pgDate(res.start)
		end = pgDate(res.end)
	}
	var batch interface{}
	if batchID != 0 {
		batch = batchID
	}
	_, err = tx.Exec(ctx, `
		INSERT INTO anomaly_evaluations
		    (account_id, usage_date, currency, version, status, actual_amount,
		     baseline_mean, baseline_std, threshold_amount, baseline_start,
		     baseline_end, baseline_days, created_by_batch)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`,
		accountID, pgDate(day), currency, version, res.status,
		decimalx.ToNumeric(res.actual), mean, std, threshold, start, end,
		res.days, batch)
	return err
}

func nullableEqual(latest *string, res evalResult, isMean bool) bool {
	if !res.hasStats {
		return latest == nil
	}
	if latest == nil {
		return false
	}
	v := res.std
	if isMean {
		v = res.mean
	}
	return *latest == v.String()
}

// Rebuild recomputes ALL summaries from billing_records and re-evaluates
// anomaly history for every (account, currency), all in one transaction under
// the same advisory lock used by import. Imports block on the lock while the
// rebuild runs, then commit against rebuilt data: nothing is lost or
// double-counted. Budget alerts are intentionally never rewritten — they are
// immutable historical evidence.
func (s *Service) Rebuild(ctx context.Context, actorID int64) (int64, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, advisoryLockID); err != nil {
		return 0, err
	}

	var eventID int64
	var actor interface{}
	if actorID != 0 {
		actor = actorID
	}
	if err := tx.QueryRow(ctx,
		`INSERT INTO rebuild_events(status, started_by) VALUES ('running',$1) RETURNING id`,
		actor).Scan(&eventID); err != nil {
		return 0, err
	}

	for _, table := range []string{"daily_summaries", "monthly_summaries"} {
		if _, err := tx.Exec(ctx, `TRUNCATE `+table+` RESTART IDENTITY`); err != nil {
			return 0, err
		}
	}

	if _, err := tx.Exec(ctx, `
		INSERT INTO daily_summaries
		    (scope, account_id, cost_center_id, usage_date, currency,
		     total_amount, record_count, updated_at)
		SELECT 'account', account_id, 0, usage_date, currency,
		       round(sum(amount),6), count(*), now()
		FROM billing_records GROUP BY account_id, usage_date, currency`); err != nil {
		return 0, err
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO monthly_summaries
		    (scope, account_id, cost_center_id, month, currency,
		     total_amount, record_count, updated_at)
		SELECT 'account', account_id, 0,
		       date_trunc('month', usage_date)::date, currency,
		       round(sum(amount),6), count(*), now()
		FROM billing_records
		GROUP BY account_id, date_trunc('month', usage_date)::date, currency`); err != nil {
		return 0, err
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO daily_summaries
		    (scope, account_id, cost_center_id, usage_date, currency,
		     total_amount, record_count, updated_at)
		SELECT 'cost_center', 0, a.cost_center_id, r.usage_date, r.currency,
		       round(sum(r.amount),6), count(*), now()
		FROM billing_records r JOIN accounts a ON a.id = r.account_id
		GROUP BY a.cost_center_id, r.usage_date, r.currency`); err != nil {
		return 0, err
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO monthly_summaries
		    (scope, account_id, cost_center_id, month, currency,
		     total_amount, record_count, updated_at)
		SELECT 'cost_center', 0, a.cost_center_id,
		       date_trunc('month', r.usage_date)::date, r.currency,
		       round(sum(r.amount),6), count(*), now()
		FROM billing_records r JOIN accounts a ON a.id = r.account_id
		GROUP BY a.cost_center_id, date_trunc('month', r.usage_date)::date, r.currency`); err != nil {
		return 0, err
	}

	// Re-run anomaly evaluation from scratch (new versions appended, old kept).
	var today time.Time
	if err := tx.QueryRow(ctx, `SELECT CURRENT_DATE`).Scan(&today); err != nil {
		return 0, err
	}
	yesterday := today.AddDate(0, 0, -1)
	keys, err := distinctAccountCurrencies(ctx, tx)
	if err != nil {
		return 0, err
	}
	for _, k := range keys {
		var earliest time.Time
		if err := tx.QueryRow(ctx, `
			SELECT min(usage_date) FROM billing_records
			WHERE account_id=$1 AND currency::text=$2`,
			k.accountID, k.currency).Scan(&earliest); err != nil {
			return 0, err
		}
		// Days before earliest+30 are all insufficient_history; writing them
		// is harmless and keeps history complete, but we start at earliest to
		// avoid unbounded zero padding.
		if err := evaluateAccountCurrencyRange(ctx, tx, k.accountID,
			k.currency, earliest, yesterday, 0); err != nil {
			return 0, err
		}
	}

	if _, err := tx.Exec(ctx,
		`UPDATE rebuild_events SET status='completed', finished_at=now() WHERE id=$1`,
		eventID); err != nil {
		return 0, err
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, err
	}
	return eventID, nil
}

type accountCurrency struct {
	accountID int64
	currency  string
}

func distinctAccountCurrencies(ctx context.Context, tx pgx.Tx) ([]accountCurrency, error) {
	rows, err := tx.Query(ctx, `
		SELECT DISTINCT account_id, currency::text
		FROM billing_records ORDER BY 1, 2`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []accountCurrency
	for rows.Next() {
		var k accountCurrency
		var c string
		if err := rows.Scan(&k.accountID, &c); err != nil {
			return nil, err
		}
		k.currency = strings.TrimSpace(c)
		out = append(out, k)
	}
	return out, rows.Err()
}

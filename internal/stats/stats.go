// Package stats implements the anomaly baseline rule.
//
// Rule: for a target day D the baseline is the 30 immediately preceding complete
// days D-30 .. D-1, one value per day per (account, currency). Days without a
// bill line contribute zero ("include zero-spend days"). The current day never
// participates in its own baseline.
//
// Defined edge cases:
//   - InsufficientHistory: the account itself did not exist for the full
//     window (created after D-30). An existing account with zero spend is not
//     insufficient: zero-spend days are valid baseline observations. The
//     evaluation is stored with status insufficient_history, no mean/std/
//     threshold, and can never be an anomaly.
//   - Zero variance (std == 0): threshold equals mean. An anomaly is raised
//     only when actual > mean (strictly); an equal value is normal, so a
//     constant-spend series does not alert on itself.
package stats

import (
	"time"

	"costlens/internal/money"

	"github.com/shopspring/decimal"
)

const WindowDays = 30

type Status string

const (
	StatusAnomaly             Status = "anomaly"
	StatusNormal              Status = "normal"
	StatusInsufficientHistory Status = "insufficient_history"
)

type Result struct {
	Status      Status
	Mean        decimal.Decimal // zero when insufficient
	Std         decimal.Decimal
	Threshold   decimal.Decimal // mean + 2*std, zero when insufficient
	HistoryDays int
}

// BaselineWindow returns the [from, to] date range (inclusive, UTC midnights)
// of the 30 complete days preceding target.
func BaselineWindow(target time.Time) (from, to time.Time) {
	t := truncDate(target)
	to = t.AddDate(0, 0, -1)
	from = t.AddDate(0, 0, -WindowDays)
	return from, to
}

// Evaluate computes the anomaly result for target given a daily-value map for
// the baseline window. daily must already contain entries for observed days in
// [D-30, D-1]; missing days are treated as zero. accountCreated is the date
// the account came into existence: if it was created after D-30, the account
// has not lived through the full window and history is insufficient.
func Evaluate(daily map[time.Time]decimal.Decimal, accountCreated, target time.Time, actual decimal.Decimal) Result {
	from, to := BaselineWindow(target)
	if truncDate(accountCreated).After(from) {
		return Result{Status: StatusInsufficientHistory, HistoryDays: 0}
	}

	vals := make([]decimal.Decimal, 0, WindowDays)
	for d := from; !d.After(to); d = d.AddDate(0, 0, 1) {
		v, ok := daily[truncDate(d)]
		if !ok {
			v = decimal.Zero
		}
		vals = append(vals, v)
	}
	if len(vals) != WindowDays {
		return Result{Status: StatusInsufficientHistory, HistoryDays: len(vals)}
	}

	mean, std := money.MeanStd(vals)
	threshold := mean.Add(std.Mul(decimal.NewFromInt(2)))
	r := Result{
		Status:      StatusNormal,
		Mean:        money.RoundStorage(mean),
		Std:         money.RoundStorage(std),
		Threshold:   money.RoundStorage(threshold),
		HistoryDays: WindowDays,
	}
	if actual.GreaterThan(threshold) {
		r.Status = StatusAnomaly
	}
	return r
}

func truncDate(t time.Time) time.Time {
	y, m, d := t.Date()
	return time.Date(y, m, d, 0, 0, 0, 0, t.Location())
}

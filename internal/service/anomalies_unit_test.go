package service

import (
	"testing"
	"time"

	"github.com/shopspring/decimal"
)

func d(s string) time.Time {
	t, err := time.Parse("2006-01-02", s)
	if err != nil {
		panic(err)
	}
	return t
}

func totalsFor(series map[string]string) map[string]decimal.Decimal {
	out := map[string]decimal.Decimal{}
	for k, v := range series {
		out[k] = decimal.RequireFromString(v)
	}
	return out
}

// build 30-day baseline ending at day-1 with constant value v; days without
// keys are zero-filled by dayTotalsZeroFilled in real runs, so the test
// provides them explicitly when zeros matter.
func constantBaseline(day string, v string, startOffset int) map[string]decimal.Decimal {
	t := map[string]decimal.Decimal{}
	end := d(day).AddDate(0, 0, -1)
	for i := startOffset; i >= 1; i-- {
		bd := end.AddDate(0, 0, -(i - 1))
		t[bd.Format("2006-01-02")] = decimal.RequireFromString(v)
	}
	return t
}

func TestEvalInsufficientHistory(t *testing.T) {
	day := d("2026-02-15")
	earliest := d("2026-02-01") // only 14 days before day
	totals := constantBaseline("2026-02-15", "10", 14)
	totals["2026-02-15"] = decimal.RequireFromString("1000")

	r := computeEval(totals, day, earliest, true)
	if r.status != "insufficient_history" {
		t.Fatalf("want insufficient_history, got %s", r.status)
	}
}

func TestEvalZeroVarianceEqual(t *testing.T) {
	// 30 days of exactly 10.00; day itself 10.00 -> zero_variance_below.
	day := d("2026-03-15")
	earliest := d("2026-01-01")
	totals := constantBaseline("2026-03-15", "10", 30)
	totals["2026-03-15"] = decimal.RequireFromString("10")

	r := computeEval(totals, day, earliest, true)
	if r.status != "zero_variance_below" {
		t.Fatalf("want zero_variance_below, got %s", r.status)
	}
	if r.threshold.String() != "10" {
		t.Fatalf("want threshold 10, got %s", r.threshold)
	}
}

func TestEvalZeroVarianceSpike(t *testing.T) {
	// 30 flat days at 10, day = 10.01 must already be anomalous.
	day := d("2026-03-15")
	earliest := d("2026-01-01")
	totals := constantBaseline("2026-03-15", "10", 30)
	totals["2026-03-15"] = decimal.RequireFromString("10.01")

	r := computeEval(totals, day, earliest, true)
	if r.status != "anomalous" {
		t.Fatalf("want anomalous for any value above flat mean, got %s", r.status)
	}
}

func TestEvalZeroSpendDaysIncluded(t *testing.T) {
	// 15 days at 20 + 15 zero days (keys provided as "0"), day = 14.
	// mean = 10; variance = ((15*(20-10)^2)+(15*(0-10)^2))/30 = 100;
	// std = 10; threshold = 30. Day 14 -> normal.
	day := d("2026-03-15")
	earliest := d("2026-01-01")
	totals := map[string]decimal.Decimal{}
	for i := 1; i <= 30; i++ {
		bd := day.AddDate(0, 0, -i)
		v := "0"
		if i <= 15 {
			v = "20"
		}
		totals[bd.Format("2006-01-02")] = decimal.RequireFromString(v)
	}
	totals["2026-03-15"] = decimal.RequireFromString("14")

	r := computeEval(totals, day, earliest, true)
	if r.status != "normal" {
		t.Fatalf("want normal, got %s", r.status)
	}
	if r.mean.String() != "10" {
		t.Fatalf("mean want 10 got %s", r.mean)
	}
	if r.std.String() != "10" {
		t.Fatalf("std want 10 got %s", r.std)
	}

	// Same baseline but day = 30 exactly -> not anomalous (must exceed).
	totals["2026-03-15"] = decimal.RequireFromString("30")
	r = computeEval(totals, day, earliest, true)
	if r.status != "normal" {
		t.Fatalf("equality must be normal, got %s", r.status)
	}
	totals["2026-03-15"] = decimal.RequireFromString("30.01")
	r = computeEval(totals, day, earliest, true)
	if r.status != "anomalous" {
		t.Fatalf("30.01 > 30 must be anomalous, got %s", r.status)
	}
}

func TestEvalZerosOnlyBaseline(t *testing.T) {
	// 30 zero-spend days and a spend day -> anomalous.
	day := d("2026-03-15")
	earliest := d("2026-01-01")
	totals := constantBaseline("2026-03-15", "0", 30)
	totals["2026-03-15"] = decimal.RequireFromString("0.000001")
	r := computeEval(totals, day, earliest, true)
	if r.status != "anomalous" {
		t.Fatalf("spike above all-zero baseline must be anomalous, got %s", r.status)
	}
}

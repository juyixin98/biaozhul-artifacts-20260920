package stats

import (
	"testing"
	"time"

	"github.com/shopspring/decimal"
)

func day(n int) time.Time {
	return time.Date(2026, 1, n, 0, 0, 0, 0, time.UTC)
}

func seriesOf(v float64) map[time.Time]decimal.Decimal {
	m := map[time.Time]decimal.Decimal{}
	for i := 0; i < WindowDays; i++ {
		m[day(1+i)] = decimal.NewFromFloat(v)
	}
	return m
}

func TestBaselineWindowExcludesCurrentDay(t *testing.T) {
	from, to := BaselineWindow(day(31))
	if !from.Equal(day(1)) || !to.Equal(day(30)) {
		t.Fatalf("window = %s..%s want Jan1..Jan30", from, to)
	}
}

func TestInsufficientHistory(t *testing.T) {
	// Account created Jan 5: it did not exist for Jan1..Jan4, so the Jan31
	// baseline window is incomplete.
	r := Evaluate(seriesOf(10), day(5), day(31), decimal.NewFromInt(100))
	if r.Status != StatusInsufficientHistory {
		t.Fatalf("status = %s want insufficient_history", r.Status)
	}
}

func TestSufficientHistoryExistingAccountWithZeroDays(t *testing.T) {
	// Account existed since Jan1 even though it only spent on ten days; the
	// twenty zero days are valid baseline observations.
	m := map[time.Time]decimal.Decimal{}
	for i := 20; i < WindowDays; i++ {
		m[day(1+i)] = decimal.NewFromInt(300)
	}
	r := Evaluate(m, day(1), day(31), decimal.NewFromInt(301))
	if r.Status == StatusInsufficientHistory {
		t.Fatalf("existing account with zero days must be sufficient: %s", r.Status)
	}
	if !r.Mean.Equal(decimal.NewFromInt(100)) {
		t.Fatalf("mean = %s want 100 (zero days must count)", r.Mean)
	}
}

func TestZeroVarianceStrictGreater(t *testing.T) {
	m := seriesOf(100) // mean 100, std 0
	// Actual exactly equals mean+0 => normal (strictly greater required).
	if r := Evaluate(m, day(1), day(31), decimal.NewFromInt(100)); r.Status != StatusNormal {
		t.Fatalf("equal actual: %s want normal", r.Status)
	}
	// Actual one cent above => anomaly.
	if r := Evaluate(m, day(1), day(31), decimal.RequireFromString("100.01")); r.Status != StatusAnomaly {
		t.Fatalf("above actual: %s want anomaly", r.Status)
	}
}

func TestAnomalyMath(t *testing.T) {
	// 29 days of 100, one day of 160: mean=102, std = sqrt(variance).
	m := map[time.Time]decimal.Decimal{}
	for i := 0; i < WindowDays; i++ {
		v := int64(100)
		if i == 29 {
			v = 160
		}
		m[day(1+i)] = decimal.NewFromInt(v)
	}
	// variance = (29*2^2 + 58^2)/30 = (116+3364)/30 = 116; threshold = 102+2*sqrt(116) ~ 123.55
	small := decimal.RequireFromString("123")
	if r := Evaluate(m, day(1), day(31), small); r.Status != StatusNormal {
		t.Fatalf("123 vs threshold %s: %s", r.Threshold, r.Status)
	}
	big := decimal.RequireFromString("124")
	if r := Evaluate(m, day(1), day(31), big); r.Status != StatusAnomaly {
		t.Fatalf("124 vs threshold %s: %s", r.Threshold, r.Status)
	}
}

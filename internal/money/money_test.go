package money

import (
	"testing"

	"github.com/shopspring/decimal"
)

func TestParseAmount(t *testing.T) {
	ok := []string{"0", "0.000000", "12", "12.30", ".5", "10000000000000.999999"}
	for _, in := range ok {
		if _, err := ParseAmount(in); err != nil {
			t.Errorf("ParseAmount(%q) unexpected error: %v", in, err)
		}
	}
	bad := []string{
		"", "-1", "1e3", "1.2E2", "1.0000001", // too many fraction digits
		"100000000000000", "abc", "1,000.00", "nan", // non-canonical / huge
	}
	for _, in := range bad {
		if _, err := ParseAmount(in); err == nil {
			t.Errorf("ParseAmount(%q) expected error, got nil", in)
		}
	}
	d, err := ParseAmount("12.30")
	if err != nil {
		t.Fatal(err)
	}
	if d.String() != "12.3" {
		t.Errorf("canonical value = %s", d.String())
	}
}

func TestMeanStdIncludesZeroes(t *testing.T) {
	// Values: nine 100s followed by 21 zeros.
	vals := make([]decimal.Decimal, 30)
	mean := decimal.RequireFromString("30") // 900/30
	for i := 0; i < 30; i++ {
		if i < 9 {
			vals[i] = decimal.NewFromInt(100)
		}
	}
	m, s := MeanStd(vals)
	if !m.Equal(mean) {
		t.Fatalf("mean = %s want 30", m)
	}
	// population variance = (9*70^2 + 21*30^2)/30 = (44100+18900)/30 = 2100
	want := Sqrt(decimal.NewFromInt(2100))
	if s.Sub(want).Abs().GreaterThan(decimal.RequireFromString("0.000001")) {
		t.Fatalf("std = %s want %s", s, want)
	}
}

func TestZeroVariance(t *testing.T) {
	vals := make([]decimal.Decimal, 30)
	for i := range vals {
		vals[i] = decimal.NewFromInt(50)
	}
	m, s := MeanStd(vals)
	if !m.Equal(decimal.NewFromInt(50)) || !s.IsZero() {
		t.Fatalf("mean=%s std=%s want 50,0", m, s)
	}
}

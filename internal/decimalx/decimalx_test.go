package decimalx

import (
	"testing"

	"github.com/shopspring/decimal"
)

func TestSqrtKnownValues(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"0", "0"},
		{"1", "1"},
		{"4", "2"},
		{"100", "10"},
		{"2", "1.41421356"},
		{"0.25", "0.5"},
		{"1000000", "1000"},
	}
	for _, c := range cases {
		got := Sqrt(decimal.RequireFromString(c.in), 8).String()
		want := decimal.RequireFromString(c.want).String()
		if got != want {
			t.Errorf("sqrt(%s) = %s, want %s", c.in, got, want)
		}
	}
}

func TestSqrtSquaredMatches(t *testing.T) {
	// For a spread of values, sqrt(x)^2 must round-trip within 1e-5
	// (sqrt is computed to 8 fractional digits; the squared error scales as
	// 2*sqrt(x)*1e-8).
	tol, _ := decimal.NewFromString("0.00001")
	for _, s := range []string{"3.14159", "12345.6789", "0.000001", "99999999.25"} {
		x := decimal.RequireFromString(s)
		r := Sqrt(x, 8)
		back := r.Mul(r)
		diff := back.Sub(x).Abs()
		if diff.GreaterThan(tol) {
			t.Errorf("roundtrip %s -> %s -> %s diff %s", s, r, back, diff)
		}
	}
}

func TestSqrtNegativePanics(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("expected panic")
		}
	}()
	Sqrt(decimal.RequireFromString("-1"), 8)
}

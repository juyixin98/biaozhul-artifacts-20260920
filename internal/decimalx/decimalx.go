// Package decimalx bridges shopspring/decimal with pgx's NUMERIC type and
// provides an exact-decimal square root used by anomaly detection.
//
// All money math in CostLens goes through decimal (fixed point); float64 is
// never used for amounts or for the threshold comparison.
package decimalx

import (
	"errors"
	"math/big"

	"github.com/jackc/pgx/v5/pgtype"
	"github.com/shopspring/decimal"
)

// ToNumeric converts a decimal to a valid pgtype.Numeric.
func ToNumeric(d decimal.Decimal) pgtype.Numeric {
	var n pgtype.Numeric
	// Scan parses the canonical fixed-point string without float.
	if err := n.Scan(d.String()); err != nil {
		panic("decimalx: cannot scan decimal string: " + err.Error())
	}
	return n
}

// FromNumeric converts a pgtype.Numeric to a decimal. SQL NULL is reported as
// an error; callers reading nullable columns should check Valid first.
func FromNumeric(n pgtype.Numeric) (decimal.Decimal, error) {
	if !n.Valid {
		return decimal.Decimal{}, errors.New("decimalx: NULL numeric")
	}
	v, err := n.Value()
	if err != nil {
		return decimal.Decimal{}, err
	}
	s, ok := v.(string)
	if !ok {
		return decimal.Decimal{}, errors.New("decimalx: unexpected numeric value type")
	}
	return decimal.NewFromString(s)
}

// MustFromNumeric is FromNumeric that panics on error; used only where the
// column is NOT NULL.
func MustFromNumeric(n pgtype.Numeric) decimal.Decimal {
	d, err := FromNumeric(n)
	if err != nil {
		panic(err)
	}
	return d
}

// Sqrt returns the square root of x rounded to precision fractional digits.
// x must be non-negative. Computed by Newton iteration in big.Float, so the
// float64 seed affects only convergence speed, never the rounded result.
func Sqrt(x decimal.Decimal, precision int32) decimal.Decimal {
	if x.IsNegative() {
		panic("decimalx: sqrt of negative")
	}
	if x.IsZero() {
		return decimal.Zero
	}
	bits := uint(precision)*4 + 200
	xf, ok := new(big.Float).SetPrec(bits).SetString(x.String())
	if !ok {
		panic("decimalx: cannot convert to big.Float")
	}
	// Seed: scaled float64 sqrt (value roughly right; refined below).
	f64, _ := xf.Float64()
	z, _ := new(big.Float).SetPrec(bits).SetString(decimal.NewFromFloat(f64).String())
	if z == nil {
		z = big.NewFloat(1).SetPrec(bits)
	}

	two := big.NewFloat(2).SetPrec(bits)
	tol := new(big.Float).SetPrec(bits).SetFloat64(1)
	ten := big.NewFloat(10).SetPrec(bits)
	for i := int32(0); i < precision+20; i++ {
		tol.Quo(tol, ten)
	}
	for i := 0; i < 50; i++ {
		q := new(big.Float).SetPrec(bits).Quo(xf, z)   // x / z
		next := new(big.Float).SetPrec(bits).Add(z, q) // z + x/z
		next.Quo(next, two)                            // (z + x/z) / 2
		diff := new(big.Float).SetPrec(bits).Sub(next, z)
		diff.Abs(diff)
		z = next
		if diff.Cmp(tol) < 0 {
			break
		}
	}
	out := z.Text('f', int(precision)+4)
	d, err := decimal.NewFromString(out)
	if err != nil {
		panic("decimalx: bad sqrt output: " + out)
	}
	return d.Round(precision)
}

// Fixed renders a NUMERIC value with exactly places fractional digits.
// pgx's Numeric.Value() strips trailing zeros; columns with a declared scale
// (money is NUMERIC(20,6), baseline stats NUMERIC(20,8)) must keep theirs so
// API output and snapshots are stable (1 -> "1.000000").
func Fixed(n pgtype.Numeric, places int32) string {
	return MustFromNumeric(n).StringFixed(places)
}

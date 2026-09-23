package twap

import (
	"math/big"
	"strings"
)

var (
	bigTen = big.NewInt(10)
)

// ratToFixed renders num/den as a fixed-point decimal with `digits` fractional
// digits, rounded half-away-from-zero. den must be non-zero.
func ratToFixed(num, den *big.Int, digits int) string {
	if den.Sign() == 0 {
		return ""
	}
	neg := num.Sign() < 0
	n := new(big.Int).Abs(num)

	scale := new(big.Int).Exp(bigTen, big.NewInt(int64(digits)), nil)
	scaled := new(big.Int).Mul(n, scale)
	q, r := new(big.Int).QuoRem(scaled, den, new(big.Int))
	// Half-away-from-zero: round up when 2*remainder >= denominator.
	if new(big.Int).Mul(r, big.NewInt(2)).Cmp(den) >= 0 {
		q.Add(q, big.NewInt(1))
	}

	var s string
	if digits == 0 {
		s = q.String()
	} else {
		intPart := q.String()
		if len(intPart) <= digits {
			intPart = strings.Repeat("0", digits+1-len(intPart)) + intPart
		}
		s = intPart[:len(intPart)-digits] + "." + intPart[len(intPart)-digits:]
	}
	if neg && q.Sign() != 0 {
		s = "-" + s
	}
	return s
}

// coverageString renders covered/total as a decimal in [0,1] with 6 digits.
func coverageString(covered, total int64) string {
	if total <= 0 {
		return "0.000000"
	}
	s := ratToFixed(big.NewInt(covered), big.NewInt(total), 6)
	// Defensive clamp (covered cannot exceed total by construction).
	if s == "" {
		return "0.000000"
	}
	return s
}

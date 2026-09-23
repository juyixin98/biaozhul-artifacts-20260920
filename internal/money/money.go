// Package money enforces exact fixed-point money semantics.
//
// All amounts in CostLens are NUMERIC(20,6): at most 14 integer digits and at
// most 6 decimal places. Floating point is never used to parse, store or
// compute money.
package money

import (
	"fmt"
	"regexp"
	"strings"

	"github.com/shopspring/decimal"
)

const (
	IntegerDigits = 14
	Scale         = 6
)

func init() {
	decimal.DivisionPrecision = 32
}

var currencyRe = regexp.MustCompile(`^[A-Z]{3}$`)

// ErrInvalidAmount is returned when text cannot be represented as NUMERIC(20,6).
type ErrInvalidAmount struct{ Reason string }

func (e *ErrInvalidAmount) Error() string { return "invalid amount: " + e.Reason }

// ParseAmount parses a canonical decimal amount.
//
// Accepted forms: sign? digits ("12"), sign? digits "." digits ("12.30"),
// a leading-dot fraction (".5"). Scientific notation is rejected: an exponent
// silently changes scale and is not an exact fixed-point literal. More than
// Scale decimal places or IntegerDigits integer digits is rejected; amounts
// must be non-negative.
func ParseAmount(s string) (decimal.Decimal, error) {
	if s == "" {
		return decimal.Decimal{}, &ErrInvalidAmount{"empty"}
	}
	for _, ch := range s {
		if ch == 'e' || ch == 'E' {
			return decimal.Decimal{}, &ErrInvalidAmount{"scientific notation is not allowed: " + s}
		}
	}
	d, err := decimal.NewFromString(s)
	if err != nil {
		return decimal.Decimal{}, &ErrInvalidAmount{err.Error()}
	}
	if intDigits(d) > IntegerDigits {
		return decimal.Decimal{}, &ErrInvalidAmount{"more than 14 integer digits: " + s}
	}
	if fracDigits(d) > Scale {
		return decimal.Decimal{}, &ErrInvalidAmount{"more than 6 decimal places: " + s}
	}
	if d.Sign() < 0 {
		return decimal.Decimal{}, &ErrInvalidAmount{"amount must not be negative: " + s}
	}
	return d, nil
}

func intDigits(d decimal.Decimal) int {
	if d.IsZero() {
		return 1
	}
	s := d.Abs().String()
	if i := strings.IndexByte(s, '.'); i >= 0 {
		s = s[:i]
	}
	s = strings.TrimLeft(s, "0")
	if s == "" {
		return 1
	}
	return len(s)
}

func fracDigits(d decimal.Decimal) int {
	exp := d.Exponent()
	if exp >= 0 {
		return 0
	}
	return int(-exp)
}

// FracDigitsOf reports the number of digits after the decimal point.
func FracDigitsOf(d decimal.Decimal) int { return fracDigits(d) }

// ValidateCurrency checks ISO-4217-style three uppercase letters.
func ValidateCurrency(c string) error {
	if !currencyRe.MatchString(c) {
		return fmt.Errorf("currency must be 3 uppercase letters (ISO 4217), got %q", c)
	}
	return nil
}

// MeanStd returns the population mean and population standard deviation of
// vals. Callers must include zero-spend days explicitly in vals.
func MeanStd(vals []decimal.Decimal) (decimal.Decimal, decimal.Decimal) {
	n := decimal.NewFromInt(int64(len(vals)))
	sum := decimal.Zero
	for _, v := range vals {
		sum = sum.Add(v)
	}
	mean := sum.Div(n)
	sq := decimal.Zero
	for _, v := range vals {
		d := v.Sub(mean)
		sq = sq.Add(d.Mul(d))
	}
	variance := sq.Div(n)
	return mean, Sqrt(variance)
}

// Sqrt computes the square root of a non-negative decimal to 20 decimal places
// using Newton-Raphson entirely in fixed-point decimal arithmetic.
func Sqrt(x decimal.Decimal) decimal.Decimal {
	const prec = 20
	if x.IsZero() {
		return decimal.Zero
	}
	if x.Sign() < 0 {
		panic("Sqrt of negative decimal")
	}
	guess := x.Div(decimal.NewFromInt(2))
	two := decimal.NewFromInt(2)
	eps := decimal.New(1, -prec)
	for i := 0; i < 200; i++ {
		next := guess.Add(x.Div(guess)).Div(two)
		if next.Sub(guess).Abs().LessThan(eps) {
			return next
		}
		guess = next
	}
	return guess
}

// RoundStorage rounds a derived value to the storage scale (6 places, half-up).
func RoundStorage(d decimal.Decimal) decimal.Decimal { return d.Round(Scale) }

// Package money implements integer-cent arithmetic and the documented fee rules.
package money

import "fmt"

// Cents is the single money type used everywhere. Never float.
type Cents int64

func (c Cents) String() string {
	neg := c < 0
	if neg {
		c = -c
	}
	s := fmt.Sprintf("%s%d.%02d", sign(neg), c/100, c%100)
	return s
}

func sign(neg bool) string {
	if neg {
		return "-"
	}
	return ""
}

// Fee computes the default processing fee: fee = roundHalfUp(amount*bps/10000) + fixed.
// bps is basis points (290 == 2.9%). The proportional part uses ROUND HALF UP
// (ties go away from zero), so the boundary where amount*bps lands on exactly
// x.5 cents always rounds up. The fixed component is added afterwards and is
// never itself rounded.
func Fee(amount Cents, bps int, fixed Cents) Cents {
	if amount <= 0 {
		return 0
	}
	return Cents(mulDiv(int64(amount), int64(bps), 10000)) + fixed
}

// RefundFee returns the slice of the ORIGINAL capture fee that is returned with
// a refund, when alreadyRefundedFee/remainingRefundedFee are tracked by caller.
//
// Rule: refunds give back a proportional share of the original fee, half-up
// rounded, BUT the running total of returned fees may never (a) exceed the
// original fee, nor (b) make the *remaining* fee pool negative even after
// rounding. The final refund of a payment returns exactly the fee remainder so
// the books close to zero with no accumulated rounding residue.
//
//	feeRefund = roundHalfUp(originalFee * refundAmount / capturedAmount)
//
// Callers clamp: if this is the last refund (refundAmount == remaining gross),
// feeRefund = originalFee - alreadyRefundedFee exactly.
func RefundFee(originalFee Cents, refundAmount, capturedAmount Cents, alreadyRefundedFee Cents, last bool) Cents {
	if last {
		return originalFee - alreadyRefundedFee
	}
	d := Cents(mulDiv(int64(originalFee), int64(refundAmount), int64(capturedAmount)))
	rem := originalFee - alreadyRefundedFee
	if d > rem {
		d = rem
	}
	if d < 0 {
		d = 0
	}
	return d
}

// mulDiv computes roundHalfUp(a*b/d) for positive operands.
func mulDiv(a, b, d int64) int64 {
	if a == 0 || b == 0 {
		return 0
	}
	neg := (a < 0) != (b < 0)
	if a < 0 {
		a = -a
	}
	if b < 0 {
		b = -b
	}
	q := (a * b) / d
	r := (a * b) % d
	if r*2 >= d { // tie => up
		q++
	}
	if neg {
		return -q
	}
	return q
}

// Validate a fee schedule.
func ValidFeeSchedule(bps int, fixed Cents) error {
	if bps < 0 || bps > 10000 {
		return fmt.Errorf("fee bps must be in [0,10000], got %d", bps)
	}
	if fixed < 0 {
		return fmt.Errorf("fixed fee must be >= 0, got %d", fixed)
	}
	return nil
}

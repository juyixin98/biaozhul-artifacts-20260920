package budget

import "math/bits"

// All token quantities are integers. Fractional rates are handled by carrying
// sub-token remainder units measured on a common nanosecond scale:
//
//	1 token = scale nanoseconds of "token time"
//
// A refill of `num` tokens per `den` nanoseconds produces `num*scale` scaled
// units per `den` ns; Frac stores the sub-token remainder in [0, scale).
// Choosing scale == 1e9 keeps the remainder on a nanosecond axis (Frac is the
// number of nanoseconds of refill still needed to mint one whole token),
// which makes event payloads easy to read.
const scale = int64(1_000_000_000)

// u128 is a little-endian 128-bit unsigned integer: lo = low 64 bits.
type u128 struct {
	hi, lo uint64
}

func mul64(a, b uint64) u128 {
	hi, lo := bits.Mul64(a, b)
	return u128{hi: hi, lo: lo}
}

func (a u128) sub(b u128) u128 {
	lo, borrow := bits.Sub64(a.lo, b.lo, 0)
	hi, _ := bits.Sub64(a.hi, b.hi, borrow)
	return u128{hi: hi, lo: lo}
}

func (a u128) cmp(b u128) int {
	switch {
	case a.hi < b.hi:
		return -1
	case a.hi > b.hi:
		return 1
	case a.lo < b.lo:
		return -1
	case a.lo > b.lo:
		return 1
	default:
		return 0
	}
}

// div128by64 computes (hi:lo)/d, returning quotient's two halves and the
// remainder. d must be nonzero. Callers must be prepared for qhi != 0.
func div128by64(hi, lo, d uint64) (qhi, qlo, rem uint64) {
	if hi < d {
		// Quotient fits in 64 bits: single hardware divide.
		qlo, rem = bits.Div64(hi, lo, d)
		return 0, qlo, rem
	}
	// Two-limb long division:
	//	q1, r1 = hi / d            (r1 < d)
	//	q0, r  = (r1 : lo) / d     (valid since r1 < d)
	q1, r1 := bits.Div64(0, hi, d)
	q0, r := bits.Div64(r1, lo, d)
	return q1, q0, r
}

// div128by64Ceil is ceil((hi:lo)/d) returned as a 128-bit quotient.
func div128by64Ceil(hi, lo, d uint64) (q u128, rem uint64) {
	qhi, qlo, r := div128by64(hi, lo, d)
	q = u128{hi: qhi, lo: qlo}
	rem = r
	if r != 0 {
		q.lo++
		if q.lo == 0 {
			q.hi++
		}
	}
	return q, rem
}

// toInt64Clamped converts a 128-bit value to int64, saturating at MaxInt64.
func (a u128) toInt64Clamped() int64 {
	const maxI64 = uint64(1<<63 - 1)
	if a.hi != 0 || a.lo > maxI64 {
		return 1<<63 - 1
	}
	return int64(a.lo)
}

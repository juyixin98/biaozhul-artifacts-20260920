// Package u128 provides the small set of exact 128-bit unsigned integer
// operations the token bucket needs to avoid overflows when multiplying
// rates by elapsed time.
package u128

import "math/bits"

// U128 is a 128-bit unsigned integer split into 64-bit halves.
type U128 struct {
	Hi uint64
	Lo uint64
}

// From64 widens a uint64.
func From64(x uint64) U128 { return U128{Lo: x} }

// Mul64 returns the 128-bit product of two uint64 values.
func Mul64(a, b uint64) U128 {
	hi, lo := bits.Mul64(a, b)
	return U128{Hi: hi, Lo: lo}
}

// MulU64 returns x * y. It panics if the full product exceeds 128 bits.
//
// x.Lo*y contributes to bits 0..127 and x.Hi*y to bits 64..191, so the
// 64..127 lane is the sum of the former's high half and the latter's low
// half; overflow beyond bit 128 occurs only if the latter product itself
// spans more than 64 bits or that lane sum carries.
func MulU64(x U128, y uint64) U128 {
	lowHi, lo := bits.Mul64(x.Lo, y)
	highHi, highLo := bits.Mul64(x.Hi, y)
	hi, carry := bits.Add64(lowHi, highLo, 0)
	if highHi != 0 || carry != 0 {
		panic("u128: multiplication overflow")
	}
	return U128{Hi: hi, Lo: lo}
}

// AddU64 returns x + y, panicking on 128-bit overflow.
func AddU64(x U128, y uint64) U128 {
	lo := x.Lo + y
	hi := x.Hi
	if lo < x.Lo {
		hi++
		if hi == 0 {
			panic("u128: addition overflow")
		}
	}
	return U128{Hi: hi, Lo: lo}
}

// SubU64 returns x - y, panicking on underflow.
func SubU64(x U128, y uint64) U128 {
	lo, borrow := bits.Sub64(x.Lo, y, 0)
	hi, borrow := bits.Sub64(x.Hi, 0, borrow)
	if borrow != 0 {
		panic("u128: subtraction underflow")
	}
	return U128{Hi: hi, Lo: lo}
}

// IsZero reports whether x == 0.
func (x U128) IsZero() bool { return x.Hi == 0 && x.Lo == 0 }

// FitsInt64 reports whether x fits in a non-negative int64.
func (x U128) FitsInt64() bool {
	const sign = uint64(1) << 63
	return x.Hi == 0 && x.Lo < sign
}

// DivU64 returns quotient and remainder of x / y (y must be non-zero).
//
// It is a two-limb schoolbook division. Each step calls bits.Div64 with the
// running remainder as the high input; the remainder is always strictly less
// than y, which is exactly bits.Div64's precondition, and the high-limb step
// starts from hi=0 so its quotient fits 64 bits even when x.Hi >= y.
func DivU64(x U128, y uint64) (q U128, r U128) {
	if y == 0 {
		panic("u128: divide by zero")
	}
	if x.Hi < y {
		// Fast path.
		qlo, rem := bits.Div64(x.Hi, x.Lo, y)
		return U128{Lo: qlo}, U128{Lo: rem}
	}
	qHi, remHi := bits.Div64(0, x.Hi, y)
	qLo, remLo := bits.Div64(remHi, x.Lo, y)
	return U128{Hi: qHi, Lo: qLo}, U128{Lo: remLo}
}

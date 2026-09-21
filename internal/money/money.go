// Package money holds the domain-wide money rules for ClearSettle.
//
// All amounts are integer cents (int64). No float is used anywhere.
package money

// Fee defaults: 2.9% + 30 cents of the captured amount.
const (
	DefaultFeeBPS        = 290
	DefaultFeeFixedCents = 30
	BPSBase              = 10000
)

// Fee computes the processing fee for gross cents using
//
//	fee = gross * bps / 10000 + fixed
//
// Rounding is half-up (commercial rounding): when the discarded fraction is
// exactly half a cent, the result rounds up. Integer-only arithmetic keeps
// this deterministic.
func Fee(gross int64, bps int, fixed int64) int64 {
	num := gross * int64(bps)
	// half-up division: add (base-1)/2 before truncating.
	return (num+BPSBase/2)/BPSBase + fixed
}

// FeeRefund computes how much of the original fee must be released when a
// cumulative refunded total of refundedCents is in effect on capturedCents.
//
// Rule: the $0.30 fixed portion is non-refundable (it covers acquirer costs);
// the proportional 2.9% portion is refunded pro-rata. The released amount is
// recomputed cumulatively:
//
//	released(totalRefunded) = feeOn(captured) - feeOn(captured - totalRefunded)
//
// Callers must compute the per-refund delta themselves (see RefundFeeDelta).
// This keeps partial refunds independent of the order they are issued in and
// guarantees the sum of released fees can never exceed the proportional part.
func FeeRefund(captured, totalRefunded int64, bps int) int64 {
	if totalRefunded <= 0 || captured <= 0 {
		return 0
	}
	if totalRefunded > captured {
		totalRefunded = captured
	}
	return proportionalFee(captured, bps) - proportionalFee(captured-totalRefunded, bps)
}

// RefundFeeDelta returns the fee released by one additional partial refund.
//
//	previousTotal : cumulative refunded cents *before* this refund
//	thisRefund    : cents refunded by this request
func RefundFeeDelta(captured, previousTotal, thisRefund int64, bps int) int64 {
	return FeeRefund(captured, previousTotal+thisRefund, bps) -
		FeeRefund(captured, previousTotal, bps)
}

func proportionalFee(gross int64, bps int) int64 {
	num := gross * int64(bps)
	return (num + BPSBase/2) / BPSBase
}

// NetForMerchant is what the merchant actually receives for a capture:
// gross minus fee.
func NetForMerchant(gross int64, bps int, fixed int64) int64 {
	return gross - Fee(gross, bps, fixed)
}

// Abs returns |x|.
func Abs(x int64) int64 {
	if x < 0 {
		return -x
	}
	return x
}

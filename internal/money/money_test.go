package money

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestFeeHalfUp(t *testing.T) {
	// Default schedule 2.9% + 30 cents.
	cases := []struct {
		gross int64
		want  int64
	}{
		{0, 30},      // fixed still charged (captures of 0 are rejected upstream)
		{100, 33},    // 2.9 + 30 = 32.9 -> half-up 33
		{172, 35},    // 4.988 + 30 = 34.988 -> 35
		{173, 35},    // 5.017 + 30 = 35.017 -> 35
		{500, 45},    // 14.5 + 30 = 44.5 -> half-up 45
		{1000, 59},   // 29 + 30
		{499, 44},    // 14.471 + 30 = 44.471 -> 44
		{10000, 320}, // 290 + 30
		{33333, 997}, // 966.657 + 30 = 996.657 -> 997
	}
	for _, tc := range cases {
		got := Fee(tc.gross, DefaultFeeBPS, DefaultFeeFixedCents)
		assert.Equal(t, tc.want, got, "gross=%d", tc.gross)
	}
}

func TestFeeRoundingHalfUpExactHalf(t *testing.T) {
	// Find a gross where 290*gross/10000 lands on exactly x.5:
	// 290*g = 5000k + 5000 -> g = (5000k+5000)/290 = 500(k+1)/29.
	// k+1=29 -> g = 500. So 290*500/10000 = 14.5 exactly -> must round UP to 15.
	assert.Equal(t, int64(15), (int64(500)*DefaultFeeBPS+BPSBase/2)/BPSBase)
}

func TestRefundFeeIsProportionalAndOrderIndependent(t *testing.T) {
	const captured int64 = 10000 // fee proportional part = 290
	// Two partial refunds of 3000 + 7000 (full).
	d1 := RefundFeeDelta(captured, 0, 3000, DefaultFeeBPS)
	d2 := RefundFeeDelta(captured, 3000, 7000, DefaultFeeBPS)
	// Reversed order: 7000 then 3000.
	e1 := RefundFeeDelta(captured, 0, 7000, DefaultFeeBPS)
	e2 := RefundFeeDelta(captured, 7000, 3000, DefaultFeeBPS)
	assert.Equal(t, d1+d2, e1+e2, "total fee refund independent of order")
	// Full refund releases exactly the proportional (2.9%) part, never the 30c.
	assert.Equal(t, int64(290), d1+d2)
	assert.Equal(t, int64(290), FeeRefund(captured, captured, DefaultFeeBPS))
	// Fixed $0.30 is retained even on a full refund.
	assert.Less(t, d1+d2, Fee(captured, DefaultFeeBPS, DefaultFeeFixedCents))
}

func TestRefundFeeNeverExceedsProportional(t *testing.T) {
	for _, captured := range []int64{1, 2, 99, 100, 101, 9999, 10000, 1234567} {
		full := FeeRefund(captured, captured, DefaultFeeBPS)
		prop := (captured*DefaultFeeBPS + BPSBase/2) / BPSBase
		assert.LessOrEqual(t, full, prop, "captured=%d", captured)
	}
}

func TestPartialRefundCumulativeMonotonic(t *testing.T) {
	const captured int64 = 5000
	var prevTotal int64
	for _, amt := range []int64{1000, 1000, 1500, 1500} {
		d := RefundFeeDelta(captured, prevTotal, amt, DefaultFeeBPS)
		assert.GreaterOrEqual(t, d, int64(0))
		prevTotal += amt
	}
	assert.Equal(t, prevTotal, captured)
	assert.Equal(t, int64(145), FeeRefund(captured, captured, DefaultFeeBPS)) // 2.9% of 5000
}

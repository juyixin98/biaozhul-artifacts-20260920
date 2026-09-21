package money

import "testing"

func TestFee(t *testing.T) {
	cases := []struct {
		name    string
		amount  Cents
		bps     int
		fixed   Cents
		wantFee Cents
	}{
		// Default schedule 2.9% + 30 cents, proportional part rounded half up.
		{"zero", 0, 290, 30, 0},
		{"1 dollar", 100, 290, 30, 33},              // 2.9 -> 3, +30
		{"10 dollars", 1000, 290, 30, 59},           // 29 + 30
		{"100 dollars", 10000, 290, 30, 320},        // 290 + 30
		{"stripe-classic 31.5", 3150, 290, 30, 121}, // 91.35 -> 91 +30
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := Fee(tc.amount, tc.bps, tc.fixed)
			if got != tc.wantFee {
				t.Fatalf("Fee(%d,%d,%d)=%d want %d", tc.amount, tc.bps, tc.fixed, got, tc.wantFee)
			}
		})
	}
}

// TestFeeRoundingBoundaries pins the half-up rule at exact 0.5-cent ties.
func TestFeeRoundingBoundaries(t *testing.T) {
	// Choose amounts where amount*290 mod 10000 == 5000 (exact half-cent tie).
	// 5000/290 is not integral; the smallest solution to 290*a ≡ 5000 (mod 10000):
	// gcd(290,10000)=10, and 5000 is divisible by 10, so solutions exist.
	// a = (5000/10) * inv(29 mod 1000) mod 1000. 29*?≡1 mod 1000; inverse is 569 (29*569=16501).
	// a0 = 500*569 mod 1000 = 500; check 290*500=145000 -> 14.5 cents exact tie -> rounds to 15.
	got := Fee(500, 290, 0)
	if got != 15 {
		t.Fatalf("half-cent tie at amount=500: got fee %d, want 15 (half up)", got)
	}
	// One cent below the tie boundary: 290*499=144710 -> 14.471 -> 14.
	if got := Fee(499, 290, 0); got != 14 {
		t.Fatalf("amount=499: got %d want 14", got)
	}
	// One cent above: 290*501=145290 -> 14.529 -> 15.
	if got := Fee(501, 290, 0); got != 15 {
		t.Fatalf("amount=501: got %d want 15", got)
	}
}

func TestFeeNegativeRejected(t *testing.T) {
	if err := ValidFeeSchedule(290, -1); err == nil {
		t.Fatal("negative fixed fee must be rejected")
	}
	if err := ValidFeeSchedule(10001, 0); err == nil {
		t.Fatal("bps above 10000 must be rejected")
	}
}

// TestRefundFeeProportional verifies proportional fee return and exact closure
// on the final refund (no rounding residue).
func TestRefundFeeProportional(t *testing.T) {
	const captured Cents = 10000
	fee := Fee(captured, 290, 30) // 320
	if fee != 320 {
		t.Fatalf("setup fee=%d want 320", fee)
	}

	// Three partial refunds that together return the full amount:
	parts := []Cents{3333, 3333, 3334}
	var refundedGross, refundedFee Cents
	for i, part := range parts {
		remaining := captured - refundedGross
		last := part == remaining
		fr := RefundFee(fee, part, captured, refundedFee, last)
		refundedGross += part
		refundedFee += fr
		t.Logf("refund %d amount=%d feeBack=%d cumulativeFee=%d", i+1, part, fr, refundedFee)
	}
	if refundedGross != captured {
		t.Fatalf("gross refunded=%d want %d", refundedGross, captured)
	}
	if refundedFee != fee {
		t.Fatalf("cumulative fee refunded=%d want exactly original fee %d (no residue)", refundedFee, fee)
	}
}

// TestRefundFeeNeverExceedsOriginal ensures rounding can't over-return fees.
func TestRefundFeeNeverExceedsOriginal(t *testing.T) {
	fee := Fee(100, 290, 30) // 33
	// Refund 60 then 40 cents.
	fr1 := RefundFee(fee, 60, 100, 0, false)
	fr2 := RefundFee(fee, 40, 100, fr1, true) // final: exact remainder
	if fr1+fr2 != fee {
		t.Fatalf("fee back %d+%d=%d want %d", fr1, fr2, fr1+fr2, fee)
	}
	if fr1 > fee {
		t.Fatalf("single partial fee refund %d exceeds fee %d", fr1, fee)
	}
}

func TestCentsString(t *testing.T) {
	cases := map[Cents]string{
		0: "0.00", 5: "0.05", -5: "-0.05", 1234: "12.34", -1234: "-12.34", 100: "1.00",
	}
	for in, want := range cases {
		if got := in.String(); got != want {
			t.Errorf("%d String()=%q want %q", in, got, want)
		}
	}
}

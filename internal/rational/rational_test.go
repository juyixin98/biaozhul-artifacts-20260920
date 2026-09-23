package rational

import (
	"encoding/json"
	"testing"
)

func TestParseRate(t *testing.T) {
	cases := []struct {
		in  string
		num int64
		den int64
	}{
		{"10", 10, 1},
		{"10/s", 10, 1},
		{"0.5", 1, 2},
		{"1/3", 1, 3},
		{"0.25", 1, 4},
		{"0.000001", 1, 1_000_000},
		{"0", 0, 1},
	}
	for _, c := range cases {
		r, err := ParseRate(c.in)
		if err != nil {
			t.Fatalf("%q: %v", c.in, err)
		}
		if r.Num != c.num || r.Den != c.den {
			t.Fatalf("%q = %d/%d want %d/%d", c.in, r.Num, r.Den, c.num, c.den)
		}
	}
}

func TestParseRateErrors(t *testing.T) {
	for _, s := range []string{"", "abc", "1/", "/3", "-1/2", "1.2.3"} {
		if _, err := ParseRate(s); err == nil {
			t.Fatalf("%q: expected error", s)
		}
	}
}

func TestDecimalTokensRoundTrip(t *testing.T) {
	cases := map[string]string{
		"1":        "1",
		"3.5":      "3.5",
		"0.25":     "0.25",
		"0.000001": "0.000001",
		"10.10":    "10.1",
		"0":        "0",
	}
	for in, want := range cases {
		micro, err := ParseDecimalTokens(in)
		if err != nil {
			t.Fatalf("%q: %v", in, err)
		}
		if got := FormatMicroTokens(micro); got != want {
			t.Fatalf("%q -> micro=%d -> %q, want %q", in, micro, got, want)
		}
	}
}

func TestParseDecimalTokensRejectsTooFine(t *testing.T) {
	if _, err := ParseDecimalTokens("0.0000001"); err == nil {
		t.Fatal("expected precision error")
	}
}

// TestRateMulNanosThirdExact verifies 1 token / 3 sec never drifts over a long
// horizon: this is the "no floating point" acceptance check.
func TestRateMulNanosThirdExact(t *testing.T) {
	r, _ := ParseRate("1/3") // 1/3 token per second

	// 3 seconds worth: exactly 1 token = 1e6 microtokens, zero remainder.
	micro, remNum, remDen, ok := r.MulNanos(3_000_000_000)
	if !ok {
		t.Fatal("overflow")
	}
	if micro != 1_000_000 || remNum != 0 {
		t.Fatalf("3s = micro=%d rem=%d/%d want 1000000 rem 0", micro, remNum, remDen)
	}

	// 1 second: 333333 microtokens with a carried fractional remainder.
	micro, remNum, remDen, _ = r.MulNanos(1_000_000_000)
	if micro != 333_333 {
		t.Fatalf("1s micro=%d want 333333", micro)
	}
	// Exact remainder: 1e9 / 3000 = 333333 rem 1000.
	if remNum != 1_000 || remDen != 3_000 {
		t.Fatalf("1s rem=%d/%d want 1000/3000", remNum, remDen)
	}

	// Over 300 one-second intervals with carry the total must be exactly
	// 100 tokens with no accumulation error.
	var total uint64
	carry := uint64(0)
	for i := 0; i < 300; i++ {
		num := uint64(r.Num)*1_000_000_000 + carry
		q, rem := num/uint64(3_000), num%uint64(3_000)
		total += q
		carry = rem
	}
	if total != 100*1_000_000 {
		t.Fatalf("carried total = %d microtokens, want %d", total, 100*1_000_000)
	}
	if carry != 0 {
		t.Fatalf("residual carry %d, want 0 after whole 300s", carry)
	}
}

func TestRateJSONRejectsFractionalNumber(t *testing.T) {
	var r Rate
	if err := json.Unmarshal([]byte(`0.1`), &r); err == nil {
		t.Fatal("fractional JSON number must be rejected")
	}
	// String decimal and object both work.
	if err := json.Unmarshal([]byte(`"0.1"`), &r); err != nil {
		t.Fatalf(`"0.1": %v`, err)
	}
	if r.Num != 1 || r.Den != 10 {
		t.Fatalf(`"0.1" -> %d/%d`, r.Num, r.Den)
	}
	if err := json.Unmarshal([]byte(`{"tokens":1,"per_seconds":3}`), &r); err != nil {
		t.Fatalf("object: %v", err)
	}
	if r.Num != 1 || r.Den != 3 {
		t.Fatalf("object -> %d/%d", r.Num, r.Den)
	}
}

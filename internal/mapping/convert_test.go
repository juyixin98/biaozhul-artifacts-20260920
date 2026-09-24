package mapping

import (
	"errors"
	"math"
	"testing"
)

func TestConvertInt_LinearExact(t *testing.T) {
	cases := []struct {
		name     string
		in       int64
		num, den int64
		mode     RoundingPolicy
		want     int64
	}{
		{"ms to ns", 1_727_000_000_123, 1_000_000, 1, RoundingReject, 1_727_000_000_123_000_000},
		{"ns to ms exact", 1_727_000_000_123_000_000, 1, 1_000_000, RoundingReject, 1_727_000_000_123},
		{"zero", 0, 1000, 1, RoundingReject, 0},
		{"nearest down", 294_400_499 - 273_150_000, 1, 1000, RoundingNearest, 21_250}, // 21250.499 -> 21250
		{"nearest half up", 21_250_500, 1, 1000, RoundingNearest, 21_251},
		{"floor positive", 21_250_999, 1, 1000, RoundingFloor, 21_250},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ConvertInt(tc.in, tc.num, tc.den, 0, 0, tc.mode, "x")
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tc.want {
				t.Fatalf("got %d, want %d", got, tc.want)
			}
		})
	}
}

func TestConvertInt_AffineCelsiusKelvin(t *testing.T) {
	// v1 milli-C -> v2 micro-K: uk = mc*1000 + 273_150_000
	got, err := ConvertInt(21_250, 1000, 1, 0, 273_150_000, RoundingReject, "temp_milli_c")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if want := int64(294_400_000); got != want {
		t.Fatalf("got %d, want %d (21.25°C = 294.4K)", got, want)
	}

	// negative temperature: -40.0°C milli-C -> 233.15K micro-K
	got, err = ConvertInt(-40_000, 1000, 1, 0, 273_150_000, RoundingReject, "temp_milli_c")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if want := int64(233_150_000); got != want {
		t.Fatalf("got %d, want %d", got, want)
	}

	// reverse: (uk-273_150_000)/1000
	got, err = ConvertInt(233_150_000, 1, 1000, -273_150_000, 0, RoundingReject, "temperature_uk")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if want := int64(-40_000); got != want {
		t.Fatalf("got %d, want %d", got, want)
	}
}

func TestConvertInt_RoundingRejects(t *testing.T) {
	_, err := ConvertInt(1_727_000_000_123_456_789, 1, 1_000_000, 0, 0, RoundingReject, "observed_at_ns")
	var f *Failure
	if !errors.As(err, &f) || f.Code != FailRoundingRequired {
		t.Fatalf("expected ROUNDING_REQUIRED failure, got %v", err)
	}
	if f.Field != "observed_at_ns" {
		t.Fatalf("error not locatable: %+v", f)
	}
}

func TestConvertInt_NegativeRounding(t *testing.T) {
	// -21250.5 micro offset units /1000: nearest half-away -> -21251, floor -> -21251, truncate would be -21250
	got, err := ConvertInt(-21_250_500, 1, 1000, 0, 0, RoundingNearest, "x")
	if err != nil || got != -21_251 {
		t.Fatalf("nearest: got %d err %v, want -21251", got, err)
	}
	got, err = ConvertInt(-21_250_499, 1, 1000, 0, 0, RoundingNearest, "x")
	if err != nil || got != -21_250 {
		t.Fatalf("nearest: got %d err %v, want -21250", got, err)
	}
	got, err = ConvertInt(-21_250_001, 1, 1000, 0, 0, RoundingFloor, "x")
	if err != nil || got != -21_251 {
		t.Fatalf("floor: got %d err %v, want -21251", got, err)
	}
}

func TestConvertInt_Overflow(t *testing.T) {
	// math.MaxInt64 ms -> ns overflows.
	_, err := ConvertInt(math.MaxInt64, 1_000_000, 1, 0, 0, RoundingReject, "timestamp_ms")
	var f *Failure
	if !errors.As(err, &f) || f.Code != FailIntegerOverflow {
		t.Fatalf("expected INTEGER_OVERFLOW, got %v", err)
	}
	// Post-add overflow: 1*1000 + MaxInt64 exceeds int64.
	_, err = ConvertInt(1, 1000, 1, 0, math.MaxInt64, RoundingReject, "temp")
	if !errors.As(err, &f) || f.Code != FailIntegerOverflow {
		t.Fatalf("expected INTEGER_OVERFLOW on post_add, got %v", err)
	}
	// Pre-add overflow: MinInt64 - 1 at the affine input stage.
	_, err = ConvertInt(math.MinInt64, 1, 1, -1, 0, RoundingReject, "temp")
	if !errors.As(err, &f) || f.Code != FailIntegerOverflow {
		t.Fatalf("expected INTEGER_OVERFLOW on pre_add, got %v", err)
	}
}

func TestMapEnum_ErrorCarriesOriginal(t *testing.T) {
	m := &EnumMapping{From: "phase", OnMissing: EnumPolicyError, Values: map[int64]int64{0: 0, 1: 1}}
	out, carried, err := MapEnum(m, 4, "trace-x")
	if err == nil {
		t.Fatal("expected error for unmapped enum")
	}
	var f *Failure
	if !errors.As(err, &f) {
		t.Fatalf("wrong error type: %v", err)
	}
	if f.Code != FailEnumOverflow || f.OriginalEnum == nil || *f.OriginalEnum != 4 {
		t.Fatalf("failure must carry original value 4, got %+v", f)
	}
	if out != 0 || carried != nil {
		t.Fatalf("no output on error path, got out=%d carried=%v", out, carried)
	}
}

func TestMapEnum_CarryDoesNotSilentlyZero(t *testing.T) {
	m := &EnumMapping{From: "phase", OnMissing: EnumPolicyCarry, Values: map[int64]int64{0: 0, 1: 1}}
	out, carried, err := MapEnum(m, 4, "trace-x")
	if err != nil {
		t.Fatalf("carry policy must not error, got %v", err)
	}
	if out != 0 || carried == nil || *carried != 4 {
		t.Fatalf("expected zero target with carried original 4, got out=%d carried=%v", out, carried)
	}
	// A mapped value returns no carry marker at all.
	out, carried, err = MapEnum(m, 1, "trace-x")
	if err != nil || out != 1 || carried != nil {
		t.Fatalf("mapped value: out=%d carried=%v err=%v", out, carried, err)
	}
}

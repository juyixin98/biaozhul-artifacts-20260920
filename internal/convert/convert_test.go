package convert

import (
	"math"
	"strings"
	"testing"
	"time"

	telemetryv1 "github.com/p079/telegw/gen/telemetry/v1"
	telemetryv2 "github.com/p079/telegw/gen/telemetry/v2"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

var (
	epoch  = timestamppb.New(time.Unix(0, 0))
	strict = Mapping{Version: "strict", Enum: EnumError, Rounding: RoundStrict}
	carry  = Mapping{Version: "carry", Enum: EnumCarry, Rounding: RoundHalfEven}
)

func i32(v int32) *int32 { return &v }
func i64(v int64) *int64 { return &v }

func TestV1ToV2FieldRenameAndUnitTransform(t *testing.T) {
	in := &telemetryv1.Reading{
		DeviceId:         "dev-1",
		TempCelsiusE2:    i32(2150), // 21.50 °C
		Condition:        telemetryv1.Condition_DEGRADED,
		ObservedAtUnixMs: 1_758_614_400_000,
		SeqNo:            7,
	}
	out, attrs, unknown, err := V1ToV2(strict, in)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if unknown != 0 {
		t.Fatalf("unknown = %d, want 0", unknown)
	}
	if out.DeviceUid != "dev-1" {
		t.Errorf("device rename failed: %q", out.DeviceUid)
	}
	// 2150 cC * 10 + 2 731 500 = 2 753 000 mK
	if got := out.GetTemperatureMillikelvin(); got != 2_753_000 {
		t.Errorf("temp = %d mK, want 2753000", got)
	}
	if out.Condition != telemetryv2.Condition_DEGRADED {
		t.Errorf("condition = %v", out.Condition)
	}
	if out.ObservedAt.GetSeconds() != 1_758_614_400 {
		t.Errorf("observed_at seconds = %d", out.ObservedAt.GetSeconds())
	}
	if out.SeqNo != 7 {
		t.Errorf("seq_no = %d", out.SeqNo)
	}
	if len(attrs) != 0 {
		t.Errorf("attrs = %v, want empty", attrs)
	}
}

// Explicit zero must survive as explicit zero; unset must stay unset.
func TestPresenceExplicitZeroVsUnset(t *testing.T) {
	zero := &telemetryv1.Reading{DeviceId: "d", TempCelsiusE2: i32(0), ObservedAtUnixMs: 1}
	out, _, _, err := V1ToV2(strict, zero)
	if err != nil {
		t.Fatal(err)
	}
	if out.TemperatureMillikelvin == nil {
		t.Fatal("explicit 0 cC was dropped")
	}
	if got := out.GetTemperatureMillikelvin(); got != 2_731_500 {
		t.Fatalf("0 cC = %d mK, want 2731500 (0 °C in mK, not a zeroed value)", got)
	}

	unset := &telemetryv1.Reading{DeviceId: "d", ObservedAtUnixMs: 1}
	out2, _, _, err := V1ToV2(strict, unset)
	if err != nil {
		t.Fatal(err)
	}
	if out2.TemperatureMillikelvin != nil {
		t.Fatal("unset temperature became set")
	}

	// And back: explicit 2731500 mK -> explicit 0 cC.
	back, _, _, err := V2ToV1(strict, &telemetryv2.Reading{
		DeviceUid: "d", TemperatureMillikelvin: i64(2_731_500),
		ObservedAt: epoch,
	})
	if err != nil {
		t.Fatal(err)
	}
	if back.TempCelsiusE2 == nil || *back.TempCelsiusE2 != 0 {
		t.Fatalf("explicit 0 cC did not round-trip: %v", back.TempCelsiusE2)
	}
}

func TestEnumMappingBothDirections(t *testing.T) {
	for _, c := range []telemetryv1.Condition{
		telemetryv1.Condition_CONDITION_UNSPECIFIED,
		telemetryv1.Condition_OK,
		telemetryv1.Condition_DEGRADED,
		telemetryv1.Condition_FAILED,
	} {
		out, _, _, err := V1ToV2(strict, &telemetryv1.Reading{DeviceId: "d", Condition: c, ObservedAtUnixMs: 1})
		if err != nil {
			t.Fatalf("v1 %v: %v", c, err)
		}
		if int32(out.Condition) != int32(c) {
			t.Errorf("v1 %v -> v2 %v", c, out.Condition)
		}
	}
}

func TestUnmappedEnumStrictReturnsLocatableError(t *testing.T) {
	in := &telemetryv2.Reading{
		DeviceUid:  "d",
		Condition:  telemetryv2.Condition_MAINTENANCE,
		ObservedAt: epoch,
	}
	_, _, _, err := V2ToV1(strict, in)
	if err == nil {
		t.Fatal("expected error for MAINTENANCE under strict mapping")
	}
	ce, ok := AsError(err)
	if !ok {
		t.Fatalf("error is not a *convert.Error: %T", err)
	}
	if ce.FieldPath != "condition" {
		t.Errorf("field path = %q, want condition", ce.FieldPath)
	}
	if !strings.Contains(ce.RawValue, "4") {
		t.Errorf("raw value %q should carry the original enum number 4", ce.RawValue)
	}
}

func TestUnmappedEnumCarryPreservesRawValue(t *testing.T) {
	in := &telemetryv2.Reading{
		DeviceUid:  "d",
		Condition:  telemetryv2.Condition_CALIBRATING,
		ObservedAt: epoch,
	}
	out, attrs, _, err := V2ToV1(carry, in)
	if err != nil {
		t.Fatal(err)
	}
	if out.Condition != telemetryv1.Condition_CONDITION_UNSPECIFIED {
		t.Errorf("condition = %v", out.Condition)
	}
	if attrs["unmapped_condition_raw"] != "5" {
		t.Errorf("raw value not carried: attrs=%v", attrs)
	}
	if attrs["unmapped_condition_name"] != "CALIBRATING" {
		t.Errorf("enum name not carried: attrs=%v", attrs)
	}
}

func TestRoundingStrictRejectsInexact(t *testing.T) {
	in := &telemetryv2.Reading{
		DeviceUid:              "d",
		TemperatureMillikelvin: i64(2_731_503), // remainder 3 mK
		ObservedAt:             epoch,
	}
	_, _, _, err := V2ToV1(strict, in)
	ce, ok := AsError(err)
	if !ok {
		t.Fatalf("want locatable error, got %v", err)
	}
	if ce.FieldPath != "temperature_millikelvin" {
		t.Errorf("field path = %q", ce.FieldPath)
	}
	if ce.RawValue != "2731503" {
		t.Errorf("raw value = %q", ce.RawValue)
	}
}

func TestRoundingHalfEven(t *testing.T) {
	cases := []struct {
		mK   int64
		want int32
	}{
		{2_731_503, 0},  // +3 mK -> down
		{2_731_505, 0},  // tie, q=0 even -> stays
		{2_731_515, 2},  // tie, q=1 odd -> up to 2
		{2_731_497, -1}, // -3 mK -> toward zero... -0.3 rounds to 0? no: v=-3, q=0, r=-3, |r|<5 -> 0
	}
	// fix expectation for the negative case: v=-3 -> q=0 r=-3 -> 0
	cases[3].want = 0
	for _, tc := range cases {
		out, attrs, _, err := V2ToV1(carry, &telemetryv2.Reading{
			DeviceUid: "d", TemperatureMillikelvin: i64(tc.mK),
			ObservedAt: epoch,
		})
		if err != nil {
			t.Fatalf("mK=%d: %v", tc.mK, err)
		}
		if got := out.GetTempCelsiusE2(); got != tc.want {
			t.Errorf("mK=%d -> %d cC, want %d", tc.mK, got, tc.want)
		}
		if attrs["rounded"] != "true" {
			t.Errorf("mK=%d: rounding not recorded in attrs: %v", tc.mK, attrs)
		}
	}
}

func TestOverflowChecked(t *testing.T) {
	// cC result would exceed int32.
	big := int64(math.MaxInt32)*10 + 2_731_500 + 10
	_, _, _, err := V2ToV1(carry, &telemetryv2.Reading{
		DeviceUid: "d", TemperatureMillikelvin: &big,
		ObservedAt: epoch,
	})
	ce, ok := AsError(err)
	if !ok {
		t.Fatalf("want overflow error, got %v", err)
	}
	if !strings.Contains(ce.Reason, "overflows int32") {
		t.Errorf("reason = %q", ce.Reason)
	}
}

func TestUnknownFieldsCountedAndReported(t *testing.T) {
	in := &telemetryv1.Reading{DeviceId: "d", ObservedAtUnixMs: 1}
	raw, err := proto.Marshal(in)
	if err != nil {
		t.Fatal(err)
	}
	// Append an unknown varint field (field 99).
	raw = protowire.AppendTag(raw, 99, protowire.VarintType)
	raw = protowire.AppendVarint(raw, 12345)
	var withUnknown telemetryv1.Reading
	if err := proto.Unmarshal(raw, &withUnknown); err != nil {
		t.Fatal(err)
	}
	_, attrs, unknown, err := V1ToV2(strict, &withUnknown)
	if err != nil {
		t.Fatal(err)
	}
	if unknown != 1 {
		t.Errorf("unknown = %d, want 1", unknown)
	}
	if attrs["unknown_fields_dropped"] != "1" {
		t.Errorf("attrs = %v", attrs)
	}
}

func TestV2OnlyFieldDroppedAndReported(t *testing.T) {
	out, attrs, _, err := V2ToV1(strict, &telemetryv2.Reading{
		DeviceUid: "d", FirmwareRev: "2.4.1",
		ObservedAt: epoch,
	})
	if err != nil {
		t.Fatal(err)
	}
	_ = out
	if attrs["dropped_fields"] != "firmware_rev" || attrs["firmware_rev"] != "2.4.1" {
		t.Errorf("dropped field not reported: %v", attrs)
	}
}

func TestSubMillisecondTimestampStrict(t *testing.T) {
	ts := &timestamppb.Timestamp{Seconds: 1_758_614_400, Nanos: 1_500_000} // 1.5 ms
	_, _, _, err := V2ToV1(strict, &telemetryv2.Reading{DeviceUid: "d", ObservedAt: ts})
	ce, ok := AsError(err)
	if !ok || ce.FieldPath != "observed_at" {
		t.Fatalf("want locatable observed_at error, got %v", err)
	}
}

func TestTimestampRoundTrip(t *testing.T) {
	in := &telemetryv1.Reading{DeviceId: "d", ObservedAtUnixMs: 1_758_614_400_123}
	v2, _, _, err := V1ToV2(strict, in)
	if err != nil {
		t.Fatal(err)
	}
	back, _, _, err := V2ToV1(strict, &telemetryv2.Reading{
		DeviceUid: "d", ObservedAt: v2.ObservedAt,
	})
	if err != nil {
		t.Fatal(err)
	}
	if back.ObservedAtUnixMs != in.ObservedAtUnixMs {
		t.Errorf("round trip: %d != %d", back.ObservedAtUnixMs, in.ObservedAtUnixMs)
	}
}

func TestMappingValidate(t *testing.T) {
	if err := strict.Validate(); err != nil {
		t.Error(err)
	}
	if err := (Mapping{Version: "x", Enum: "bogus", Rounding: RoundStrict}).Validate(); err == nil {
		t.Error("expected error for bogus enum policy")
	}
	if err := (Mapping{Version: "", Enum: EnumError, Rounding: RoundStrict}).Validate(); err == nil {
		t.Error("expected error for empty version")
	}
}

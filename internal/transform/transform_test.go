package transform

import (
	"errors"
	"testing"

	telemetryv1 "github.com/example/compgw/gen/telemetry/v1"
	telemetryv2 "github.com/example/compgw/gen/telemetry/v2"
	cryptox "github.com/example/compgw/internal/crypto"
	"github.com/example/compgw/internal/mapping"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

var (
	key         = mapping.DefaultHMACKey
	specs       = mapping.SeedSpecs()
	specV1V2    = specs[0]
	specV2V1    = specs[1]
	specLenient = specs[2]
)

func sign1(r *telemetryv1.TelemetryRecord) *telemetryv1.TelemetryRecord {
	r.Signature = cryptox.Sign(key, cryptox.CanonicalV1(r.GetDeviceId(), r.GetTimestampMs(),
		r.GetTempMilliC(), int32(r.GetStatus()), r.GetLabels(), r.GetRequestId(), r.BatteryMilliPct))
	return r
}

func sign2(r *telemetryv2.TelemetryRecord) *telemetryv2.TelemetryRecord {
	r.Signature = cryptox.Sign(key, cryptox.CanonicalV2(r.GetDeviceId(), r.GetObservedAtNs(),
		r.GetTemperatureUk(), int32(r.GetPhase()), r.GetLabels(), r.GetTraceId(), r.BatteryPctMicro))
	return r
}

func sampleV1() *telemetryv1.TelemetryRecord {
	bat := int64(870)
	return sign1(&telemetryv1.TelemetryRecord{
		DeviceId: "sensor-A", TimestampMs: 1_727_000_000_123, TempMilliC: 21_250,
		Status: telemetryv1.Status_STATUS_ACTIVE, RequestId: "req-1",
		Labels: map[string]string{"site": "dc-1"}, BatteryMilliPct: &bat,
	})
}

func TestV1ToV2_HappyPath(t *testing.T) {
	res, err := V1ToV2(&specV1V2, sampleV1(), false)
	if err != nil {
		t.Fatalf("convert: %v", err)
	}
	out := res.Record
	// explicit field renames + unit conversions
	if out.GetDeviceId() != "sensor-A" || out.GetTraceId() != "req-1" {
		t.Fatalf("identity/rename fields wrong: %v", out)
	}
	if out.GetObservedAtNs() != 1_727_000_000_123_000_000 {
		t.Fatalf("timestamp unit: got %d", out.GetObservedAtNs())
	}
	if out.GetTemperatureUk() != 294_400_000 {
		t.Fatalf("temperature affine unit: got %d", out.GetTemperatureUk())
	}
	if out.GetPhase() != telemetryv2.Phase_PHASE_ACTIVE {
		t.Fatalf("enum: got %v", out.GetPhase())
	}
	if out.GetBatteryPctMicro() != 870_000 {
		t.Fatalf("battery unit: got %d", out.GetBatteryPctMicro())
	}
	// output signature must verify under the v2 canonical form
	if !cryptox.Verify(key, cryptox.CanonicalV2(out.GetDeviceId(), out.GetObservedAtNs(),
		out.GetTemperatureUk(), int32(out.GetPhase()), out.GetLabels(), out.GetTraceId(),
		out.BatteryPctMicro), out.GetSignature()) {
		t.Fatal("outbound v2 signature invalid")
	}
}

func TestUnknownFieldsCountedAndPreservedOnWire(t *testing.T) {
	r := sampleV1()
	raw, err := proto.Marshal(r)
	if err != nil {
		t.Fatal(err)
	}
	// Append an unknown field (field 50, wire type 0 varint = 123).
	raw = protowire.AppendTag(raw, 50, protowire.VarintType)
	raw = protowire.AppendVarint(raw, 123)

	var got telemetryv1.TelemetryRecord
	if err := proto.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	if n := CountUnknown(got.ProtoReflect()); n != 1 {
		t.Fatalf("expected 1 unknown field, got %d", n)
	}
	res, err := V1ToV2(&specV1V2, &got, false)
	if err != nil {
		t.Fatalf("unknown fields must not reject the message: %v", err)
	}
	if res.UnknownN != 1 {
		t.Fatalf("provenance should report 1 unknown field, got %d", res.UnknownN)
	}
	// A marshal/unmarshal round trip of the inbound message preserves the field.
	raw2, _ := proto.Marshal(&got)
	var round telemetryv1.TelemetryRecord
	if err := proto.Unmarshal(raw2, &round); err != nil {
		t.Fatal(err)
	}
	if n := CountUnknown(round.ProtoReflect()); n != 1 {
		t.Fatalf("unknown field not preserved through wire round trip: %d", n)
	}
}

func TestBattery_AbsentVsExplicitZero(t *testing.T) {
	absent := sign1(&telemetryv1.TelemetryRecord{
		DeviceId: "d", TimestampMs: 1000, TempMilliC: 0,
		Status: telemetryv1.Status_STATUS_IDLE, RequestId: "r",
	})
	resA, err := V1ToV2(&specV1V2, absent, false)
	if err != nil {
		t.Fatal(err)
	}
	if resA.Record.BatteryPctMicro != nil {
		t.Fatalf("absent battery must stay absent, got %v", *resA.Record.BatteryPctMicro)
	}

	zero := int64(0)
	explicit := sign1(&telemetryv1.TelemetryRecord{
		DeviceId: "d", TimestampMs: 1000, TempMilliC: 0,
		Status: telemetryv1.Status_STATUS_IDLE, RequestId: "r", BatteryMilliPct: &zero,
	})
	resZ, err := V1ToV2(&specV1V2, explicit, false)
	if err != nil {
		t.Fatal(err)
	}
	if resZ.Record.BatteryPctMicro == nil || *resZ.Record.BatteryPctMicro != 0 {
		t.Fatalf("explicit zero must convert to explicit zero, got %v", resZ.Record.BatteryPctMicro)
	}
	// Signatures must differ, proving the distinction is cryptographically carried.
	if string(resA.Record.Signature) == string(resZ.Record.Signature) {
		t.Fatal("absent vs explicit-zero produced identical signatures")
	}
}

func TestV2ToV1_MaintenanceStrictFailsLocatably(t *testing.T) {
	r := sign2(&telemetryv2.TelemetryRecord{
		DeviceId: "d", ObservedAtNs: 1_000_000, TemperatureUk: 294_400_000,
		Phase: telemetryv2.Phase_PHASE_MAINTENANCE, TraceId: "tr-4",
	})
	_, err := V2ToV1(&specV2V1, r, false)
	var f *mapping.Failure
	if !errors.As(err, &f) {
		t.Fatalf("expected Failure, got %v", err)
	}
	if f.Code != mapping.FailEnumOverflow || f.Field != "phase" || f.RecordID != "tr-4" {
		t.Fatalf("failure not locatable: %+v", f)
	}
	if f.OriginalEnum == nil || *f.OriginalEnum != 4 {
		t.Fatalf("must carry original enum 4, got %v", f.OriginalEnum)
	}
}

func TestV2ToV1_MaintenanceLenientCarries(t *testing.T) {
	r := sign2(&telemetryv2.TelemetryRecord{
		DeviceId: "d", ObservedAtNs: 1_000_000, TemperatureUk: 294_400_000,
		Phase: telemetryv2.Phase_PHASE_MAINTENANCE, TraceId: "tr-4",
	})
	res, err := V2ToV1(&specLenient, r, false)
	if err != nil {
		t.Fatalf("lenient policy should carry, not fail: %v", err)
	}
	if res.Record.GetStatus() != telemetryv1.Status_STATUS_UNSPECIFIED {
		t.Fatalf("target enum should be 0, got %v", res.Record.GetStatus())
	}
	if res.Record.GetRawPhase() != 4 || res.Carried == nil || *res.Carried != 4 {
		t.Fatalf("raw_phase must carry 4, got raw=%v carried=%v", res.Record.GetRawPhase(), res.Carried)
	}
	// Output still validly signed.
	if !cryptox.Verify(key, cryptox.CanonicalV1(res.Record.GetDeviceId(), res.Record.GetTimestampMs(),
		res.Record.GetTempMilliC(), int32(res.Record.GetStatus()), res.Record.GetLabels(),
		res.Record.GetRequestId(), res.Record.BatteryMilliPct), res.Record.GetSignature()) {
		t.Fatal("carried-record signature invalid")
	}
}

func TestV2ToV1_RoundingPolicy(t *testing.T) {
	mk := func() *telemetryv2.TelemetryRecord {
		return sign2(&telemetryv2.TelemetryRecord{
			DeviceId: "d", ObservedAtNs: 1_727_000_000_123_456_789,
			TemperatureUk: 294_400_499, Phase: telemetryv2.Phase_PHASE_ACTIVE, TraceId: "tr-r",
		})
	}
	// strict REJECT: timestamp sub-ms AND temperature sub-milli-degree both fail.
	_, err := V2ToV1(&specV2V1, mk(), false)
	var f *mapping.Failure
	if !errors.As(err, &f) || f.Code != mapping.FailRoundingRequired {
		t.Fatalf("expected ROUNDING_REQUIRED, got %v", err)
	}
	// lenient NEAREST: temp 21250.499mC -> 21250; ts 123.456789ms -> 123.
	res, err := V2ToV1(&specLenient, mk(), false)
	if err != nil {
		t.Fatalf("lenient should round: %v", err)
	}
	if res.Record.GetTempMilliC() != 21_250 {
		t.Fatalf("nearest temp: got %d", res.Record.GetTempMilliC())
	}
	if res.Record.GetTimestampMs() != 1_727_000_000_123 {
		t.Fatalf("nearest ts: got %d", res.Record.GetTimestampMs())
	}
}

func TestSignatureInvalid(t *testing.T) {
	r := sampleV1()
	r.Signature[0] ^= 0xFF
	_, err := V1ToV2(&specV1V2, r, false)
	var f *mapping.Failure
	if !errors.As(err, &f) || f.Code != mapping.FailSignatureInvalid {
		t.Fatalf("expected SIGNATURE_INVALID, got %v", err)
	}
}

func TestV1ToV2_Overflow(t *testing.T) {
	r := sign1(&telemetryv1.TelemetryRecord{
		DeviceId: "d", TimestampMs: 1, TempMilliC: 9_223_372_036_854_775_000,
		Status: telemetryv1.Status_STATUS_ACTIVE, RequestId: "big",
	})
	_, err := V1ToV2(&specV1V2, r, false)
	var f *mapping.Failure
	if !errors.As(err, &f) || f.Code != mapping.FailIntegerOverflow || f.Field != "temp_milli_c" {
		t.Fatalf("expected locatable INTEGER_OVERFLOW on temp_milli_c, got %v", err)
	}
}

func TestRoundTripV2V1V2(t *testing.T) {
	bat := int64(870_000)
	orig := sign2(&telemetryv2.TelemetryRecord{
		DeviceId: "sensor-A", ObservedAtNs: 1_727_000_000_123_000_000,
		TemperatureUk: 294_400_000, Phase: telemetryv2.Phase_PHASE_IDLE,
		TraceId: "tr-rt", Labels: map[string]string{"k": "v"}, BatteryPctMicro: &bat,
	})
	r1, err := V2ToV1(&specV2V1, orig, false)
	if err != nil {
		t.Fatal(err)
	}
	r2, err := V1ToV2(&specV1V2, r1.Record, false)
	if err != nil {
		t.Fatal(err)
	}
	back := r2.Record
	if back.GetObservedAtNs() != orig.GetObservedAtNs() ||
		back.GetTemperatureUk() != orig.GetTemperatureUk() ||
		back.GetPhase() != orig.GetPhase() ||
		back.GetBatteryPctMicro() != orig.GetBatteryPctMicro() ||
		back.GetTraceId() != orig.GetTraceId() {
		t.Fatalf("round trip changed values:\norig=%v\nback=%v", orig, back)
	}
}

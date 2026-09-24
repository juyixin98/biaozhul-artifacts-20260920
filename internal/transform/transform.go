// Package transform converts telemetry records between schema v1 and v2 using
// a mapping.Spec: explicit field renames, exact integer unit conversions,
// explicit enum tables, HMAC verification/re-signing, and preservation of
// proto3 optional presence and unknown fields.
package transform

import (
	"fmt"

	telemetryv1 "github.com/example/compgw/gen/telemetry/v1"
	telemetryv2 "github.com/example/compgw/gen/telemetry/v2"
	cryptox "github.com/example/compgw/internal/crypto"
	"github.com/example/compgw/internal/mapping"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// Result carries a converted record plus provenance details.
type ResultV1ToV2 struct {
	Record   *telemetryv2.TelemetryRecord
	UnknownN uint32
}

type ResultV2ToV1 struct {
	Record   *telemetryv1.TelemetryRecord
	UnknownN uint32
	Carried  *int32 // original enum number when policy is CARRY_ORIGINAL
}

// V1ToV2 validates the inbound HMAC (unless skipVerify), applies the mapping,
// and signs the output with the v2 key. The spec's direction is not assumed;
// callers select the right spec.
func V1ToV2(spec *mapping.Spec, in *telemetryv1.TelemetryRecord, skipVerify bool) (*ResultV1ToV2, error) {
	if spec.Direction != mapping.DirectionV1ToV2 {
		return nil, fmt.Errorf("mapping %q direction %s cannot convert v1->v2", spec.Name, spec.Direction)
	}
	recID := in.GetRequestId()

	batV1 := in.BatteryMilliPct // *int64, nil when absent
	if !skipVerify {
		canon := cryptox.CanonicalV1(in.GetDeviceId(), in.GetTimestampMs(), in.GetTempMilliC(),
			int32(in.GetStatus()), in.GetLabels(), in.GetRequestId(), batV1)
		if !cryptox.Verify(spec.HMACKeyV1, canon, in.GetSignature()) {
			return nil, &mapping.Failure{Code: mapping.FailSignatureInvalid, Field: "signature",
				RecordID: recID, Message: "inbound v1 HMAC-SHA256 signature did not verify"}
		}
	}

	out := &telemetryv2.TelemetryRecord{
		DeviceId: in.GetDeviceId(),
		Labels:   in.GetLabels(),
		TraceId:  in.GetRequestId(),
	}

	// Scalar unit conversions, each located by its explicit field name.
	ns, err := convertField(spec, "timestamp_ms", in.GetTimestampMs())
	if err != nil {
		return nil, locErr(err, recID)
	}
	out.ObservedAtNs = ns

	uk, err := convertField(spec, "temp_milli_c", in.GetTempMilliC())
	if err != nil {
		return nil, locErr(err, recID)
	}
	out.TemperatureUk = uk

	phase, carried0, err := mapping.MapEnumField(spec, "status", int64(in.GetStatus()), recID)
	if err != nil {
		return nil, err
	}
	_ = carried0 // no v1->v2 unmappable values can exist for current schemas
	out.Phase = telemetryv2.Phase(phase)

	// Optional battery: absence stays absence; an explicit value converts.
	if batV1 != nil {
		bat, err := convertField(spec, "battery_milli_pct", *batV1)
		if err != nil {
			return nil, locErr(err, recID)
		}
		out.BatteryPctMicro = &bat
	}

	// Re-sign with the target-schema key.
	out.Signature = cryptox.Sign(spec.HMACKeyV2, cryptox.CanonicalV2(
		out.GetDeviceId(), out.GetObservedAtNs(), out.GetTemperatureUk(),
		int32(out.GetPhase()), out.GetLabels(), out.GetTraceId(), out.BatteryPctMicro))

	return &ResultV1ToV2{Record: out, UnknownN: CountUnknown(in.ProtoReflect())}, nil
}

// V2ToV1 is the reverse. When the phase is unmappable, either a locatable
// error is returned (EnumPolicyError) or status becomes 0 and the original
// numeric value is carried on raw_phase and provenance (EnumPolicyCarry).
func V2ToV1(spec *mapping.Spec, in *telemetryv2.TelemetryRecord, skipVerify bool) (*ResultV2ToV1, error) {
	if spec.Direction != mapping.DirectionV2ToV1 {
		return nil, fmt.Errorf("mapping %q direction %s cannot convert v2->v1", spec.Name, spec.Direction)
	}
	recID := in.GetTraceId()
	batV2 := in.BatteryPctMicro

	if !skipVerify {
		canon := cryptox.CanonicalV2(in.GetDeviceId(), in.GetObservedAtNs(), in.GetTemperatureUk(),
			int32(in.GetPhase()), in.GetLabels(), in.GetTraceId(), batV2)
		if !cryptox.Verify(spec.HMACKeyV2, canon, in.GetSignature()) {
			return nil, &mapping.Failure{Code: mapping.FailSignatureInvalid, Field: "signature",
				RecordID: recID, Message: "inbound v2 HMAC-SHA256 signature did not verify"}
		}
	}

	out := &telemetryv1.TelemetryRecord{
		DeviceId:  in.GetDeviceId(),
		Labels:    in.GetLabels(),
		RequestId: in.GetTraceId(),
	}

	ms, err := convertField(spec, "observed_at_ns", in.GetObservedAtNs())
	if err != nil {
		return nil, locErr(err, recID)
	}
	out.TimestampMs = ms

	mc, err := convertField(spec, "temperature_uk", in.GetTemperatureUk())
	if err != nil {
		return nil, locErr(err, recID)
	}
	out.TempMilliC = mc

	var carried *int32
	status, carriedEnum, err := mapping.MapEnumField(spec, "phase", int64(in.GetPhase()), recID)
	if err != nil {
		return nil, err
	}
	out.Status = telemetryv1.Status(status)
	if carriedEnum != nil {
		c := int32(*carriedEnum)
		carried = &c
		out.RawPhase = &c
	}

	if batV2 != nil {
		bat, err := convertField(spec, "battery_pct_micro", *batV2)
		if err != nil {
			return nil, locErr(err, recID)
		}
		out.BatteryMilliPct = &bat
	}

	out.Signature = cryptox.Sign(spec.HMACKeyV1, cryptox.CanonicalV1(
		out.GetDeviceId(), out.GetTimestampMs(), out.GetTempMilliC(),
		int32(out.GetStatus()), out.GetLabels(), out.GetRequestId(), out.BatteryMilliPct))

	return &ResultV2ToV1{Record: out, UnknownN: CountUnknown(in.ProtoReflect()), Carried: carried}, nil
}

func convertField(spec *mapping.Spec, from string, in int64) (int64, error) {
	for _, f := range spec.Fields {
		if f.From != from {
			continue
		}
		switch f.Kind {
		case "identity":
			return in, nil
		case "int_unit":
			return mapping.ConvertInt(in, f.Unit.Num, f.Unit.Den, f.Unit.PreAdd, f.Unit.PostAdd, f.Unit.Mode, from)
		}
	}
	return 0, &mapping.Failure{Field: from, Message: "no mapping rule for field"}
}

// locErr stamps a RecordID onto mapping.Fail coming out of int conversion.
func locErr(err error, recID string) error {
	var f *mapping.Failure
	if asErr(err, &f) && f.RecordID == "" {
		f.RecordID = recID
	}
	return err
}

func asErr(err error, target **mapping.Failure) bool {
	if fe, ok := err.(*mapping.Failure); ok {
		*target = fe
		return true
	}
	return false
}

// CountUnknown returns how many unrecognized fields are present in m's unknown
// bytes. Unknown fields are never rejected and survive a marshal round trip.
func CountUnknown(m protoreflect.Message) uint32 {
	var n uint32
	ranges := m.GetUnknown()
	for len(ranges) > 0 {
		num, _, nBytes := protowire.ConsumeField(ranges)
		if num == 0 || nBytes <= 0 {
			break
		}
		n++
		ranges = ranges[nBytes:]
	}
	return n
}

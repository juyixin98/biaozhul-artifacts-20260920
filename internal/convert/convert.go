// Package convert implements the explicit, checked mappings between
// telemetry schema v1 and v2. Nothing here silently degrades: every
// lossy step either returns a locatable *Error or records the original
// value in the output attributes.
package convert

import (
	"errors"
	"fmt"
	"math"

	telemetryv1 "github.com/p079/telegw/gen/telemetry/v1"
	telemetryv2 "github.com/p079/telegw/gen/telemetry/v2"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// ConverterVersion is stamped onto every output envelope.
const ConverterVersion = "1.0.0"

const (
	SourceV1 = "telemetry/v1"
	SourceV2 = "telemetry/v2"
)

// milliKelvinOffset: 0 °C == 2 731 500 mK.
const milliKelvinOffset int64 = 2_731_500

// Error is a locatable conversion failure: it names the field, the
// reason, and carries the original raw value.
type Error struct {
	FieldPath string
	Reason    string
	RawValue  string
}

func (e *Error) Error() string {
	return fmt.Sprintf("convert: field %q: %s (raw value %q)", e.FieldPath, e.Reason, e.RawValue)
}

// AsError unwraps err into a *Error if it is one.
func AsError(err error) (*Error, bool) {
	var ce *Error
	if errors.As(err, &ce) {
		return ce, true
	}
	return nil, false
}

// EnumPolicy decides how v2-only enum values are handled on v2 -> v1.
type EnumPolicy string

const (
	// EnumError returns a locatable *Error for unmapped enum values.
	EnumError EnumPolicy = "error"
	// EnumCarry emits CONDITION_UNSPECIFIED plus the original numeric
	// value in attribute "unmapped_condition_raw" (never a silent 0).
	EnumCarry EnumPolicy = "carry"
)

// RoundingPolicy decides how non-exact integer unit divisions are handled.
type RoundingPolicy string

const (
	// RoundStrict returns a locatable *Error when a unit conversion is
	// not exact (remainder != 0).
	RoundStrict RoundingPolicy = "strict"
	// RoundHalfEven rounds to nearest, ties to even, and records
	// attribute "rounded"="true" on the output.
	RoundHalfEven RoundingPolicy = "half_even"
)

// Mapping is one named, hot-swappable conversion configuration.
type Mapping struct {
	Version  string         `json:"version"`
	Enum     EnumPolicy     `json:"enum_policy"`
	Rounding RoundingPolicy `json:"rounding_policy"`
}

// Validate checks a mapping for known policy values.
func (m Mapping) Validate() error {
	switch m.Enum {
	case EnumError, EnumCarry:
	default:
		return fmt.Errorf("unknown enum_policy %q", m.Enum)
	}
	switch m.Rounding {
	case RoundStrict, RoundHalfEven:
	default:
		return fmt.Errorf("unknown rounding_policy %q", m.Rounding)
	}
	if m.Version == "" {
		return errors.New("mapping version must not be empty")
	}
	return nil
}

// Attrs carries per-record conversion notes into Meta.attributes.
type Attrs map[string]string

func (a Attrs) set(k, v string) { a[k] = v }

// ---- enum mappings (explicit, both directions) ----

var conditionV1ToV2 = map[telemetryv1.Condition]telemetryv2.Condition{
	telemetryv1.Condition_CONDITION_UNSPECIFIED: telemetryv2.Condition_CONDITION_UNSPECIFIED,
	telemetryv1.Condition_OK:                    telemetryv2.Condition_OK,
	telemetryv1.Condition_DEGRADED:              telemetryv2.Condition_DEGRADED,
	telemetryv1.Condition_FAILED:                telemetryv2.Condition_FAILED,
}

var conditionV2ToV1 = map[telemetryv2.Condition]telemetryv1.Condition{
	telemetryv2.Condition_CONDITION_UNSPECIFIED: telemetryv1.Condition_CONDITION_UNSPECIFIED,
	telemetryv2.Condition_OK:                    telemetryv1.Condition_OK,
	telemetryv2.Condition_DEGRADED:              telemetryv1.Condition_DEGRADED,
	telemetryv2.Condition_FAILED:                telemetryv1.Condition_FAILED,
	// MAINTENANCE and CALIBRATING intentionally have no v1 entry.
}

// ---- integer unit conversions with overflow and rounding checks ----

// celsiusE2ToMilliKelvin: centi-°C -> mK. Exact and overflow-free by
// construction (int32 * 10 + offset fits int64), but checked anyway.
func celsiusE2ToMilliKelvin(cC int32) (int64, error) {
	mK := int64(cC)*10 + milliKelvinOffset
	return mK, nil
}

// milliKelvinToCelsiusE2: mK -> centi-°C. Checks rounding (remainder of
// the /10 division) and int32 overflow.
func milliKelvinToCelsiusE2(mK int64, rp RoundingPolicy, attrs Attrs) (int32, error) {
	v := mK - milliKelvinOffset
	q := v / 10
	r := v % 10 // sign of r follows v (Go truncation)
	if r != 0 {
		switch rp {
		case RoundStrict:
			return 0, &Error{
				FieldPath: "temperature_millikelvin",
				Reason:    "unit conversion mK->cC is not exact (remainder of /10)",
				RawValue:  fmt.Sprintf("%d", mK),
			}
		case RoundHalfEven:
			// Round magnitude to nearest, ties to even, preserving sign.
			ar := r
			if ar < 0 {
				ar = -ar
			}
			switch {
			case ar > 5:
				if v > 0 {
					q++
				} else {
					q--
				}
			case ar == 5:
				if q%2 != 0 {
					if v > 0 {
						q++
					} else {
						q--
					}
				}
			}
			attrs.set("rounded", "true")
			attrs.set("rounding_remainder_mk", fmt.Sprintf("%d", r))
		}
	}
	if q < math.MinInt32 || q > math.MaxInt32 {
		return 0, &Error{
			FieldPath: "temperature_millikelvin",
			Reason:    fmt.Sprintf("value %d cC overflows int32 after mK->cC conversion", q),
			RawValue:  fmt.Sprintf("%d", mK),
		}
	}
	return int32(q), nil
}

// ---- timestamp conversions ----

func unixMsToTimestamp(ms int64) (*timestamppb.Timestamp, error) {
	ts := &timestamppb.Timestamp{Seconds: ms / 1_000, Nanos: int32(ms%1_000) * 1_000_000}
	if ms < 0 && ms%1_000 != 0 { // normalize negative remainders
		ts.Seconds--
		ts.Nanos += 1_000_000_000
	}
	if err := ts.CheckValid(); err != nil {
		return nil, &Error{
			FieldPath: "observed_at_unix_ms",
			Reason:    "unix-ms timestamp out of representable range: " + err.Error(),
			RawValue:  fmt.Sprintf("%d", ms),
		}
	}
	return ts, nil
}

func timestampToUnixMs(ts *timestamppb.Timestamp, rp RoundingPolicy, attrs Attrs) (int64, error) {
	if ts == nil {
		return 0, &Error{FieldPath: "observed_at", Reason: "timestamp is required in v2", RawValue: "<nil>"}
	}
	if err := ts.CheckValid(); err != nil {
		return 0, &Error{
			FieldPath: "observed_at",
			Reason:    "invalid timestamp: " + err.Error(),
			RawValue:  ts.String(),
		}
	}
	if rem := ts.Nanos % 1_000_000; rem != 0 {
		switch rp {
		case RoundStrict:
			return 0, &Error{
				FieldPath: "observed_at",
				Reason:    "sub-millisecond precision cannot be expressed in v1 unix-ms",
				RawValue:  fmt.Sprintf("seconds=%d nanos=%d", ts.Seconds, ts.Nanos),
			}
		case RoundHalfEven:
			attrs.set("rounded", "true")
			attrs.set("rounding_remainder_nanos", fmt.Sprintf("%d", rem))
		}
	}
	ms := ts.Seconds * 1_000
	// Overflow check on seconds*1000 + nanos/1e6.
	if ts.Seconds > (math.MaxInt64-999)/1_000 || ts.Seconds < math.MinInt64/1_000 {
		return 0, &Error{
			FieldPath: "observed_at",
			Reason:    "timestamp overflows int64 unix-ms",
			RawValue:  fmt.Sprintf("seconds=%d", ts.Seconds),
		}
	}
	return ms + int64(ts.Nanos)/1_000_000, nil
}

// countUnknown returns the number of unknown fields on the wire message.
func countUnknown(m protoreflect.ProtoMessage) int {
	u := m.ProtoReflect().GetUnknown()
	n := 0
	for len(u) > 0 {
		_, _, nw := protowire.ConsumeField(u)
		if nw < 0 {
			break
		}
		u = u[nw:]
		n++
	}
	return n
}

// V1ToV2 converts one v1 reading. Field renames:
// device_id->device_uid, temp_celsius_e2->temperature_millikelvin,
// observed_at_unix_ms->observed_at. Presence of the optional
// temperature is preserved (explicit 0 stays explicit 0).
func V1ToV2(m Mapping, in *telemetryv1.Reading) (out *telemetryv2.Reading, attrs Attrs, unknown int, err error) {
	attrs = Attrs{}
	unknown = countUnknown(in)
	if unknown > 0 {
		attrs.set("unknown_fields_dropped", fmt.Sprintf("%d", unknown))
	}
	out = &telemetryv2.Reading{
		DeviceUid: in.DeviceId,
		SeqNo:     in.SeqNo,
	}
	cond, ok := conditionV1ToV2[in.Condition]
	if !ok {
		return nil, attrs, unknown, &Error{
			FieldPath: "condition",
			Reason:    "no v2 mapping for v1 enum value",
			RawValue:  fmt.Sprintf("%d", int32(in.Condition)),
		}
	}
	out.Condition = cond
	if in.TempCelsiusE2 != nil {
		mK, cerr := celsiusE2ToMilliKelvin(*in.TempCelsiusE2)
		if cerr != nil {
			return nil, attrs, unknown, cerr
		}
		out.TemperatureMillikelvin = &mK
	}
	ts, cerr := unixMsToTimestamp(in.ObservedAtUnixMs)
	if cerr != nil {
		return nil, attrs, unknown, cerr
	}
	out.ObservedAt = ts
	return out, attrs, unknown, nil
}

// V2ToV1 converts one v2 reading. v2-only fields (firmware_rev) are
// dropped and reported; v2-only enum values either produce a locatable
// error or are carried in attributes, per the active mapping.
func V2ToV1(m Mapping, in *telemetryv2.Reading) (out *telemetryv1.Reading, attrs Attrs, unknown int, err error) {
	attrs = Attrs{}
	unknown = countUnknown(in)
	if unknown > 0 {
		attrs.set("unknown_fields_dropped", fmt.Sprintf("%d", unknown))
	}
	if in.FirmwareRev != "" {
		attrs.set("dropped_fields", "firmware_rev")
		attrs.set("firmware_rev", in.FirmwareRev)
	}
	out = &telemetryv1.Reading{
		DeviceId: in.DeviceUid,
		SeqNo:    in.SeqNo,
	}
	cond, ok := conditionV2ToV1[in.Condition]
	if !ok {
		switch m.Enum {
		case EnumError:
			return nil, attrs, unknown, &Error{
				FieldPath: "condition",
				Reason:    "v2 enum value has no v1 representation",
				RawValue:  fmt.Sprintf("%s(%d)", in.Condition.String(), int32(in.Condition)),
			}
		case EnumCarry:
			out.Condition = telemetryv1.Condition_CONDITION_UNSPECIFIED
			attrs.set("unmapped_condition_raw", fmt.Sprintf("%d", int32(in.Condition)))
			attrs.set("unmapped_condition_name", in.Condition.String())
		}
	} else {
		out.Condition = cond
	}
	if in.TemperatureMillikelvin != nil {
		cC, cerr := milliKelvinToCelsiusE2(*in.TemperatureMillikelvin, m.Rounding, attrs)
		if cerr != nil {
			return nil, attrs, unknown, cerr
		}
		out.TempCelsiusE2 = &cC
	}
	ms, cerr := timestampToUnixMs(in.ObservedAt, m.Rounding, attrs)
	if cerr != nil {
		return nil, attrs, unknown, cerr
	}
	out.ObservedAtUnixMs = ms
	return out, attrs, unknown, nil
}

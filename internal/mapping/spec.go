// Package mapping defines the explicit mapping specification between
// telemetry schema versions and performs field renames, integer unit
// conversions with overflow/rounding checks, and enum translation.
package mapping

import (
	"encoding/json"
	"errors"
	"fmt"
)

// RoundingPolicy decides what happens when an exact integer conversion would
// require discarding a non-zero remainder.
type RoundingPolicy string

const (
	// RoundingReject fails the conversion with ROUNDING_REQUIRED (default).
	RoundingReject RoundingPolicy = "REJECT"
	// RoundingNearest rounds half away from zero.
	RoundingNearest RoundingPolicy = "NEAREST"
	// RoundingFloor rounds toward negative infinity.
	RoundingFloor RoundingPolicy = "FLOOR"
)

// UnmappableEnumPolicy decides what happens when a source enum value has no
// target representation (only possible v2 -> v1, e.g. PHASE_MAINTENANCE).
type UnmappableEnumPolicy string

const (
	// EnumPolicyError returns a locatable ENUM_UNMAPPABLE error carrying the
	// original numeric value (default; never silently degrades to 0).
	EnumPolicyError UnmappableEnumPolicy = "ERROR"
	// EnumPolicyCarry emits the default enum (0) but preserves the original
	// numeric value on the output record (raw_phase) and on provenance.
	EnumPolicyCarry UnmappableEnumPolicy = "CARRY_ORIGINAL"
)

// LinearIntUnit converts a value with an explicit integer affine transform:
//
//	out = round_div((in + pre_add) * num, den) + post_add
//
// Specifying integers explicitly (rather than float multipliers) keeps every
// unit conversion exact and auditable. pre_add/post_add cover offset units
// such as Celsius/Kelvin (273.15 K); both are optional and default to zero.
type LinearIntUnit struct {
	Num     int64          `json:"num"`
	Den     int64          `json:"den"`
	PreAdd  int64          `json:"pre_add,omitempty"`
	PostAdd int64          `json:"post_add,omitempty"`
	Mode    RoundingPolicy `json:"mode"`
}

// FieldMapping is one explicit field rename/convert rule.
type FieldMapping struct {
	From string         `json:"from"`
	To   string         `json:"to"`
	Kind string         `json:"kind"` // "identity" | "int_unit"
	Unit *LinearIntUnit `json:"unit,omitempty"`
}

// EnumMapping is an explicit numeric enum table plus behaviour for values
// absent from the table.
type EnumMapping struct {
	From      string               `json:"from"`
	To        string               `json:"to"`
	Values    map[int64]int64      `json:"values"` // source number -> target number
	OnMissing UnmappableEnumPolicy `json:"on_missing"`
}

// Spec is the full explicit mapping between two schema versions.
// Direction is "v1_v2" (v1 -> v2) or "v2_v1".
type Spec struct {
	Name          string         `json:"name"`
	Direction     string         `json:"direction"`
	SourceVersion string         `json:"source_version"`
	TargetVersion string         `json:"target_version"`
	Fields        []FieldMapping `json:"fields"`
	Enums         []EnumMapping  `json:"enums"`
	// HMAC keys per version. The inbound signature is verified with the
	// source-version key; the outbound record is signed with the target key.
	HMACKeyV1 string `json:"hmac_key_v1"`
	HMACKeyV2 string `json:"hmac_key_v2"`
}

const (
	DirectionV1ToV2 = "v1_v2"
	DirectionV2ToV1 = "v2_v1"
)

func ParseSpec(raw []byte) (*Spec, error) {
	var s Spec
	if err := json.Unmarshal(raw, &s); err != nil {
		return nil, fmt.Errorf("invalid mapping spec json: %w", err)
	}
	if err := s.Validate(); err != nil {
		return nil, err
	}
	return &s, nil
}

func (s *Spec) Validate() error {
	if s.Name == "" {
		return errors.New("mapping spec: name is required")
	}
	if s.Direction != DirectionV1ToV2 && s.Direction != DirectionV2ToV1 {
		return fmt.Errorf("mapping spec: direction must be %q or %q, got %q",
			DirectionV1ToV2, DirectionV2ToV1, s.Direction)
	}
	if s.SourceVersion == "" || s.TargetVersion == "" {
		return errors.New("mapping spec: source_version and target_version are required")
	}
	for i, f := range s.Fields {
		switch f.Kind {
		case "identity":
			if f.Unit != nil {
				return fmt.Errorf("mapping spec: field %d (%s): identity field must not carry a unit", i, f.From)
			}
		case "int_unit":
			if f.Unit == nil {
				return fmt.Errorf("mapping spec: field %d (%s): int_unit field needs a unit", i, f.From)
			}
			if f.Unit.Num <= 0 || f.Unit.Den <= 0 {
				return fmt.Errorf("mapping spec: field %d (%s): unit num/den must be positive", i, f.From)
			}
			switch f.Unit.Mode {
			case RoundingReject, RoundingNearest, RoundingFloor:
			default:
				return fmt.Errorf("mapping spec: field %d (%s): unknown rounding mode %q", i, f.From, f.Unit.Mode)
			}
		default:
			return fmt.Errorf("mapping spec: field %d: unknown kind %q", i, f.Kind)
		}
	}
	for i, e := range s.Enums {
		if len(e.Values) == 0 {
			return fmt.Errorf("mapping spec: enum %d requires explicit values", i)
		}
		switch e.OnMissing {
		case EnumPolicyError, EnumPolicyCarry:
		default:
			return fmt.Errorf("mapping spec: enum %d: unknown on_missing policy %q", i, e.OnMissing)
		}
	}
	return nil
}

func (s *Spec) JSON() []byte {
	b, _ := json.MarshalIndent(s, "", "  ")
	return b
}

// EnumByName finds an enum mapping by source field name.
func (s *Spec) EnumByName(from string) (*EnumMapping, bool) {
	for i := range s.Enums {
		if s.Enums[i].From == from {
			return &s.Enums[i], true
		}
	}
	return nil, false
}

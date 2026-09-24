package mapping

// SeedSpecs are the mappings provisioned on first startup. Activating another
// stored mapping hot-switches conversion behaviour without redeploying.
//
// Unit derivation (all integers, exact):
//
//	v1 timestamp_ms -> v2 observed_at_ns : ns = ms * 1_000_000
//	v2 observed_at_ns -> v1 timestamp_ms : ms = ns / 1_000_000
//
//	v1 temp_milli_c (1e-3 °C) -> v2 temperature_uk (1e-6 K):
//	  uk = mC * 1000 + 273_150_000
//	    (mC*1e-3 = °C; K = °C + 273.15; in micro-K the offset is 273.15e6)
//	v2 temperature_uk -> v1 temp_milli_c:
//	  mC = (uk - 273_150_000) / 1000
//
//	battery_milli_pct (1e-3 %) -> battery_pct_micro (1e-6 %): micro = milli*1000
func SeedSpecs() []Spec {
	strictV1V2 := Spec{
		Name:          "strict-v1-v2",
		Direction:     DirectionV1ToV2,
		SourceVersion: "v1",
		TargetVersion: "v2",
		Fields: []FieldMapping{
			{From: "device_id", To: "device_id", Kind: "identity"},
			{From: "labels", To: "labels", Kind: "identity"},
			{From: "request_id", To: "trace_id", Kind: "identity"},
			{From: "timestamp_ms", To: "observed_at_ns", Kind: "int_unit",
				Unit: &LinearIntUnit{Num: 1_000_000, Den: 1, Mode: RoundingReject}},
			{From: "temp_milli_c", To: "temperature_uk", Kind: "int_unit",
				Unit: &LinearIntUnit{Num: 1000, Den: 1, PostAdd: 273_150_000, Mode: RoundingReject}},
			{From: "battery_milli_pct", To: "battery_pct_micro", Kind: "int_unit",
				Unit: &LinearIntUnit{Num: 1000, Den: 1, Mode: RoundingReject}},
		},
		Enums: []EnumMapping{{
			From: "status", To: "phase", OnMissing: EnumPolicyError,
			Values: map[int64]int64{0: 0, 1: 1, 2: 2, 3: 3},
		}},
		HMACKeyV1: DefaultHMACKey,
		HMACKeyV2: DefaultHMACKey,
	}
	strictV2V1 := Spec{
		Name:          "strict-v2-v1",
		Direction:     DirectionV2ToV1,
		SourceVersion: "v2",
		TargetVersion: "v1",
		Fields: []FieldMapping{
			{From: "device_id", To: "device_id", Kind: "identity"},
			{From: "labels", To: "labels", Kind: "identity"},
			{From: "trace_id", To: "request_id", Kind: "identity"},
			{From: "observed_at_ns", To: "timestamp_ms", Kind: "int_unit",
				Unit: &LinearIntUnit{Num: 1, Den: 1_000_000, Mode: RoundingReject}},
			{From: "temperature_uk", To: "temp_milli_c", Kind: "int_unit",
				Unit: &LinearIntUnit{Num: 1, Den: 1000, PreAdd: -273_150_000, Mode: RoundingReject}},
			{From: "battery_pct_micro", To: "battery_milli_pct", Kind: "int_unit",
				Unit: &LinearIntUnit{Num: 1, Den: 1000, Mode: RoundingReject}},
		},
		// 4 (PHASE_MAINTENANCE) deliberately absent -> ENUM_UNMAPPABLE.
		Enums: []EnumMapping{{
			From: "phase", To: "status", OnMissing: EnumPolicyError,
			Values: map[int64]int64{0: 0, 1: 1, 2: 2, 3: 3},
		}},
		HMACKeyV1: DefaultHMACKey,
		HMACKeyV2: DefaultHMACKey,
	}

	lenientV2V1 := strictV2V1
	lenientV2V1.Name = "lenient-v2-v1"
	lenientV2V1.Fields = []FieldMapping{}
	for _, f := range strictV2V1.Fields {
		cp := f
		if f.Unit != nil {
			u := *f.Unit
			u.Mode = RoundingNearest
			cp.Unit = &u
		}
		lenientV2V1.Fields = append(lenientV2V1.Fields, cp)
	}
	lenientV2V1.Enums = []EnumMapping{{
		From: "phase", To: "status", OnMissing: EnumPolicyCarry,
		Values: map[int64]int64{0: 0, 1: 1, 2: 2, 3: 3},
	}}

	return []Spec{strictV1V2, strictV2V1, lenientV2V1}
}

// DefaultHMACKey is the demo key shared by both seed mappings. Override in
// production by activating a mapping spec with distinct keys.
const DefaultHMACKey = "compgw-dev-shared-hmac-key-v1"

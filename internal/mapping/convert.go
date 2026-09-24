package mapping

import (
	"fmt"
	"math/big"
)

// FailureCode mirrors gateway.v1.ConversionError.Code.
type FailureCode string

const (
	FailEnumOverflow     FailureCode = "ENUM_UNMAPPABLE"
	FailIntegerOverflow  FailureCode = "INTEGER_OVERFLOW"
	FailRoundingRequired FailureCode = "ROUNDING_REQUIRED"
	FailSignatureInvalid FailureCode = "SIGNATURE_INVALID"
)

// Failure is a locatable conversion failure: it names the exact field and
// carries context such as the original enum number. It never substitutes a
// silent default.
type Failure struct {
	Code         FailureCode
	Field        string
	Message      string
	RecordID     string
	OriginalEnum *int64
}

func (f *Failure) Error() string {
	if f.RecordID != "" {
		return fmt.Sprintf("conversion %s on field %q of record %q: %s", f.Code, f.Field, f.RecordID, f.Message)
	}
	return fmt.Sprintf("conversion %s on field %q: %s", f.Code, f.Field, f.Message)
}

var (
	minInt64 = big.NewInt(-1 << 63)
	maxInt64 = new(big.Int).Sub(new(big.Int).Lsh(big.NewInt(1), 63), big.NewInt(1))
)

// ConvertInt applies the explicit affine transform
//
//	round_div((in + preAdd) * num, den) + postAdd
//
// enforcing the rounding policy and int64 range at every stage. Negative
// inputs are handled correctly for all policies (NEAREST rounds half away
// from zero; FLOOR toward -inf).
func ConvertInt(in, num, den, preAdd, postAdd int64, mode RoundingPolicy, field string) (int64, error) {
	if den <= 0 || num <= 0 {
		return 0, &Failure{Code: FailIntegerOverflow, Field: field,
			Message: fmt.Sprintf("unit multiplier num=%d den=%d must be positive", num, den)}
	}
	x, ok := addChecked(in, preAdd)
	if !ok {
		return 0, &Failure{Code: FailIntegerOverflow, Field: field,
			Message: fmt.Sprintf("pre_add overflow: %d + %d", in, preAdd)}
	}
	xmul := new(big.Int).Mul(big.NewInt(x), big.NewInt(num))

	// Detect remainder before division. We work with absolute values to make
	// rounding under negative values explicit.
	absNum := new(big.Int).Abs(xmul)
	absDen := big.NewInt(den)
	qAbs := new(big.Int).Quo(absNum, absDen)
	remAbs := new(big.Int).Rem(absNum, absDen)
	hasRem := remAbs.Sign() != 0
	negative := xmul.Sign() < 0

	if hasRem && mode == RoundingReject {
		return 0, &Failure{Code: FailRoundingRequired, Field: field,
			Message: fmt.Sprintf("exact conversion of %d (affine num=%d den=%d pre_add=%d) requires rounding; policy REJECT",
				in, num, den, preAdd)}
	}

	q := qAbs
	if negative {
		q = new(big.Int).Neg(qAbs)
		switch {
		case hasRem && mode == RoundingNearest:
			// half away from zero: negative halves go more negative
			if roundAway(remAbs, absDen) {
				q.Sub(q, big.NewInt(1))
			}
		case hasRem && mode == RoundingFloor:
			q.Sub(q, big.NewInt(1)) // truncation was toward zero; floor is one lower
		}
	} else if hasRem && mode == RoundingNearest {
		if roundAway(remAbs, absDen) {
			q = new(big.Int).Add(qAbs, big.NewInt(1))
		}
	}

	if q.Cmp(minInt64) < 0 || q.Cmp(maxInt64) > 0 {
		return 0, &Failure{Code: FailIntegerOverflow, Field: field,
			Message: fmt.Sprintf("converted value %s exceeds int64 range", q.String())}
	}

	// Apply post_add in arbitrary precision and only narrow at the end, so an
	// overflow here cannot wrap around into an in-range int64.
	q.Add(q, big.NewInt(postAdd))
	if q.Cmp(minInt64) < 0 || q.Cmp(maxInt64) > 0 {
		return 0, &Failure{Code: FailIntegerOverflow, Field: field,
			Message: fmt.Sprintf("post_add overflow: %s exceeds int64 range", q.String())}
	}
	return q.Int64(), nil
}

func addChecked(a, b int64) (int64, bool) {
	r := a + b
	if (r > a) != (b > 0) && b != 0 {
		return 0, false
	}
	return r, true
}

// roundAway reports whether, given remainder r and denominator d (both > 0),
// the magnitude should be rounded away from zero: 2*r >= d (half rounds away).
func roundAway(r, d *big.Int) bool {
	return new(big.Int).Mul(r, big.NewInt(2)).Cmp(d) >= 0
}

// MapEnumField looks up the enum mapping whose source field is `from` and
// translates value through its explicit table.
func MapEnumField(s *Spec, from string, value int64, recordID string) (int64, *int64, error) {
	m, ok := s.EnumByName(from)
	if !ok {
		return 0, nil, &Failure{Field: from, RecordID: recordID,
			Message: fmt.Sprintf("no enum mapping for field %q", from)}
	}
	return MapEnum(m, value, recordID)
}

// MapEnum translates an enum number through an explicit table. On a missing
// entry it either fails with a locatable ENUM_UNMAPPABLE failure carrying the
// original value, or returns 0 plus the carried original value. It never maps
// an unmapped value to 0 without saying so.
func MapEnum(m *EnumMapping, value int64, recordID string) (mapped int64, carried *int64, err error) {
	if out, ok := m.Values[value]; ok {
		return out, nil, nil
	}
	switch m.OnMissing {
	case EnumPolicyCarry:
		v := value
		return 0, &v, nil
	default:
		return 0, nil, &Failure{
			Code:         FailEnumOverflow,
			Field:        m.From,
			RecordID:     recordID,
			OriginalEnum: &value,
			Message:      fmt.Sprintf("enum value %d on %q has no mapping to target schema", value, m.From),
		}
	}
}

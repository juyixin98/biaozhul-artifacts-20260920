// Package histogram implements cumulative-bucket histograms with +Inf
// support, layout compatibility checks, coarsening (boundary contraction),
// merging and quantile interval estimation.
package histogram

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strconv"
)

// Sentinel errors. Callers can use errors.Is to detect them.
var (
	// ErrInvalidHistogram is the root error for structural validation failures.
	ErrInvalidHistogram = errors.New("invalid histogram")
	// ErrIncompatibleLayout means two histograms have different bucket layouts
	// and direct (index-aligned) merging is impossible.
	ErrIncompatibleLayout = errors.New("incompatible bucket layout")
	// ErrNoCommonBounds means the layouts share no finite upper bound, so
	// contraction to a common coarse layout is impossible.
	ErrNoCommonBounds = errors.New("no common finite bounds")
	// ErrNotAContract means a requested target boundary set is not a subset
	// of the histogram's own boundaries.
	ErrNotAContract = errors.New("target bounds are not a contract (subset)")
	// ErrEmptyHistogram means a quantile was requested on zero observations.
	ErrEmptyHistogram = errors.New("empty histogram: no observations")
	// ErrInvalidQuantile means q is outside [0,1].
	ErrInvalidQuantile = errors.New("invalid quantile: q must be in [0,1]")
)

// Bound is a bucket upper boundary. A boundary is either finite or +Inf.
// Every well-formed histogram ends with a +Inf bucket.
type Bound struct {
	Value float64
	Inf   bool
}

// FiniteBound returns a finite boundary.
func FiniteBound(v float64) Bound { return Bound{Value: v} }

// InfBound returns the +Inf boundary.
func InfBound() Bound { return Bound{Inf: true} }

// IsInf reports whether b is +Inf.
func (b Bound) IsInf() bool { return b.Inf }

// Cmp returns -1/0/1 comparing boundaries; +Inf sorts last.
func (b Bound) Cmp(o Bound) int {
	switch {
	case b.Inf && o.Inf:
		return 0
	case b.Inf:
		return 1
	case o.Inf:
		return -1
	case b.Value < o.Value:
		return -1
	case b.Value > o.Value:
		return 1
	default:
		return 0
	}
}

func (b Bound) String() string {
	if b.Inf {
		return "+Inf"
	}
	return strconv.FormatFloat(b.Value, 'g', -1, 64)
}

// MarshalJSON encodes +Inf as the string "+Inf" and finite bounds as numbers.
func (b Bound) MarshalJSON() ([]byte, error) {
	if b.Inf {
		return []byte(`"+Inf"`), nil
	}
	if math.IsNaN(b.Value) {
		return nil, fmt.Errorf("cannot marshal NaN bound")
	}
	return json.Marshal(b.Value)
}

// UnmarshalJSON accepts numbers, "+Inf" (case-insensitive) or numeric strings.
func (b *Bound) UnmarshalJSON(data []byte) error {
	var s string
	if err := json.Unmarshal(data, &s); err == nil {
		switch s {
		case "+Inf", "+inf", "inf", "Inf":
			b.Inf = true
			b.Value = 0
			return nil
		}
		v, err := strconv.ParseFloat(s, 64)
		if err != nil {
			return fmt.Errorf("bound %q: %w", s, err)
		}
		if math.IsInf(v, 1) {
			b.Inf = true
			b.Value = 0
			return nil
		}
		if math.IsNaN(v) || math.IsInf(v, -1) {
			return fmt.Errorf("bound %q must be finite or +Inf", s)
		}
		b.Value = v
		return nil
	}
	var f float64
	if err := json.Unmarshal(data, &f); err != nil {
		return fmt.Errorf("bound must be a number or \"+Inf\": %w", err)
	}
	if math.IsInf(f, 1) {
		b.Inf = true
		b.Value = 0
		return nil
	}
	if math.IsNaN(f) || math.IsInf(f, -1) {
		return errors.New("bound must be finite or +Inf")
	}
	b.Value = f
	return nil
}

// Bucket is one cumulative bucket: CumulativeCount counts all observations
// with value <= Upper. Buckets are cumulative (Prometheus convention).
type Bucket struct {
	Upper           Bound  `json:"upper"`
	CumulativeCount uint64 `json:"cumulative_count"`
}

// Histogram is a cumulative-bucket histogram.
type Histogram struct {
	TotalCount uint64   `json:"total_count"` // total number of observations
	Sum        float64  `json:"sum"`         // sum of all observed values
	Buckets    []Bucket `json:"buckets"`     // sorted ascending by Upper, last one +Inf
}

// IsEmpty reports whether the histogram carries no observations.
// An empty histogram is the additive identity in merging.
func (h *Histogram) IsEmpty() bool { return h.TotalCount == 0 }

// NormalizedCopy returns a copy whose buckets are sorted ascending by Upper.
// It does not validate the contents.
func (h *Histogram) NormalizedCopy() *Histogram {
	cp := &Histogram{TotalCount: h.TotalCount, Sum: h.Sum, Buckets: make([]Bucket, len(h.Buckets))}
	copy(cp.Buckets, h.Buckets)
	sort.SliceStable(cp.Buckets, func(i, j int) bool {
		return cp.Buckets[i].Upper.Cmp(cp.Buckets[j].Upper) < 0
	})
	return cp
}

// Validate checks structural integrity of a cumulative histogram:
//   - Sum must not be NaN;
//   - there must be at least one bucket and the last upper bound must be +Inf;
//   - upper bounds must be strictly increasing;
//   - cumulative counts must be monotonically non-decreasing;
//   - the +Inf bucket count must equal TotalCount (count conservation);
//   - an empty histogram (TotalCount == 0) may only contain zero counts.
//
// All found problems are joined into one error wrapping ErrInvalidHistogram.
func (h *Histogram) Validate() error {
	var errs []error
	if math.IsNaN(h.Sum) {
		errs = append(errs, errors.New("sum is NaN"))
	}
	if h.TotalCount == 0 {
		for i, bk := range h.Buckets {
			if bk.CumulativeCount != 0 {
				errs = append(errs, fmt.Errorf("empty histogram (total_count=0) but bucket %d (%s) has count %d",
					i, bk.Upper, bk.CumulativeCount))
			}
		}
	}
	if len(h.Buckets) == 0 {
		errs = append(errs, errors.New("histogram must contain at least the +Inf bucket"))
		return errors.Join(append([]error{ErrInvalidHistogram}, errs...)...)
	}
	sorted := h.NormalizedCopy()
	for i := 1; i < len(sorted.Buckets); i++ {
		if sorted.Buckets[i].Upper.Cmp(sorted.Buckets[i-1].Upper) == 0 {
			errs = append(errs, fmt.Errorf("duplicate upper bound %s", sorted.Buckets[i].Upper))
		}
	}
	if !sorted.Buckets[len(sorted.Buckets)-1].Upper.IsInf() {
		errs = append(errs, errors.New("last bucket upper bound must be +Inf"))
	}
	for i := 1; i < len(sorted.Buckets); i++ {
		if sorted.Buckets[i].CumulativeCount < sorted.Buckets[i-1].CumulativeCount {
			errs = append(errs, fmt.Errorf("cumulative counts decrease: %s=%d > %s=%d",
				sorted.Buckets[i-1].Upper, sorted.Buckets[i-1].CumulativeCount,
				sorted.Buckets[i].Upper, sorted.Buckets[i].CumulativeCount))
		}
	}
	inf := sorted.Buckets[len(sorted.Buckets)-1]
	if inf.Upper.IsInf() && inf.CumulativeCount != h.TotalCount {
		errs = append(errs, fmt.Errorf("+Inf bucket count %d != total_count %d", inf.CumulativeCount, h.TotalCount))
	}
	if len(errs) == 0 {
		return nil
	}
	return errors.Join(append([]error{ErrInvalidHistogram}, errs...)...)
}

// Bounds returns the sorted upper bounds of the histogram.
func (h *Histogram) Bounds() []Bound {
	out := make([]Bound, len(h.Buckets))
	for i, bk := range h.NormalizedCopy().Buckets {
		out[i] = bk.Upper
	}
	return out
}

// CountAt returns the cumulative count at boundary ub; false if ub is absent.
func (h *Histogram) CountAt(ub Bound) (uint64, bool) {
	return h.countAt(ub)
}

// countAt returns the cumulative count at boundary ub; false if ub is absent.
func (h *Histogram) countAt(ub Bound) (uint64, bool) {
	for _, bk := range h.Buckets {
		if bk.Upper.Cmp(ub) == 0 {
			return bk.CumulativeCount, true
		}
	}
	return 0, false
}

// hasBound reports whether the histogram carries bucket ub.
func (h *Histogram) hasBound(ub Bound) bool {
	_, ok := h.countAt(ub)
	return ok
}

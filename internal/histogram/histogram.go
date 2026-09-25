// Package histogram implements cumulative-bucket histograms and the
// boundary-merge rules for the observability backend.
//
// A histogram is a set of cumulative buckets: Bounds[i] is the inclusive
// upper bound of bucket i and Counts[i] is the number of observations with
// value <= Bounds[i]. The last bound must be +Inf, so the last count is the
// total number of observations.
//
// Merge rules:
//   - identical boundaries  -> counts are added directly;
//   - different boundaries  -> both histograms are shrunk to the common
//     (coarser) boundary set, i.e. the intersection of the two bound lists,
//     then added. Shrinking a cumulative histogram to a subset of its
//     boundaries is lossless. Refinement (splitting buckets) is never
//     attempted, because per-bucket distributions are unknown.
package histogram

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
)

// Histogram is a cumulative-bucket histogram.
type Histogram struct {
	Name   string    `json:"name"`
	Bounds []float64 `json:"bounds"` // inclusive upper bounds, last must be +Inf
	Counts []uint64  `json:"counts"` // cumulative counts, len(Counts) == len(Bounds)
}

// Float is a float64 whose JSON form supports ±Inf as the strings
// "+Inf"/"-Inf" (plain JSON cannot represent infinities).
type Float float64

// UnmarshalJSON accepts a JSON number or one of the strings
// "+Inf", "Inf", "Infinity", "-Inf", "-Infinity".
func (f *Float) UnmarshalJSON(data []byte) error {
	if len(data) > 0 && data[0] == '"' {
		var s string
		if err := json.Unmarshal(data, &s); err != nil {
			return err
		}
		switch s {
		case "+Inf", "Inf", "+Infinity", "Infinity":
			*f = Float(math.Inf(1))
		case "-Inf", "-Infinity":
			*f = Float(math.Inf(-1))
		default:
			return fmt.Errorf("invalid bound string %q", s)
		}
		return nil
	}
	var v float64
	if err := json.Unmarshal(data, &v); err != nil {
		return err
	}
	*f = Float(v)
	return nil
}

// MarshalJSON emits a number, or "+Inf"/"-Inf" for infinities.
func (f Float) MarshalJSON() ([]byte, error) {
	v := float64(f)
	switch {
	case math.IsInf(v, 1):
		return []byte(`"+Inf"`), nil
	case math.IsInf(v, -1):
		return []byte(`"-Inf"`), nil
	case math.IsNaN(v):
		return nil, errors.New("NaN is not representable in JSON")
	}
	return json.Marshal(v)
}

type histogramJSON struct {
	Name   string   `json:"name"`
	Bounds []Float  `json:"bounds"`
	Counts []uint64 `json:"counts"`
}

// MarshalJSON encodes bounds with ±Inf support.
func (h Histogram) MarshalJSON() ([]byte, error) {
	bounds := make([]Float, len(h.Bounds))
	for i, b := range h.Bounds {
		bounds[i] = Float(b)
	}
	return json.Marshal(histogramJSON{Name: h.Name, Bounds: bounds, Counts: h.Counts})
}

// UnmarshalJSON decodes bounds with ±Inf support.
func (h *Histogram) UnmarshalJSON(data []byte) error {
	var aux histogramJSON
	if err := json.Unmarshal(data, &aux); err != nil {
		return err
	}
	h.Name = aux.Name
	h.Counts = aux.Counts
	h.Bounds = make([]float64, len(aux.Bounds))
	for i, b := range aux.Bounds {
		h.Bounds[i] = float64(b)
	}
	return nil
}

// Validation errors.
var (
	ErrNoBuckets       = errors.New("histogram has no buckets")
	ErrCountLen        = errors.New("counts length must equal bounds length")
	ErrBoundsNotSorted = errors.New("bounds must be strictly increasing")
	ErrMissingInf      = errors.New("last bound must be +Inf")
	ErrCountsNotMono   = errors.New("cumulative counts must be non-decreasing")
	ErrNoCommonBounds  = errors.New("histograms share no common boundaries")
	ErrEmptyHistogram  = errors.New("histogram is empty (total count is zero)")
	ErrBadQuantile     = errors.New("quantile must be in [0, 1]")
)

// Validate checks the structural invariants of a cumulative histogram:
// at least one bucket, equal-length slices, strictly increasing bounds
// ending at +Inf, and monotonically non-decreasing cumulative counts.
// Because the last bound is +Inf, the last cumulative count IS the total,
// so total consistency is implied by the monotonicity check.
func (h *Histogram) Validate() error {
	if len(h.Bounds) == 0 {
		return ErrNoBuckets
	}
	if len(h.Counts) != len(h.Bounds) {
		return fmt.Errorf("%w: %d counts vs %d bounds", ErrCountLen, len(h.Counts), len(h.Bounds))
	}
	for i, b := range h.Bounds {
		if math.IsNaN(b) || (math.IsInf(b, 0) && !math.IsInf(b, 1)) {
			return fmt.Errorf("%w: bound %d is %v", ErrBoundsNotSorted, i, b)
		}
		if i > 0 && !(b > h.Bounds[i-1]) {
			return fmt.Errorf("%w: bounds[%d]=%v <= bounds[%d]=%v", ErrBoundsNotSorted, i, b, i-1, h.Bounds[i-1])
		}
	}
	if !math.IsInf(h.Bounds[len(h.Bounds)-1], 1) {
		return ErrMissingInf
	}
	for i := 1; i < len(h.Counts); i++ {
		if h.Counts[i] < h.Counts[i-1] {
			return fmt.Errorf("%w: counts[%d]=%d < counts[%d]=%d", ErrCountsNotMono, i, h.Counts[i], i-1, h.Counts[i-1])
		}
	}
	return nil
}

// Total returns the total number of observations (cumulative count at +Inf).
func (h *Histogram) Total() uint64 {
	if len(h.Counts) == 0 {
		return 0
	}
	return h.Counts[len(h.Counts)-1]
}

// Clone returns a deep copy.
func (h *Histogram) Clone() *Histogram {
	c := &Histogram{Name: h.Name, Bounds: make([]float64, len(h.Bounds)), Counts: make([]uint64, len(h.Counts))}
	copy(c.Bounds, h.Bounds)
	copy(c.Counts, h.Counts)
	return c
}

func sameBounds(a, b []float64) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// commonBounds returns the sorted intersection of two bound lists.
// Both lists end at +Inf, so the result is never empty.
func commonBounds(a, b []float64) []float64 {
	set := make(map[float64]struct{}, len(a))
	for _, v := range a {
		set[v] = struct{}{}
	}
	var out []float64
	for _, v := range b {
		if _, ok := set[v]; ok {
			out = append(out, v)
		}
	}
	sort.Float64s(out)
	return out
}

// Rebin shrinks a cumulative histogram to a coarser boundary set.
// Every target bound must exist in the source; the cumulative count at a
// coarser bound is read off directly, so the operation is lossless and
// conserves the total.
func (h *Histogram) Rebin(target []float64) (*Histogram, error) {
	idx := make(map[float64]int, len(h.Bounds))
	for i, b := range h.Bounds {
		idx[b] = i
	}
	out := &Histogram{Name: h.Name, Bounds: append([]float64(nil), target...), Counts: make([]uint64, len(target))}
	for i, b := range target {
		j, ok := idx[b]
		if !ok {
			return nil, fmt.Errorf("rebin: bound %v not present in source histogram %q", b, h.Name)
		}
		out.Counts[i] = h.Counts[j]
	}
	return out, nil
}

// Merge combines two cumulative histograms into a new one.
//
// If the boundaries are identical the counts are added directly. Otherwise
// both sides are shrunk to their common (coarser) boundaries — the
// intersection of the two bound lists — and then added. The merge never
// invents finer buckets. The result is validated before being returned, and
// its total always equals the sum of the inputs' totals (count conservation).
func Merge(a, b *Histogram) (*Histogram, error) {
	if err := a.Validate(); err != nil {
		return nil, fmt.Errorf("left histogram %q invalid: %w", a.Name, err)
	}
	if err := b.Validate(); err != nil {
		return nil, fmt.Errorf("right histogram %q invalid: %w", b.Name, err)
	}

	ra, rb := a, b
	if !sameBounds(a.Bounds, b.Bounds) {
		common := commonBounds(a.Bounds, b.Bounds)
		if len(common) == 0 {
			return nil, ErrNoCommonBounds // unreachable while +Inf is mandatory, kept for safety
		}
		var err error
		if ra, err = a.Rebin(common); err != nil {
			return nil, err
		}
		if rb, err = b.Rebin(common); err != nil {
			return nil, err
		}
	}

	out := &Histogram{
		Name:   a.Name + "+" + b.Name,
		Bounds: append([]float64(nil), ra.Bounds...),
		Counts: make([]uint64, len(ra.Counts)),
	}
	for i := range out.Counts {
		out.Counts[i] = ra.Counts[i] + rb.Counts[i]
	}
	if err := out.Validate(); err != nil {
		return nil, fmt.Errorf("merged histogram invalid: %w", err)
	}
	if out.Total() != a.Total()+b.Total() {
		return nil, fmt.Errorf("merge violated count conservation: %d != %d+%d", out.Total(), a.Total(), b.Total())
	}
	return out, nil
}

// MergeAll merges a list of histograms left to right.
func MergeAll(hs []*Histogram) (*Histogram, error) {
	switch len(hs) {
	case 0:
		return nil, errors.New("merge: no histograms given")
	case 1:
		return hs[0].Clone(), nil
	}
	acc := hs[0].Clone()
	for _, h := range hs[1:] {
		var err error
		acc, err = Merge(acc, h)
		if err != nil {
			return nil, err
		}
	}
	return acc, nil
}

// QuantileEstimate is the result of a quantile query. Because only
// cumulative bucket counts are known, the true quantile can only be pinned
// to a bucket interval; Estimate is the linear interpolation inside it.
type QuantileEstimate struct {
	Quantile float64 `json:"quantile"`
	Estimate Float   `json:"estimate"` // linear interpolation within the bucket
	Lower    Float   `json:"lower"`    // bucket lower bound (exclusive), -Inf for the first bucket
	Upper    Float   `json:"upper"`    // bucket upper bound (inclusive), +Inf for the last bucket
}

// Quantile estimates the q-quantile. It returns the containing bucket as an
// interval [Lower, Upper] plus a linearly interpolated point estimate.
// For the +Inf bucket the estimate is the bucket's lower bound and Upper is
// +Inf: the true value can be anywhere above it.
func (h *Histogram) Quantile(q float64) (*QuantileEstimate, error) {
	if q < 0 || q > 1 || math.IsNaN(q) {
		return nil, ErrBadQuantile
	}
	if err := h.Validate(); err != nil {
		return nil, err
	}
	total := h.Total()
	if total == 0 {
		return nil, ErrEmptyHistogram
	}

	rank := q * float64(total)
	// First bucket whose cumulative count reaches the rank.
	i := sort.Search(len(h.Counts), func(i int) bool { return float64(h.Counts[i]) >= rank })
	if i == len(h.Counts) {
		i = len(h.Counts) - 1
	}

	lower := math.Inf(-1)
	if i > 0 {
		lower = h.Bounds[i-1]
	}
	upper := h.Bounds[i]

	var prev uint64
	if i > 0 {
		prev = h.Counts[i-1]
	}
	inBucket := h.Counts[i] - prev

	var est float64
	switch {
	case math.IsInf(upper, 1):
		est = lower // cannot interpolate into an open-ended bucket
	case inBucket == 0:
		est = upper
	default:
		frac := (rank - float64(prev)) / float64(inBucket)
		if frac < 0 {
			frac = 0
		}
		if frac > 1 {
			frac = 1
		}
		base := lower
		if math.IsInf(base, -1) {
			base = 0 // first finite bucket: interpolate from 0
		}
		est = base + frac*(upper-base)
	}
	return &QuantileEstimate{Quantile: q, Estimate: Float(est), Lower: Float(lower), Upper: Float(upper)}, nil
}

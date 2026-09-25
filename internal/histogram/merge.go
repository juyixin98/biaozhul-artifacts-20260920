package histogram

import (
	"errors"
	"fmt"
	"math"
)

// EqualLayout reports whether h and o have exactly the same ordered set of
// upper boundaries (including the trailing +Inf bucket). Only histograms
// with equal layouts can be merged by direct index-aligned bucket addition.
func EqualLayout(h, o *Histogram) bool {
	a, b := h.NormalizedCopy(), o.NormalizedCopy()
	if len(a.Buckets) != len(b.Buckets) {
		return false
	}
	for i := range a.Buckets {
		if a.Buckets[i].Upper.Cmp(b.Buckets[i].Upper) != 0 {
			return false
		}
	}
	return true
}

// CommonBounds returns the common *coarse* layout shared by all inputs:
// the intersection of their finite upper bounds, sorted ascending, plus the
// trailing +Inf bucket. Contracting each histogram to this set loses the
// finest boundaries but preserves exact cumulative counts at shared bounds.
// It returns ErrNoCommonBounds when the inputs share no finite bound.
func CommonBounds(hs ...*Histogram) ([]Bound, error) {
	if len(hs) == 0 {
		return nil, errors.New("CommonBounds: no histograms")
	}
	counts := map[Bound]int{}
	for _, h := range hs {
		seen := map[Bound]bool{}
		for _, bk := range h.NormalizedCopy().Buckets {
			if bk.Upper.IsInf() || seen[bk.Upper] {
				continue
			}
			seen[bk.Upper] = true
			counts[bk.Upper]++
		}
	}
	var common []Bound
	for b, n := range counts {
		if n == len(hs) {
			common = append(common, b)
		}
	}
	// Sort deterministically by sorting a normalized throwaway histogram? Keep
	// it simple: sort Bounds directly.
	for i := 1; i < len(common); i++ {
		for j := i; j > 0 && common[j].Cmp(common[j-1]) < 0; j-- {
			common[j], common[j-1] = common[j-1], common[j]
		}
	}
	if len(common) == 0 {
		return nil, ErrNoCommonBounds
	}
	return append(common, InfBound()), nil
}

// Contract returns a coarsened histogram using only targetBounds: the target
// bounds must form a subset (contract) of h's own boundaries, and must
// include +Inf. Cumulative counts at the retained boundaries stay exact;
// information at dropped (finer) boundaries is intentionally discarded.
func (h *Histogram) Contract(targetBounds []Bound) (*Histogram, error) {
	n := h.NormalizedCopy()
	target := make([]Bound, len(targetBounds))
	copy(target, targetBounds)
	for i := 1; i < len(target); i++ {
		for j := i; j > 0 && target[j].Cmp(target[j-1]) < 0; j-- {
			target[j], target[j-1] = target[j-1], target[j]
		}
	}
	if len(target) == 0 || !target[len(target)-1].IsInf() {
		return nil, fmt.Errorf("%w: target must end with +Inf", ErrNotAContract)
	}
	for _, tb := range target {
		if !n.hasBound(tb) {
			return nil, fmt.Errorf("%w: bound %s not present", ErrNotAContract, tb)
		}
	}
	out := &Histogram{TotalCount: n.TotalCount, Sum: n.Sum, Buckets: make([]Bucket, 0, len(target))}
	for _, tb := range target {
		c, _ := n.countAt(tb)
		out.Buckets = append(out.Buckets, Bucket{Upper: tb, CumulativeCount: c})
	}
	return out, nil
}

// MergeResult is the outcome of a merge plus the conservation check.
type MergeResult struct {
	Merged           *Histogram
	Inputs           int
	Coarsened        bool    // true when boundary contraction was applied
	CommonBounds     []Bound // layout used after contraction (nil when direct)
	InputTotalCounts uint64  // sum of input total counts
	Conserved        bool    // merged total == sum of input totals
	BucketConserved  bool    // merged cumulative counts preserved at layout bounds
}

// MergeStrict merges histograms only when their layouts are identical.
// The inputs must already pass Validate. It returns ErrIncompatibleLayout
// otherwise (the caller may retry with MergeCoarsen).
func MergeStrict(hs ...*Histogram) (*MergeResult, error) {
	if len(hs) == 0 {
		return nil, errors.New("MergeStrict: no histograms")
	}
	base := hs[0].NormalizedCopy()
	for _, h := range hs[1:] {
		if !EqualLayout(base, h) {
			return nil, fmt.Errorf("%w: %v vs %v", ErrIncompatibleLayout, base.Bounds(), h.Bounds())
		}
	}
	out := &Histogram{Sum: 0, Buckets: make([]Bucket, len(base.Buckets))}
	for i := range out.Buckets {
		out.Buckets[i].Upper = base.Buckets[i].Upper
	}
	var totals uint64
	for _, h := range hs {
		n := h.NormalizedCopy()
		out.Sum += n.Sum
		totals += n.TotalCount
		for i := range out.Buckets {
			out.Buckets[i].CumulativeCount += n.Buckets[i].CumulativeCount
		}
	}
	out.TotalCount = out.Buckets[len(out.Buckets)-1].CumulativeCount
	return finalize(out, hs, false, nil, totals), nil
}

// MergeCoarsen merges histograms whose layouts differ by first contracting
// every input onto the intersection of their boundaries (the common coarse
// layout). Counts at shared boundaries stay exact; finer boundaries that not
// all inputs share are dropped. Returns ErrNoCommonBounds when no finite
// bound is shared.
func MergeCoarsen(hs ...*Histogram) (*MergeResult, error) {
	if len(hs) == 0 {
		return nil, errors.New("MergeCoarsen: no histograms")
	}
	common, err := CommonBounds(hs...)
	if err != nil {
		return nil, err
	}
	contracted := make([]*Histogram, 0, len(hs))
	var totals uint64
	var sum float64
	coarsened := false
	for _, h := range hs {
		c, err := h.Contract(common)
		if err != nil {
			return nil, err
		}
		if len(c.Buckets) != len(h.NormalizedCopy().Buckets) {
			coarsened = true
		}
		contracted = append(contracted, c)
		totals += h.TotalCount
		sum += h.Sum
	}
	out := &Histogram{Sum: sum, Buckets: make([]Bucket, len(common))}
	for i, b := range common {
		out.Buckets[i].Upper = b
	}
	for _, c := range contracted {
		for i := range out.Buckets {
			out.Buckets[i].CumulativeCount += c.Buckets[i].CumulativeCount
		}
	}
	out.TotalCount = out.Buckets[len(out.Buckets)-1].CumulativeCount
	return finalize(out, hs, coarsened, common, totals), nil
}

// Merge attempts a direct merge and, on incompatible layouts, contracts to
// the common coarse layout and retries. Empty histograms are treated as the
// additive identity and dropped (an all-empty merge yields an empty result
// on the first non-empty input's layout, or a minimal empty histogram).
func Merge(hs ...*Histogram) (*MergeResult, error) {
	if len(hs) == 0 {
		return nil, errors.New("Merge: no histograms")
	}
	var nonEmpty []*Histogram
	var firstLayout *Histogram
	for _, h := range hs {
		if err := h.Validate(); err != nil {
			return nil, err
		}
		if !h.IsEmpty() {
			nonEmpty = append(nonEmpty, h)
		} else if firstLayout == nil {
			firstLayout = h.NormalizedCopy()
		}
	}
	if len(nonEmpty) == 0 {
		// Sum of zeros: reuse a layout if present, else minimal +Inf layout.
		if firstLayout != nil {
			z := &Histogram{Sum: 0, Buckets: make([]Bucket, len(firstLayout.Buckets))}
			for i, bk := range firstLayout.Buckets {
				z.Buckets[i].Upper = bk.Upper
			}
			return finalize(z, hs, false, nil, 0), nil
		}
		z := &Histogram{Buckets: []Bucket{{Upper: InfBound()}}}
		return finalize(z, hs, false, nil, 0), nil
	}
	res, err := MergeStrict(nonEmpty...)
	if err == nil {
		res.Inputs = len(hs)
		return res, nil
	}
	if !errors.Is(err, ErrIncompatibleLayout) {
		return nil, err
	}
	res, err = MergeCoarsen(nonEmpty...)
	if err != nil {
		return nil, err
	}
	res.Inputs = len(hs)
	return res, nil
}

func finalize(out *Histogram, inputs []*Histogram, coarsened bool, common []Bound, totals uint64) *MergeResult {
	conserved := out.TotalCount == totals
	bucketConserved := true
	if !coarsened && len(inputs) > 0 {
		// For a direct merge each output bucket must equal the sum of inputs
		// at the same boundary.
		layout := out.Buckets
		sums := make([]uint64, len(layout))
		for _, h := range inputs {
			n := h.NormalizedCopy()
			for i, bk := range layout {
				if c, ok := n.countAt(bk.Upper); ok {
					sums[i] += c
				} else {
					bucketConserved = false
				}
			}
		}
		for i := range layout {
			if sums[i] != out.Buckets[i].CumulativeCount {
				bucketConserved = false
			}
		}
	} else if coarsened {
		// After contraction, every retained bucket must equal the sum of the
		// input counts at that same shared boundary.
		sums := make(map[Bound]uint64)
		for _, h := range inputs {
			n := h.NormalizedCopy()
			for _, bk := range out.Buckets {
				if c, ok := n.countAt(bk.Upper); ok {
					sums[bk.Upper] += c
				}
			}
		}
		for _, bk := range out.Buckets {
			if sums[bk.Upper] != bk.CumulativeCount {
				bucketConserved = false
			}
		}
	}
	if math.IsNaN(out.Sum) {
		// defensive; validated inputs cannot produce this
		bucketConserved = false
	}
	return &MergeResult{
		Merged:           out,
		Inputs:           len(inputs),
		Coarsened:        coarsened,
		CommonBounds:     common,
		InputTotalCounts: totals,
		Conserved:        conserved,
		BucketConserved:  bucketConserved,
	}
}

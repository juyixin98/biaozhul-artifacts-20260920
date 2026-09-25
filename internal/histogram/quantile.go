package histogram

import "math"

// QuantileEstimate is a quantile estimate with its uncertainty interval.
//
// Point is the Prometheus-style linear interpolation estimate; it is nil when
// no point estimate exists (empty histogram, or rank falls inside the open
// +Inf bucket whose upper edge is unknown).
//
// Lower/Upper bound the bucket enclosing the requested rank. They use nil
// pointers to represent -Inf (rank 0 bucket) / +Inf (open-ended top bucket).
// The width therefore may be unbounded, which is exactly the honest interval
// a cumulative histogram can provide.
type QuantileEstimate struct {
	Q     float64  `json:"q"`
	Rank  float64  `json:"rank"`
	Point *float64 `json:"point"`
	Lower *float64 `json:"lower"`
	Upper *float64 `json:"upper"`
	// FiniteInterval is true only when both edges are finite.
	FiniteInterval bool   `json:"finite_interval"`
	Note           string `json:"note,omitempty"`
}

// EstimateQuantile estimates q in [0,1] from a cumulative histogram.
// Returns ErrEmptyHistogram for zero observations and ErrInvalidQuantile
// for out-of-range q. The returned interval is the enclosing bucket:
//
//   - rank 0 / first bucket (prevCount==0): lower = -Inf (nil);
//   - rank inside the +Inf bucket: upper = +Inf (nil);
//   - q == 1: point = +Inf bucket's lower edge (rank == count).
func EstimateQuantile(h *Histogram, q float64) (QuantileEstimate, error) {
	if q < 0 || q > 1 || math.IsNaN(q) {
		return QuantileEstimate{}, ErrInvalidQuantile
	}
	if h.IsEmpty() {
		return QuantileEstimate{Q: q}, ErrEmptyHistogram
	}
	n := h.NormalizedCopy()
	count := float64(n.TotalCount)
	rank := q * count
	e := QuantileEstimate{Q: q, Rank: rank}

	// q == 1 lives on the lower edge of the +Inf bucket.
	if q == 1 {
		var prevUpper *float64
		// Find the largest finite upper bound; counts before it define prev.
		for i := range n.Buckets {
			bk := n.Buckets[i]
			if bk.Upper.IsInf() {
				break
			}
			v := bk.Upper.Value
			prevUpper = &v
		}
		if prevUpper == nil {
			// Only a +Inf bucket: interval is (-Inf, +Inf).
			e.Note = "all observations lie in the open +Inf bucket"
			return e, nil
		}
		pv := *prevUpper
		e.Point = &pv
		e.Lower = &pv
		e.Upper = nil
		e.Note = "q=1: lower edge of the open +Inf bucket"
		return e, nil
	}

	// Find the first bucket whose cumulative count >= rank.
	var prevCount float64
	var prevUpper *float64 // nil = -Inf
	for i := range n.Buckets {
		bk := n.Buckets[i]
		c := float64(bk.CumulativeCount)
		if c >= rank || bk.Upper.IsInf() {
			if bk.Upper.IsInf() {
				// Rank lands in the open-ended top bucket.
				e.Lower = prevUpper // may be nil (everything in +Inf)
				e.Upper = nil       // +Inf
				if prevUpper != nil && c > prevCount {
					// Cannot interpolate without a finite upper edge; expose
					// no fake point.
					e.Note = "rank falls inside the open +Inf bucket"
				} else if prevUpper == nil {
					e.Note = "all observations lie in the open +Inf bucket"
				}
				return e, nil
			}
			upper := bk.Upper.Value
			if rank == 0 && prevCount == 0 {
				// Prometheus returns the first finite upper bound as the point.
				e.Point = &upper
				e.Lower = nil
				e.Upper = &upper
				return e, nil
			}
			e.Lower = prevUpper
			e.Upper = &upper
			if c > prevCount {
				var lo float64
				if prevUpper != nil {
					lo = *prevUpper
				}
				p := lo + (upper-lo)*(rank-prevCount)/(c-prevCount)
				e.Point = &p
			}
			e.FiniteInterval = e.Lower != nil
			return e, nil
		}
		prevCount = c
		if !bk.Upper.IsInf() {
			v := bk.Upper.Value
			prevUpper = &v
		}
	}
	// Unreachable for a validated histogram.
	return e, nil
}

// EstimateQuantiles estimates several quantiles in one pass of convenience.
func EstimateQuantiles(h *Histogram, qs []float64) ([]QuantileEstimate, error) {
	out := make([]QuantileEstimate, 0, len(qs))
	for _, q := range qs {
		e, err := EstimateQuantile(h, q)
		if err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, nil
}

package sched

import (
	"sort"
	"time"
)

// EarliestFeasible finds the earliest start time s such that:
//
//   - windowStart <= s, and s+duration <= windowEnd (when windowEnd is set);
//   - placing demand of length duration at s never exceeds resource capacity,
//     taking all existing reservations cur into account.
//
// It returns the zero time and false when no such s exists.
//
// Algorithm (half-open intervals). Slice the timeline at every reservation
// boundary; on each piece [u,v) the aggregate load L is constant and the
// placement is feasible there iff L+demand <= capacity in every dimension. A
// failing piece forbids exactly the start positions s for which [s,s+D)
// overlaps [u,v):
//
//	s+D > u  and  s < v   ⇔   s ∈ (u-D, v)   (OPEN on both sides)
//
// Openness encodes half-open adjacency: starting at v (touching the piece's
// end) or at u-D (touching its start) is allowed. Feasible starts are exactly
// the points outside the union of these forbidden ranges; the earliest one is
// windowStart when it is not interior to a range, otherwise the (feasible)
// right endpoint of each successive merged range. Ranges that merely touch at
// a point are NOT merged, because the touching point is feasible.
//
// For an open-ended search the relevant region ends at the latest reservation
// end; beyond it lies an infinite empty piece that only fails when demand
// alone exceeds capacity (e.g. a zero-capacity dimension).
//
// Complexity: O((n+k) log n) for n reservations and k dimensions.
func EarliestFeasible(capacity Dims, cur []existing, windowStart, windowEnd time.Time, duration time.Duration, demand Dims) (time.Time, bool) {
	if windowStart.IsZero() || duration <= 0 {
		return time.Time{}, false
	}

	regionEnd := windowEnd
	openEnded := windowEnd.IsZero()
	if !openEnded {
		latest := windowEnd.Add(-duration)
		if latest.Before(windowStart) {
			return time.Time{}, false
		}
	} else {
		var maxExit time.Time
		for _, j := range cur {
			if j.interval.End.After(maxExit) {
				maxExit = j.interval.End
			}
		}
		regionEnd = maxExit
		if regionEnd.Before(windowStart) {
			regionEnd = windowStart
		}
	}

	// Forbidden open ranges (lo, hi) for start positions s. hi.IsZero() means
	// +infinity (the empty tail fails forever).
	type frange struct{ lo, hi time.Time }
	var forbidden []frange
	for _, piece := range usageSlices(cur, windowStart, regionEnd) {
		var failing []string
		if dimsFit(piece.Load, demand, capacity, &failing) {
			continue
		}
		forbidden = append(forbidden, frange{
			lo: piece.Interval.Start.Add(-duration),
			hi: piece.Interval.End,
		})
	}
	if openEnded && !dimsFit(Dims{}, demand, capacity, nil) {
		// Empty tail [regionEnd, +inf) is over capacity: every start after
		// regionEnd-D is forbidden; s = regionEnd-D is adjacent and feasible.
		forbidden = append(forbidden, frange{lo: regionEnd.Add(-duration), hi: time.Time{}})
	}

	sort.Slice(forbidden, func(i, j int) bool {
		if !forbidden[i].lo.Equal(forbidden[j].lo) {
			return forbidden[i].lo.Before(forbidden[j].lo)
		}
		// finite hi before infinite hi
		return forbidden[j].hi.IsZero() || (!forbidden[i].hi.IsZero() && forbidden[i].hi.Before(forbidden[j].hi))
	})

	contains := func(f frange, t time.Time) bool {
		// open range (lo, hi)
		return f.lo.Before(t) && (f.hi.IsZero() || t.Before(f.hi))
	}

	cand := windowStart
	for _, f := range forbidden {
		// Ranges are open: cand == f.lo is feasible (adjacency), so only a
		// range strictly containing cand can block it.
		if !contains(f, cand) {
			break // all later ranges start even later; cand stands
		}
		if f.hi.IsZero() {
			return time.Time{}, false // blocked forever
		}
		cand = f.hi
	}

	// Merge remaining overlapping ranges starting at/before cand: ranges whose
	// lo is strictly before cand's new value also block it; ranges that only
	// touch at hi leave hi feasible.
	for i := 0; i < len(forbidden); i++ {
		f := forbidden[i]
		if f.hi.IsZero() {
			if f.lo.Before(cand) {
				return time.Time{}, false
			}
			break
		}
		if f.lo.Before(cand) && f.hi.After(cand) {
			cand = f.hi
		}
	}

	if !openEnded {
		latest := windowEnd.Add(-duration)
		if cand.After(latest) {
			return time.Time{}, false
		}
	}
	return cand, true
}

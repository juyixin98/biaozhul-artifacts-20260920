// Package brute is a deliberately simple reference solver used to validate
// package sched: it enumerates every admissible start slot on a discrete
// time grid (the "discrete hour timeline" exhaustive search) and checks
// capacity cell by cell.
package brute

import (
	"sort"
	"time"
)

// Job is one existing reservation on the discrete timeline.
type Job struct {
	ID     string
	Start  int64 // inclusive slot index
	End    int64 // exclusive slot index (half-open)
	Demand map[string]int64
}

// EarliestFeasibleGrid scans every start slot s in
// [windowStart, windowEnd-duration] and returns the first for which, for
// every unit slot in [s, s+duration) and every dimension d,
//
//	sum(demand of jobs covering the slot) + want[d] <= capacity[d].
//
// It returns (0,false) when no slot fits. windowEnd < 0 means an open-ended
// window; then the search only needs to consider slots up through the latest
// job end, since after that load is constant.
func EarliestFeasibleGrid(capacity map[string]int64, jobs []Job, windowStart, windowEnd, duration int64, want map[string]int64) (int64, bool) {
	if duration <= 0 || windowEnd >= 0 && windowEnd-windowStart < duration {
		return 0, false
	}
	limit := windowEnd - duration
	if windowEnd < 0 {
		// Beyond the last job end the timeline is empty, so feasibility no
		// longer changes; always at least check the window start itself.
		limit = lastJobEnd(jobs)
		if limit < windowStart {
			limit = windowStart
		}
	}
	for s := windowStart; s <= limit; s++ {
		if fits(capacity, jobs, s, s+duration, want) {
			return s, true
		}
	}
	return 0, false
}

// FixedFits reports whether placing want on [start,start+duration) is
// feasible.
func FixedFits(capacity map[string]int64, jobs []Job, start, duration int64, want map[string]int64) bool {
	return fits(capacity, jobs, start, start+duration, want)
}

func fits(capacity map[string]int64, jobs []Job, from, to int64, want map[string]int64) bool {
	for t := from; t < to; t++ {
		load := map[string]int64{}
		for _, j := range jobs {
			// half-open: job covers [Start, End)
			if j.Start <= t && t < j.End {
				for k, v := range j.Demand {
					load[k] += v
				}
			}
		}
		for k, v := range want {
			if load[k]+v > capacity[k] {
				return false
			}
		}
	}
	return true
}

func lastJobEnd(jobs []Job) int64 {
	var m int64
	for _, j := range jobs {
		if j.End > m {
			m = j.End
		}
	}
	return m
}

// SlotsUsed returns, for diagnostics, the set of unit slots covered by
// [start,end).
func SlotsUsed(start, end int64) []int64 {
	out := make([]int64, 0, end-start)
	for t := start; t < end; t++ {
		out = append(out, t)
	}
	return out
}

// TimelineSnapshot renders capacity load per slot in [from,to), one sorted
// dimension vector per slot. Handy in test failure messages.
func TimelineSnapshot(capacity map[string]int64, jobs []Job, from, to int64) []map[string]int64 {
	out := make([]map[string]int64, 0, to-from)
	for t := from; t < to; t++ {
		load := map[string]int64{}
		for _, j := range jobs {
			if j.Start <= t && t < j.End {
				for k, v := range j.Demand {
					load[k] += v
				}
			}
		}
		out = append(out, load)
	}
	return out
}

// SortedDims returns dimension names in stable order.
func SortedDims(d map[string]int64) []string {
	keys := make([]string, 0, len(d))
	for k := range d {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// GridTime converts a base time and a unit duration to slot time t0 + n*unit.
func GridTime(base time.Time, unit time.Duration, n int64) time.Time {
	return base.Add(time.Duration(n) * unit)
}

package sched

import (
	"math/rand"
	"testing"
	"time"

	"rsrv/brute"
)

// These tests validate the sweep-based solver against the deliberately simple
// exhaustive solver in package brute on a discrete-hour timeline. Both
// implementations are written independently; agreement over randomized
// scenarios is the acceptance check for the interval/capacity algorithm.

var epoch = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

func hour(n int64) time.Time { return epoch.Add(time.Duration(n) * time.Hour) }

func gridExisting(jobs []brute.Job) ([]existing, map[string]int) {
	idx := map[string]int{}
	out := make([]existing, 0, len(jobs))
	for i, j := range jobs {
		idx[j.ID] = i
		d := Dims{}
		for k, v := range j.Demand {
			d[k] = v
		}
		out = append(out, existing{
			id: j.ID, res: "R",
			interval: Interval{Start: hour(j.Start), End: hour(j.End)},
			demand:   d,
		})
	}
	return out, idx
}

// TestDifferentialEarliest compares EarliestFeasible (sweep) against the
// brute-force grid scan over thousands of random scenarios, including open and
// closed windows, adjacent jobs and multi-dimensional capacities.
func TestDifferentialEarliest(t *testing.T) {
	const iterations = 4000
	rng := rand.New(rand.NewSource(20260923))
	dimNames := []string{"cpu", "mem", "gpu"}

	for iter := 0; iter < iterations; iter++ {
		ndim := 1 + rng.Intn(3)
		dims := dimNames[:ndim]
		capacity := Dims{}
		gridCap := map[string]int64{}
		for _, d := range dims {
			// Include capacity 0 frequently to exercise the zero-capacity rule.
			v := int64(rng.Intn(4))
			capacity[d] = v
			gridCap[d] = v
		}

		njobs := rng.Intn(7)
		var jobs []brute.Job
		for i := 0; i < njobs; i++ {
			a := int64(rng.Intn(24))
			b := a + int64(1+rng.Intn(5))
			dem := map[string]int64{}
			for _, d := range dims {
				if rng.Intn(2) == 0 {
					dem[d] = int64(1 + rng.Intn(3))
				}
			}
			if len(dem) == 0 {
				dem[dims[0]] = 1
			}
			jobs = append(jobs, brute.Job{ID: "j" + itoa(i), Start: a, End: b, Demand: dem})
		}

		duration := int64(1 + rng.Intn(5))
		dem := map[string]int64{}
		for _, d := range dims {
			if rng.Intn(2) == 0 {
				dem[d] = int64(rng.Intn(3)) // may be 0
			}
		}
		if len(dem) == 0 {
			dem[dims[0]] = 1
		}

		wStart := int64(rng.Intn(20))
		openEnded := rng.Intn(3) == 0
		var wEnd int64
		if openEnded {
			wEnd = -1
		} else {
			wEnd = wStart + duration + int64(rng.Intn(12))
		}

		cur, _ := gridExisting(jobs)
		wantStart, wantOK := brute.EarliestFeasibleGrid(gridCap, jobs, wStart, wEnd, duration, dem)
		var wEndTime time.Time
		if wEnd >= 0 {
			wEndTime = hour(wEnd)
		}
		gotStart, gotOK := EarliestFeasible(capacity, cur, hour(wStart), wEndTime,
			time.Duration(duration)*time.Hour, Dims(dem))

		if wantOK != gotOK {
			t.Fatalf("iter %d: feasibility mismatch brute=%v sweep=%v\ncap=%v jobs=%+v want=(%d,%v) dur=%d win=[%d,%d) dem=%v",
				iter, wantOK, gotOK, gridCap, jobs, wantStart, wantOK, duration, wStart, wEnd, dem)
		}
		if wantOK {
			delta := gotStart.Sub(hour(wantStart))
			if delta != 0 {
				t.Fatalf("iter %d: earliest mismatch brute=%d sweep=%d (%s)\ncap=%v jobs=%+v dur=%d win=[%d,%d) dem=%v",
					iter, wantStart, gridSlot(gotStart), gotStart, gridCap, jobs, duration, wStart, wEnd, dem)
			}
		}
	}
}

// TestDifferentialFixed compares FixedConflicts (boolean feasibility) against
// the brute-force cell scan for random fixed placements.
func TestDifferentialFixed(t *testing.T) {
	const iterations = 4000
	rng := rand.New(rand.NewSource(99))
	dimNames := []string{"a", "b"}

	for iter := 0; iter < iterations; iter++ {
		capacity := Dims{}
		gridCap := map[string]int64{}
		for _, d := range dimNames {
			v := int64(rng.Intn(3))
			capacity[d] = v
			gridCap[d] = v
		}
		var jobs []brute.Job
		for i := 0; i < rng.Intn(6); i++ {
			a := int64(rng.Intn(20))
			b := a + int64(1+rng.Intn(4))
			dem := map[string]int64{dimNames[rng.Intn(len(dimNames))]: int64(1 + rng.Intn(2))}
			jobs = append(jobs, brute.Job{ID: "j" + itoa(i), Start: a, End: b, Demand: dem})
		}
		start := int64(rng.Intn(20))
		duration := int64(1 + rng.Intn(4))
		dem := map[string]int64{}
		for _, d := range dimNames {
			if rng.Intn(2) == 0 {
				dem[d] = int64(1 + rng.Intn(2))
			}
		}
		if len(dem) == 0 {
			dem[dimNames[0]] = 1
		}

		cur, _ := gridExisting(jobs)
		cf := FixedConflicts(capacity, cur, hour(start), time.Duration(duration)*time.Hour, Dims(dem))
		gotOK := len(cf) == 0
		wantOK := brute.FixedFits(gridCap, jobs, start, duration, dem)
		if gotOK != wantOK {
			t.Fatalf("iter %d: fixed mismatch brute=%v sweep=%v\ncap=%v jobs=%+v start=%d dur=%d dem=%v conflicts=%+v",
				iter, wantOK, gotOK, gridCap, jobs, start, duration, dem, cf)
		}
	}
}

func gridSlot(t time.Time) int64 {
	return int64(t.Sub(epoch) / time.Hour)
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b []byte
	for i > 0 {
		b = append([]byte{byte('0' + i%10)}, b...)
		i /= 10
	}
	return string(b)
}

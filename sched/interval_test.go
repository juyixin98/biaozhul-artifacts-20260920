package sched

import (
	"strings"
	"testing"
	"time"
)

func TestHalfOpenAdjacency(t *testing.T) {
	a := Interval{Start: hour(2), End: hour(5)}
	b := Interval{Start: hour(5), End: hour(7)}
	c := Interval{Start: hour(4), End: hour(6)}

	if Overlaps(a, b) {
		t.Fatal("adjacent half-open intervals [2,5) and [5,7) must not overlap")
	}
	if got := OverlapDuration(a, b); got != 0 {
		t.Fatalf("adjacent overlap duration = %s, want 0", got)
	}
	if !Overlaps(a, c) {
		t.Fatal("[2,5) and [4,6) must overlap")
	}
	if got := OverlapDuration(a, c); got != time.Hour {
		t.Fatalf("overlap [2,5)x[4,6) = %s, want 1h", got)
	}

	// Endpoint cases at the solver: capacity 1, existing [5,7), demand 1.
	cap := Dims{"cpu": 1}
	cur := []existing{{id: "x", res: "R", interval: b, demand: Dims{"cpu": 1}}}
	// 2h job ending exactly at 5 -> feasible starting at 3.
	got, ok := EarliestFeasible(cap, cur, hour(0), hour(12), 2*time.Hour, Dims{"cpu": 1})
	if !ok || !got.Equal(hour(0)) {
		t.Fatalf("job [0,2) style: got %v ok=%v, want %v", got, ok, hour(0))
	}
	// 2h job immediately before [5,7): placed at 3 ([3,5)) — earliest is 0
	// given the empty timeline; constrain the window to force adjacency:
	got, ok = EarliestFeasible(cap, cur, hour(3), hour(7), 2*time.Hour, Dims{"cpu": 1})
	if !ok || !got.Equal(hour(3)) {
		t.Fatalf("adjacent-before placement got %v ok=%v, want 3", got, ok)
	}
	// Job starting exactly at the existing end (7) is adjacent and allowed.
	cf := FixedConflicts(cap, cur, hour(7), 2*time.Hour, Dims{"cpu": 1})
	if cf != nil {
		t.Fatalf("fixed [7,9) adjacent after existing [5,7) must fit, got %v", cf)
	}
	// Identical intervals must conflict (real overlap, not adjacency).
	cf = FixedConflicts(cap, cur, hour(5), 2*time.Hour, Dims{"cpu": 1})
	if len(cf) == 0 {
		t.Fatal("placement [5,7) identical to existing must conflict")
	}
}

func TestZeroCapacity(t *testing.T) {
	cap := Dims{"cpu": 0, "gpu": 2}

	// Any positive demand on the zero dimension fails even with an empty
	// timeline; fixed placement reports a synthetic, reservation-less
	// conflict naming the dimension.
	cf := FixedConflicts(cap, nil, hour(0), time.Hour, Dims{"cpu": 1})
	if len(cf) == 0 {
		t.Fatal("positive demand on capacity-0 dimension must conflict on empty timeline")
	}
	found := false
	for _, c := range cf {
		for _, d := range c.FailingDims {
			if d == "cpu" {
				found = true
			}
		}
		if c.ReservationID != "" {
			t.Fatalf("zero-capacity conflict must not name a reservation, got %q", c.ReservationID)
		}
	}
	if !found {
		t.Fatalf("conflict must name failing dimension cpu: %+v", cf)
	}

	// Zero demand on the zero dimension and demand only on a dimension with
	// capacity is fine.
	if FixedConflicts(cap, nil, hour(0), time.Hour, Dims{"cpu": 0, "gpu": 1}) != nil {
		t.Fatal("zero demand on zero-capacity dimension must be allowed")
	}

	// Earliest-slot query returns infeasible everywhere.
	if _, ok := EarliestFeasible(cap, nil, hour(0), hour(10), time.Hour, Dims{"cpu": 1}); ok {
		t.Fatal("earliest query must fail on zero-capacity dimension")
	}
}

func TestMultiDimensionalCapacity(t *testing.T) {
	cap := Dims{"cpu": 4, "gpu": 1}
	cur := []existing{
		{id: "j1", res: "R", interval: Interval{Start: hour(0), End: hour(4)}, demand: Dims{"cpu": 3, "gpu": 1}},
	}
	// cpu has room (3+1<=4) but gpu is saturated (1+1>1): must fail while
	// overlapping, become feasible exactly at hour(4).
	got, ok := EarliestFeasible(cap, cur, hour(0), hour(8), 2*time.Hour, Dims{"cpu": 1, "gpu": 1})
	if !ok || !got.Equal(hour(4)) {
		t.Fatalf("multidim earliest got %v ok=%v, want 4", got, ok)
	}
}

func TestIntervalValidation(t *testing.T) {
	if err := (&Interval{Start: hour(1), End: hour(0)}).Validate(); err == nil {
		t.Fatal("start after end must fail")
	}
	err := (&Interval{Start: hour(1), End: hour(1)}).Validate()
	ve, ok := AsValidationError(err)
	if !ok || ve.Reason != ReasonZeroDuration {
		t.Fatalf("zero-length interval: got %v, want reason %s", err, ReasonZeroDuration)
	}
}

func TestNegativeAndUnknownDimensions(t *testing.T) {
	cap := Dims{"cpu": 2}
	if err := validateDims(Dims{"cpu": -1}, true, ReasonNegativeDemand, "x"); err == nil {
		t.Fatal("negative demand must fail validation")
	}
	cf := FixedConflicts(cap, nil, hour(0), time.Hour, Dims{"gpu": 1})
	// Unknown dimension means capacity defaults to 0: a demand of 1 there
	// cannot fit.
	if cf == nil {
		t.Fatal("demand on dimension absent from capacity (treated as 0) must not fit")
	}
}

func TestFixedConflictsNamesLoad(t *testing.T) {
	cap := Dims{"cpu": 2}
	cur := []existing{
		{id: "j1", res: "R", interval: Interval{Start: hour(0), End: hour(3)}, demand: Dims{"cpu": 2}},
	}
	cf := FixedConflicts(cap, cur, hour(2), 2*time.Hour, Dims{"cpu": 1})
	if len(cf) != 1 {
		t.Fatalf("want exactly one over-capacity piece [2,3), got %+v", cf)
	}
	if !cf[0].Overlap.Start.Equal(hour(2)) || !cf[0].Overlap.End.Equal(hour(3)) {
		t.Fatalf("conflict overlap = %v, want [2,3)", cf[0].Overlap)
	}
	if cf[0].ReservationID != "j1" {
		t.Fatalf("conflict must name j1, got %q", cf[0].ReservationID)
	}
	if cf[0].LoadAtOverlap["cpu"] != 2 || cf[0].Capacity["cpu"] != 2 {
		t.Fatalf("conflict detail wrong: load=%v cap=%v", cf[0].LoadAtOverlap, cf[0].Capacity)
	}
}

func TestWindowShorterThanDuration(t *testing.T) {
	sch := New(WithClock(NewFakeClock(hour(0))))
	mustAddResource(t, sch, "R", Dims{"cpu": 4})
	_, err := sch.CommitBatch(&BatchRequest{ID: "b1", Items: []BatchItem{{
		ID: "i1", ResourceID: "R", Kind: ItemEarliest,
		Demand: Dims{"cpu": 1}, Duration: 3 * time.Hour,
		WindowStart: hour(0), WindowEnd: hour(2),
	}}})
	ve, ok := AsValidationError(err)
	if !ok || !strings.Contains(ve.Reason, ReasonWindowTooShort) && ve.Reason != ReasonWindowTooShort {
		t.Fatalf("want window_too_short, got %v", err)
	}
}

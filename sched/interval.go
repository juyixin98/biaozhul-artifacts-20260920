// Package sched implements a resource-reservation scheduling library.
//
// Time is modeled with half-open intervals [Start, End): a reservation that
// ends at t and one that starts at t do NOT conflict. Capacity is
// multi-dimensional; a placement fits when, for every dimension, the sum of
// overlapping demand plus the new demand does not exceed resource capacity.
//
// All quantities are integer (int64). The scheduling clock and the executor
// that reacts to lifecycle transitions are interfaces and can be replaced
// (in particular by fakes in tests).
package sched

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
)

// Interval is a half-open time range [Start, End).
type Interval struct {
	Start time.Time `json:"start"`
	End   time.Time `json:"end"`
}

// Dims is one vector of multi-dimensional capacity or demand.
type Dims map[string]int64

// Reasons returned in validation errors and conflict reports. They are stable
// strings so callers (and HTTP clients) can branch on them.
const (
	ReasonStartAfterEnd    = "start_after_end"
	ReasonZeroDuration     = "zero_duration"
	ReasonNegativeCapacity = "negative_capacity"
	ReasonNegativeDemand   = "negative_demand"
	ReasonUnknownDimension = "unknown_dimension"
	ReasonMissingDemand    = "missing_demand"
	ReasonWindowTooShort   = "window_too_short"
	ReasonDuplicateResID   = "duplicate_resource"
	ReasonResourceNotFound = "resource_not_found"
	ReasonBatchEmpty       = "batch_empty"
	ReasonDuplicateItem    = "duplicate_item_id"
	ReasonItemNotFound     = "item_not_found"

	// ReasonNoFeasibleSlot: the search window contains no feasible placement.
	ReasonNoFeasibleSlot = "no_feasible_slot"
	// ReasonCapacityExceeded: a fixed placement overlaps reservations whose
	// load leaves insufficient capacity in at least one dimension.
	ReasonCapacityExceeded = "capacity_exceeded"
)

// ValidationError describes a malformed request.
type ValidationError struct {
	Reason  string `json:"reason"`
	Message string `json:"message"`
}

func (e *ValidationError) Error() string { return e.Message }

func validationError(reason, format string, args ...any) error {
	return &ValidationError{Reason: reason, Message: fmt.Sprintf(format, args...)}
}

// Validate normalizes the interval (times are converted to UTC so time.Time
// values compare deterministically) and checks start < end.
func (in *Interval) Validate() error {
	in.Start = in.Start.UTC()
	in.End = in.End.UTC()
	if in.Start.After(in.End) {
		return validationError(ReasonStartAfterEnd, "interval start %s is after end %s", in.Start, in.End)
	}
	if in.Start.Equal(in.End) {
		return validationError(ReasonZeroDuration, "interval %s has zero duration (half-open intervals must be non-empty)", in.Start)
	}
	return nil
}

// Duration returns the interval length.
func (in Interval) Duration() time.Duration { return in.End.Sub(in.Start) }

// Overlaps reports whether two half-open intervals share any time. Adjacent
// intervals ([a,b) and [b,c)) do not overlap.
func Overlaps(a, b Interval) bool {
	return a.Start.Before(b.End) && b.Start.Before(a.End)
}

// OverlapDuration returns the length of the intersection, which is 0 for
// disjoint or merely adjacent intervals.
func OverlapDuration(a, b Interval) time.Duration {
	s := a.Start
	if b.Start.After(s) {
		s = b.Start
	}
	e := a.End
	if b.End.Before(e) {
		e = b.End
	}
	if !s.Before(e) {
		return 0
	}
	return e.Sub(s)
}

func validateDims(d Dims, nonPositiveAllowed bool, reason string, label string) error {
	for k, v := range d {
		if strings.TrimSpace(k) == "" {
			return validationError(reason, "%s has a blank dimension name", label)
		}
		if v < 0 {
			return validationError(reason, "%s dimension %q is negative: %d", label, k, v)
		}
		if !nonPositiveAllowed && v == 0 {
			return validationError(reason, "%s dimension %q must be positive, got 0", label, k)
		}
	}
	return nil
}

// Conflict names one existing reservation that makes a fixed placement
// impossible. Dimension details are included when the cause is capacity.
type Conflict struct {
	Reason        string   `json:"reason"`
	ResourceID    string   `json:"resource_id"`
	ReservationID string   `json:"reservation_id"`
	Interval      Interval `json:"interval"`
	Overlap       Interval `json:"overlap"`
	Demand        Dims     `json:"demand,omitempty"`
	LoadAtOverlap Dims     `json:"load_at_overlap,omitempty"`
	Capacity      Dims     `json:"capacity,omitempty"`
	FailingDims   []string `json:"failing_dimensions,omitempty"`
}

// ConflictError is returned when a fixed placement cannot be made.
type ConflictError struct {
	Conflicts []Conflict `json:"conflicts"`
}

func (e *ConflictError) Error() string {
	return fmt.Sprintf("placement conflicts with %d reservation(s)", len(e.Conflicts))
}

// existing is the minimal view of a stored reservation the pure algorithms
// need; Scheduler reservations satisfy it.
type existing struct {
	id       string
	res      string
	interval Interval
	demand   Dims
}

// IntervalUsage is one interval of a resource's timeline together with the
// aggregated demand of reservations covering it. It is produced by slicing the
// reservation set at every start/end boundary (half-open pieces).
type IntervalUsage struct {
	Interval Interval
	Load     Dims
}

// usageSlices partitions [from,to) at the start and end points of every
// existing reservation on resource res, reporting the aggregate demand on
// each resulting piece.
func usageSlices(cur []existing, from, to time.Time) []IntervalUsage {
	type ev struct {
		t     time.Time
		delta Dims
	}
	var evs []ev
	for _, j := range cur {
		s := maxTime(j.interval.Start, from)
		e := minTime(j.interval.End, to)
		if !s.Before(e) {
			continue
		}
		evs = append(evs, ev{t: s, delta: cloneDims(j.demand)}, ev{t: e, delta: negDims(j.demand)})
	}
	sort.Slice(evs, func(i, j int) bool { return evs[i].t.Before(evs[j].t) })

	var out []IntervalUsage
	load := Dims{}
	segStart := from
	flush := func(end time.Time) {
		if segStart.Before(end) {
			out = append(out, IntervalUsage{
				Interval: Interval{Start: segStart, End: end},
				Load:     cloneDims(load),
			})
		}
	}
	for _, e := range evs {
		flush(e.t)
		for k, v := range e.delta {
			load[k] += v
			if load[k] == 0 {
				delete(load, k)
			}
		}
		segStart = e.t
	}
	flush(to)
	return out
}

func dimsFit(load, demand, capacity Dims, failing *[]string) bool {
	ok := true
	for k, v := range demand {
		if load[k]+v > capacity[k] {
			ok = false
			if failing != nil {
				*failing = append(*failing, k)
			}
		}
	}
	return ok
}

// FixedConflicts checks a fixed placement [start,start+duration) with the
// given demand against existing reservations of one resource. It returns nil
// when the placement is feasible. A capacity-0 dimension makes any positive
// demand impossible (even with no other reservations); that is reported as a
// synthetic conflict with reason capacity_exceeded and an empty
// reservation_id, so the zero-capacity case is observable rather than silently
// dropped.
func FixedConflicts(capacity Dims, cur []existing, start time.Time, duration time.Duration, demand Dims) []Conflict {
	win := Interval{Start: start, End: start.Add(duration)}
	slices := usageSlices(cur, win.Start, win.End)
	var conflicts []Conflict
	for _, slice := range slices {
		var failing []string
		if dimsFit(slice.Load, demand, capacity, &failing) {
			continue
		}
		// Name every reservation overlapping this over-capacity piece. When
		// no reservation overlaps it the cause is capacity itself (e.g. a
		// zero-capacity dimension); emit a synthetic conflict then.
		named := false
		for _, j := range cur {
			if !Overlaps(j.interval, slice.Interval) {
				continue
			}
			ovStart := maxTime(j.interval.Start, slice.Interval.Start)
			ovEnd := minTime(j.interval.End, slice.Interval.End)
			conflicts = append(conflicts, Conflict{
				Reason:        ReasonCapacityExceeded,
				ResourceID:    j.res,
				ReservationID: j.id,
				Interval:      j.interval,
				Overlap:       Interval{Start: ovStart, End: ovEnd},
				Demand:        cloneDims(j.demand),
				LoadAtOverlap: cloneDims(slice.Load),
				Capacity:      cloneDims(capacity),
				FailingDims:   failing,
			})
			named = true
		}
		if !named {
			conflicts = append(conflicts, Conflict{
				Reason:        ReasonCapacityExceeded,
				Interval:      slice.Interval,
				Overlap:       slice.Interval,
				LoadAtOverlap: cloneDims(slice.Load),
				Capacity:      cloneDims(capacity),
				Demand:        cloneDims(demand),
				FailingDims:   failing,
			})
		}
	}
	return conflicts
}

func maxTime(a, b time.Time) time.Time {
	if a.After(b) {
		return a
	}
	return b
}

func minTime(a, b time.Time) time.Time {
	if a.Before(b) {
		return a
	}
	return b
}

func cloneDims(d Dims) Dims {
	out := make(Dims, len(d))
	for k, v := range d {
		out[k] = v
	}
	return out
}

func negDims(d Dims) Dims {
	out := make(Dims, len(d))
	for k, v := range d {
		out[k] = -v
	}
	return out
}

// AsValidationError unwraps a ValidationError, if err is one.
func AsValidationError(err error) (*ValidationError, bool) {
	var ve *ValidationError
	if errors.As(err, &ve) {
		return ve, true
	}
	return nil, false
}

// AsConflictError unwraps a ConflictError, if err is one.
func AsConflictError(err error) (*ConflictError, bool) {
	var ce *ConflictError
	if errors.As(err, &ce) {
		return ce, true
	}
	return nil, false
}

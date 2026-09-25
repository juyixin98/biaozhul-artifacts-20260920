package sched

import (
	"sort"
	"time"
)

func sortReservations(rs []Reservation) {
	sort.Slice(rs, func(i, j int) bool {
		if !rs[i].Interval.Start.Equal(rs[j].Interval.Start) {
			return rs[i].Interval.Start.Before(rs[j].Interval.Start)
		}
		if !rs[i].Interval.End.Equal(rs[j].Interval.End) {
			return rs[i].Interval.End.Before(rs[j].Interval.End)
		}
		return rs[i].ID < rs[j].ID
	})
}

// Pump applies every lifecycle transition due at the current clock time:
//
//   - scheduled reservations with Start <= now are handed to the executor's
//     Activate; on error they become "activation_failed";
//   - active reservations with End <= now are handed to Complete and become
//     "completed" (a Complete error is recorded on the event but the terminal
//     state stands).
//
// A reservation whose whole interval is already in the past is activated and
// completed in the same Pump. Pump is serialized; the executor is invoked
// outside the store lock, so executor implementations may call back into the
// scheduler without deadlocking.
func (s *Scheduler) Pump() {
	s.pumpMu.Lock()
	defer s.pumpMu.Unlock()
	now := s.clock.Now()

	// Phase 1: collect and run Activate callbacks outside the lock.
	s.mu.Lock()
	var activating []*Reservation
	var completing []*Reservation
	for _, r := range s.resByID {
		switch r.Status {
		case StatusScheduled:
			if !r.Interval.Start.After(now) {
				activating = append(activating, r)
			}
		case StatusActive:
			if !r.Interval.End.After(now) {
				completing = append(completing, r)
			}
		}
	}
	sort.Slice(activating, func(i, j int) bool {
		return activating[i].Interval.Start.Before(activating[j].Interval.Start)
	})
	sort.Slice(completing, func(i, j int) bool {
		return completing[i].Interval.End.Before(completing[j].Interval.End)
	})
	s.mu.Unlock()

	type actResult struct {
		r   *Reservation
		err error
	}
	results := make([]actResult, len(activating))
	for i, r := range activating {
		results[i] = actResult{r: r, err: s.exec.Activate(r)}
	}

	// Apply activation results, emit events; determine in-past completions.
	s.mu.Lock()
	var newlyDone []*Reservation
	var completeErr = map[*Reservation]string{}
	for _, res := range results {
		r := res.r
		if res.err != nil {
			r.Status = StatusFailed
			s.emitLocked(Event{
				Type: EventActivationError, BatchID: r.BatchID,
				Resource: r.ResourceID, ReservationID: r.ID,
				Payload: map[string]any{"error": res.err.Error(), "reservation": *r},
			})
			continue
		}
		r.Status = StatusActive
		snap := *r
		s.emitLocked(Event{
			Type: EventActivated, BatchID: r.BatchID,
			Resource: r.ResourceID, ReservationID: r.ID, Payload: snap,
		})
		if !r.Interval.End.After(now) {
			newlyDone = append(newlyDone, r)
		}
	}
	s.mu.Unlock()

	// Phase 2: run Complete callbacks outside the lock.
	allDone := append(append([]*Reservation{}, completing...), newlyDone...)
	for _, r := range allDone {
		if err := s.exec.Complete(r); err != nil {
			completeErr[r] = err.Error()
		}
	}

	s.mu.Lock()
	for _, r := range allDone {
		r.Status = StatusCompleted
		snap := *r
		s.emitLocked(Event{
			Type: EventCompleted, BatchID: r.BatchID,
			Resource: r.ResourceID, ReservationID: r.ID,
			Payload: map[string]any{"reservation": snap, "complete_error": completeErr[r]},
		})
	}
	s.mu.Unlock()
}

// Snapshot is a consistent point-in-time view of the whole store.
type Snapshot struct {
	At           time.Time     `json:"at"`
	Resources    []Resource    `json:"resources"`
	Reservations []Reservation `json:"reservations"`
}

// Snapshot reads the store under lock.
func (s *Scheduler) Snapshot() Snapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	res := make([]Resource, 0, len(s.resources))
	for _, r := range s.resources {
		res = append(res, *r)
	}
	sort.Slice(res, func(i, j int) bool { return res[i].ID < res[j].ID })
	var rrs []Reservation
	for _, rs := range s.resByRes {
		for _, r := range rs {
			rrs = append(rrs, *r)
		}
	}
	sortReservations(rrs)
	return Snapshot{At: s.clock.Now(), Resources: res, Reservations: rrs}
}

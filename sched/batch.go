package sched

import (
	stderrors "errors"
	"fmt"
)

// validateAndPlan runs ALL checks for a batch and produces placements. It does
// not mutate the store. Every unplaceable item is reported, so a rejection
// names each conflicting item (the "partial conflict" case).
func (s *Scheduler) validateAndPlan(req *BatchRequest) ([]PlannedItem, []ItemConflict, error) {
	if req.ID == "" {
		return nil, nil, validationError(ReasonBatchEmpty, "batch id must not be empty")
	}
	if _, dup := s.batches[req.ID]; dup {
		return nil, nil, validationError(ReasonDuplicateItem, "batch %q already exists", req.ID)
	}
	if len(req.Items) == 0 {
		return nil, nil, validationError(ReasonBatchEmpty, "batch %q has no items", req.ID)
	}

	seen := map[string]bool{}
	normalized := make([]BatchItem, len(req.Items))
	for i, it := range req.Items {
		if it.ID == "" {
			return nil, nil, validationError(ReasonMissingDemand, "batch %q item #%d has no id", req.ID, i+1)
		}
		if seen[it.ID] {
			return nil, nil, validationError(ReasonDuplicateItem, "batch %q has duplicate item id %q", req.ID, it.ID)
		}
		seen[it.ID] = true
		r, ok := s.resources[it.ResourceID]
		if !ok {
			return nil, nil, validationError(ReasonResourceNotFound, "item %q: resource %q not found", it.ID, it.ResourceID)
		}
		if len(it.Demand) == 0 {
			return nil, nil, validationError(ReasonMissingDemand, "item %q: demand must name at least one dimension", it.ID)
		}
		if err := validateDims(it.Demand, true, ReasonNegativeDemand, fmt.Sprintf("item %q demand", it.ID)); err != nil {
			return nil, nil, err
		}
		for k := range it.Demand {
			if _, ok := r.Capacity[k]; !ok {
				return nil, nil, validationError(ReasonUnknownDimension, "item %q: resource %q has no dimension %q", it.ID, it.ResourceID, k)
			}
		}
		it.Demand = cloneDims(it.Demand)
		switch it.Kind {
		case ItemFixed:
			in := it.Interval
			if err := in.Validate(); err != nil {
				return nil, nil, fmt.Errorf("item %q: %w", it.ID, err)
			}
			it.Interval = in
			it.Duration = in.Duration()
		case ItemEarliest:
			if it.Duration <= 0 {
				return nil, nil, validationError(ReasonZeroDuration, "item %q: duration must be positive", it.ID)
			}
			it.WindowStart = it.WindowStart.UTC()
			it.WindowEnd = it.WindowEnd.UTC()
			if it.WindowStart.IsZero() {
				return nil, nil, validationError(ReasonStartAfterEnd, "item %q: window_start is required", it.ID)
			}
			if !it.WindowEnd.IsZero() {
				if it.WindowEnd.Before(it.WindowStart) {
					return nil, nil, validationError(ReasonStartAfterEnd, "item %q: window_end before window_start", it.ID)
				}
				if it.WindowEnd.Sub(it.WindowStart) < it.Duration {
					return nil, nil, validationError(ReasonWindowTooShort, "item %q: window shorter than duration %s", it.ID, it.Duration)
				}
			}
		default:
			return nil, nil, validationError(ReasonMissingDemand, "item %q: unknown kind %q (want fixed|earliest)", it.ID, string(it.Kind))
		}
		normalized[i] = it
	}
	req.Items = normalized

	// Simulation. tentative[res] are the earlier items of THIS batch already
	// accepted during planning; they conflict with later items exactly like
	// stored reservations do.
	planned := make([]PlannedItem, 0, len(req.Items))
	tentative := map[string][]existing{}
	var itemConflicts []ItemConflict

	for _, it := range req.Items {
		r := s.resources[it.ResourceID]
		cur := append(s.existingLocked(it.ResourceID, nil), tentative[it.ResourceID]...)
		switch it.Kind {
		case ItemFixed:
			cf := FixedConflicts(r.Capacity, cur, it.Interval.Start, it.Duration, it.Demand)
			if cf != nil {
				for i := range cf {
					cf[i].ResourceID = it.ResourceID
				}
				itemConflicts = append(itemConflicts, ItemConflict{
					ItemID: it.ID, Kind: it.Kind, Reason: ReasonCapacityExceeded, Conflicts: cf,
				})
				continue
			}
			planned = append(planned, PlannedItem{Item: it, Start: it.Interval.Start, End: it.Interval.End})
		case ItemEarliest:
			start, ok := EarliestFeasible(r.Capacity, cur, it.WindowStart, it.WindowEnd, it.Duration, it.Demand)
			if !ok {
				itemConflicts = append(itemConflicts, ItemConflict{
					ItemID: it.ID, Kind: it.Kind, Reason: ReasonNoFeasibleSlot,
				})
				continue
			}
			planned = append(planned, PlannedItem{Item: it, Start: start, End: start.Add(it.Duration)})
		}
		pl := planned[len(planned)-1]
		tentative[it.ResourceID] = append(tentative[it.ResourceID], existing{
			id: "#" + it.ID, res: it.ResourceID,
			interval: Interval{Start: pl.Start, End: pl.End}, demand: cloneDims(it.Demand),
		})
	}

	if itemConflicts != nil {
		return nil, itemConflicts, nil
	}
	return planned, nil, nil
}

// CommitBatch atomically validates, plans and commits a batch. If ANY item
// cannot be placed, nothing is committed and a *BatchConflictError is
// returned.
func (s *Scheduler) CommitBatch(req *BatchRequest) (*Batch, error) {
	s.mu.Lock()
	now := s.clock.Now()
	planned, itemConflicts, err := s.validateAndPlan(req)
	if err != nil {
		s.mu.Unlock()
		return nil, err
	}
	if itemConflicts != nil {
		bce := &BatchConflictError{BatchID: req.ID, Items: itemConflicts}
		s.emitLocked(Event{Type: EventBatchRejected, BatchID: req.ID, RequestID: req.RequestID, Payload: bce})
		s.mu.Unlock()
		return nil, bce
	}

	b := &Batch{ID: req.ID, RequestID: req.RequestID, Items: req.Items, Planned: planned, CommittedAt: now}
	s.batches[req.ID] = b
	var events []Event
	events = append(events, Event{Type: EventBatchCommitted, BatchID: req.ID, RequestID: req.RequestID, Payload: b})
	for _, pl := range planned {
		s.nextResNum++
		id := fmt.Sprintf("%s-%d-%d", s.idPrefix, now.UnixNano(), s.nextResNum)
		rr := &Reservation{
			ID: id, BatchID: req.ID, ResourceID: pl.Item.ResourceID,
			Interval: Interval{Start: pl.Start, End: pl.End},
			Demand:   cloneDims(pl.Item.Demand),
			Status:   StatusScheduled, CreatedAt: now,
		}
		b.ReservationIDs = append(b.ReservationIDs, id)
		s.resByID[id] = rr
		s.resByRes[rr.ResourceID] = append(s.resByRes[rr.ResourceID], rr)
		events = append(events, Event{
			Type: EventReserved, BatchID: req.ID, RequestID: req.RequestID,
			Resource: rr.ResourceID, ReservationID: id, Payload: rr,
		})
	}
	for _, ev := range events {
		s.emitLocked(ev)
	}
	s.mu.Unlock()

	// Lifecycle transitions due immediately (e.g. fixed intervals in the
	// past) are applied on the next Pump; callers driving a fake clock invoke
	// it explicitly.
	s.Pump()
	return b, nil
}

// GetBatch returns a committed batch.
func (s *Scheduler) GetBatch(id string) (*Batch, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	b, ok := s.batches[id]
	return b, ok
}

// GetReservation returns one reservation.
func (s *Scheduler) GetReservation(id string) (*Reservation, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.resByID[id]
	if !ok {
		return nil, false
	}
	rc := *r
	return &rc, true
}

// Reservations returns the reservations of one resource (or all resources when
// resID is ""), sorted by start time.
func (s *Scheduler) Reservations(resID string) []Reservation {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []Reservation
	scan := s.resByRes[resID]
	if resID == "" {
		for _, rs := range s.resByRes {
			scan = append(scan, rs...)
		}
	}
	for _, r := range scan {
		out = append(out, *r)
	}
	sortReservations(out)
	return out
}

// AsBatchConflictError unwraps a *BatchConflictError, if err is one.
func AsBatchConflictError(err error) (*BatchConflictError, bool) {
	var bce *BatchConflictError
	if stderrors.As(err, &bce) {
		return bce, true
	}
	return nil, false
}

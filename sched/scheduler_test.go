package sched

import (
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

func mustAddResource(t *testing.T, sch *Scheduler, id string, cap Dims) {
	t.Helper()
	if err := sch.AddResource(&Resource{ID: id, Capacity: cap}); err != nil {
		t.Fatalf("add resource %s: %v", id, err)
	}
}

func fixed(id, res string, start, end int64, dem Dims) BatchItem {
	return BatchItem{
		ID: id, ResourceID: res, Kind: ItemFixed, Demand: dem,
		Interval: Interval{Start: hour(start), End: hour(end)},
	}
}

func earliest(id, res string, dur time.Duration, ws, we int64, dem Dims) BatchItem {
	it := BatchItem{
		ID: id, ResourceID: res, Kind: ItemEarliest, Demand: dem,
		Duration: dur, WindowStart: hour(ws),
	}
	if we >= 0 {
		it.WindowEnd = hour(we)
	}
	return it
}

// TestBatchPartialConflictIsAtomic is the core atomicity acceptance test:
// items 1 and 2 would commit, item 3 conflicts — the whole batch must be
// rejected and NOTHING may land.
func TestBatchPartialConflictIsAtomic(t *testing.T) {
	log := NewEventLog()
	clk := NewFakeClock(hour(0))
	sch := New(WithClock(clk), WithSink(log))
	mustAddResource(t, sch, "R", Dims{"cpu": 1})

	// Pre-existing reservation [3,6).
	pre, err := sch.CommitBatch(&BatchRequest{ID: "pre", Items: []BatchItem{
		fixed("p", "R", 3, 6, Dims{"cpu": 1}),
	}})
	if err != nil {
		t.Fatal(err)
	}
	if len(pre.ReservationIDs) != 1 {
		t.Fatalf("precommit: %d reservations, want 1", len(pre.ReservationIDs))
	}
	before := sch.Snapshot()
	beforeCount := len(before.Reservations)

	// Mixed batch:
	//  a [0,1)        fits
	//  b earliest 2h  fits (gets [1,3))
	//  c [5,7)        overlaps pre [3,6) at [5,6) -> conflict
	batch := &BatchRequest{ID: "mixed", Items: []BatchItem{
		fixed("a", "R", 0, 1, Dims{"cpu": 1}),
		earliest("b", "R", 2*time.Hour, 0, 12, Dims{"cpu": 1}),
		fixed("c", "R", 5, 7, Dims{"cpu": 1}),
	}}
	_, err = sch.CommitBatch(batch)
	bce, ok := AsBatchConflictError(err)
	if !ok {
		t.Fatalf("want BatchConflictError, got %T %v", err, err)
	}
	var named []string
	for _, ic := range bce.Items {
		named = append(named, ic.ItemID)
		if ic.ItemID == "c" && ic.Reason != ReasonCapacityExceeded {
			t.Fatalf("item c reason = %s, want capacity_exceeded", ic.Reason)
		}
	}
	if len(named) != 1 || named[0] != "c" {
		t.Fatalf("conflicting items = %v, want [c]", named)
	}

	after := sch.Snapshot()
	if len(after.Reservations) != beforeCount {
		t.Fatalf("ATOMICITY BROKEN: reservations went from %d to %d after rejected batch:\n%+v",
			beforeCount, len(after.Reservations), after.Reservations)
	}
	if _, exists := sch.GetBatch("mixed"); exists {
		t.Fatal("rejected batch must not be stored")
	}

	// The rejection must be observable as a structured event, and no
	// "reserved"/"committed" events may exist for the rejected batch.
	var sawReject bool
	for _, ev := range log.Events() {
		if ev.BatchID == "mixed" {
			switch ev.Type {
			case EventBatchRejected:
				sawReject = true
			case EventBatchCommitted, EventReserved:
				t.Fatalf("rejected batch emitted %s", ev.Type)
			}
		}
	}
	if !sawReject {
		t.Fatal("batch_rejected event missing")
	}
}

// TestBatchInternalConflicts ensures items inside one batch cannot overload a
// resource even when each fits against the pre-existing state alone.
func TestBatchInternalConflicts(t *testing.T) {
	sch := New(WithClock(NewFakeClock(hour(0))))
	mustAddResource(t, sch, "R", Dims{"cpu": 1})
	_, err := sch.CommitBatch(&BatchRequest{ID: "b", Items: []BatchItem{
		fixed("x", "R", 0, 3, Dims{"cpu": 1}),
		fixed("y", "R", 2, 4, Dims{"cpu": 1}),
	}})
	if err == nil {
		t.Fatal("overlapping items in one batch must be rejected together")
	}
	if len(sch.Reservations("R")) != 0 {
		t.Fatalf("nothing must land on internal conflict, got %+v", sch.Reservations("R"))
	}
}

// TestBatchAdjacentItemsFit checks half-open adjacency within a batch.
func TestBatchAdjacentItemsFit(t *testing.T) {
	sch := New(WithClock(NewFakeClock(hour(0))))
	mustAddResource(t, sch, "R", Dims{"cpu": 1})
	b, err := sch.CommitBatch(&BatchRequest{ID: "b", Items: []BatchItem{
		fixed("x", "R", 0, 3, Dims{"cpu": 1}),
		fixed("y", "R", 3, 5, Dims{"cpu": 1}),
		fixed("z", "R", 5, 6, Dims{"cpu": 1}),
	}})
	if err != nil {
		t.Fatalf("adjacent items must all commit: %v", err)
	}
	if len(b.ReservationIDs) != 3 {
		t.Fatalf("want 3 reservations, got %d", len(b.ReservationIDs))
	}
}

// TestBatchZeroCapacityAtomic exercises the zero-capacity acceptance scenario
// inside a batch.
func TestBatchZeroCapacityAtomic(t *testing.T) {
	sch := New(WithClock(NewFakeClock(hour(0))))
	mustAddResource(t, sch, "dead", Dims{"slots": 0})
	mustAddResource(t, sch, "alive", Dims{"slots": 2})
	_, err := sch.CommitBatch(&BatchRequest{ID: "b", Items: []BatchItem{
		fixed("ok", "alive", 0, 2, Dims{"slots": 1}),
		fixed("nope", "dead", 0, 2, Dims{"slots": 1}),
	}})
	bce, ok := AsBatchConflictError(err)
	if !ok {
		t.Fatalf("want conflict error, got %v", err)
	}
	if len(bce.Items) != 1 || bce.Items[0].ItemID != "nope" {
		t.Fatalf("conflicts = %+v, want only nope", bce.Items)
	}
	if len(sch.Reservations("alive")) != 0 || len(sch.Reservations("dead")) != 0 {
		t.Fatalf("zero-capacity partial conflict must not partially land: alive=%d dead=%d",
			len(sch.Reservations("alive")), len(sch.Reservations("dead")))
	}
}

func TestEarliestItemPlannedSlot(t *testing.T) {
	sch := New(WithClock(NewFakeClock(hour(0))))
	mustAddResource(t, sch, "R", Dims{"cpu": 1})
	_, err := sch.CommitBatch(&BatchRequest{ID: "pre", Items: []BatchItem{
		fixed("p", "R", 0, 2, Dims{"cpu": 1}),
	}})
	if err != nil {
		t.Fatal(err)
	}
	b, err := sch.CommitBatch(&BatchRequest{ID: "ask", Items: []BatchItem{
		earliest("e", "R", 2*time.Hour, 0, 10, Dims{"cpu": 1}),
	}})
	if err != nil {
		t.Fatal(err)
	}
	if !b.Planned[0].Start.Equal(hour(2)) || !b.Planned[0].End.Equal(hour(4)) {
		t.Fatalf("planned slot = [%v,%v), want [2,4)", b.Planned[0].Start, b.Planned[0].End)
	}
}

func TestEarliestNoSlot(t *testing.T) {
	sch := New(WithClock(NewFakeClock(hour(0))))
	mustAddResource(t, sch, "R", Dims{"cpu": 1})
	_, err := sch.CommitBatch(&BatchRequest{ID: "full", Items: []BatchItem{
		fixed("p", "R", 0, 5, Dims{"cpu": 1}),
	}})
	if err != nil {
		t.Fatal(err)
	}
	// 3h job in a 4h window fully covered by the existing 5h job: no slot.
	_, err = sch.CommitBatch(&BatchRequest{ID: "ask", Items: []BatchItem{
		earliest("e", "R", 3*time.Hour, 1, 4, Dims{"cpu": 1}),
	}})
	bce, ok := AsBatchConflictError(err)
	if !ok || len(bce.Items) != 1 || bce.Items[0].Reason != ReasonNoFeasibleSlot {
		t.Fatalf("want no_feasible_slot, got %v", err)
	}
}

func TestSchedulerEarliestQueryIsReadOnly(t *testing.T) {
	sch := New(WithClock(NewFakeClock(hour(0))))
	mustAddResource(t, sch, "R", Dims{"cpu": 1})
	_, _ = sch.CommitBatch(&BatchRequest{ID: "pre", Items: []BatchItem{
		fixed("p", "R", 0, 2, Dims{"cpu": 1}),
	}})
	got, ok, err := sch.EarliestFeasible("R", hour(0), hour(10), time.Hour, Dims{"cpu": 1})
	if err != nil || !ok || !got.Equal(hour(2)) {
		t.Fatalf("query got %v ok=%v err=%v, want hour 2", got, ok, err)
	}
	if len(sch.Reservations("R")) != 1 {
		t.Fatal("query must not create reservations")
	}
	if _, _, err := sch.EarliestFeasible("nope", hour(0), hour(10), time.Hour, Dims{"cpu": 1}); err == nil {
		t.Fatal("unknown resource must error")
	}
}

func TestValidationErrors(t *testing.T) {
	sch := New(WithClock(NewFakeClock(hour(0))))
	if err := sch.AddResource(&Resource{ID: "R", Capacity: Dims{"cpu": -1}}); err == nil {
		t.Fatal("negative capacity rejected")
	}
	mustAddResource(t, sch, "R", Dims{"cpu": 2})
	if err := sch.AddResource(&Resource{ID: "R", Capacity: Dims{}}); err == nil {
		t.Fatal("duplicate resource rejected")
	}
	// empty batch
	_, err := sch.CommitBatch(&BatchRequest{ID: "empty", Items: nil})
	if _, ok := AsValidationError(err); !ok {
		t.Fatalf("empty batch must be validation error, got %v", err)
	}
	// duplicate item ids
	_, err = sch.CommitBatch(&BatchRequest{ID: "dup", Items: []BatchItem{
		fixed("x", "R", 0, 1, Dims{"cpu": 1}),
		fixed("x", "R", 1, 2, Dims{"cpu": 1}),
	}})
	if _, ok := AsValidationError(err); !ok {
		t.Fatalf("duplicate item id must be validation error, got %v", err)
	}
	// unknown dimension
	_, err = sch.CommitBatch(&BatchRequest{ID: "unk", Items: []BatchItem{
		fixed("x", "R", 0, 1, Dims{"gpu": 1}),
	}})
	if ve, ok := AsValidationError(err); !ok || ve.Reason != ReasonUnknownDimension {
		t.Fatalf("want unknown_dimension, got %v", err)
	}
	// replay same batch id
	b1 := &BatchRequest{ID: "once", Items: []BatchItem{fixed("x", "R", 0, 1, Dims{"cpu": 1})}}
	if _, err := sch.CommitBatch(b1); err != nil {
		t.Fatal(err)
	}
	if _, err := sch.CommitBatch(b1); err == nil {
		t.Fatal("reusing a batch id must fail")
	}
}

func TestEventSequencing(t *testing.T) {
	log := NewEventLog()
	sch := New(WithClock(NewFakeClock(hour(10))), WithSink(log))
	mustAddResource(t, sch, "R", Dims{"cpu": 1})
	_, err := sch.CommitBatch(&BatchRequest{ID: "b", Items: []BatchItem{
		fixed("x", "R", 12, 14, Dims{"cpu": 1}),
	}})
	if err != nil {
		t.Fatal(err)
	}
	evs := log.Events()
	wantOrder := []string{EventResourceAdded, EventBatchCommitted, EventReserved}
	if len(evs) < len(wantOrder) {
		t.Fatalf("events = %d, want >= %d: %+v", len(evs), len(wantOrder), evs)
	}
	for i, w := range wantOrder {
		if evs[i].Type != w {
			t.Fatalf("event %d = %s, want %s", i, evs[i].Type, w)
		}
		if evs[i].Seq != int64(i+1) {
			t.Fatalf("event seq = %d, want %d", evs[i].Seq, i+1)
		}
	}
}

// recordingExecutor records lifecycle calls and can be made to fail activation.
type recordingExecutor struct {
	mu           sync.Mutex
	activated    []string
	completed    []string
	failActivate map[string]error
}

func newRecordingExecutor() *recordingExecutor {
	return &recordingExecutor{failActivate: map[string]error{}}
}

func (e *recordingExecutor) Activate(r *Reservation) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.activated = append(e.activated, r.ID)
	if err, ok := e.failActivate[r.ID]; ok {
		return err
	}
	return nil
}

func (e *recordingExecutor) Complete(r *Reservation) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.completed = append(e.completed, r.ID)
	return nil
}

func (e *recordingExecutor) snap() ([]string, []string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]string{}, e.activated...), append([]string{}, e.completed...)
}

func TestLifecycleWithFakeClock(t *testing.T) {
	clk := NewFakeClock(hour(0))
	exec := newRecordingExecutor()
	log := NewEventLog()
	sch := New(WithClock(clk), WithExecutor(exec), WithSink(log))
	mustAddResource(t, sch, "R", Dims{"cpu": 1})
	b, err := sch.CommitBatch(&BatchRequest{ID: "b", Items: []BatchItem{
		fixed("x", "R", 2, 4, Dims{"cpu": 1}),
	}})
	if err != nil {
		t.Fatal(err)
	}
	id := b.ReservationIDs[0]

	if rr, _ := sch.GetReservation(id); rr.Status != StatusScheduled {
		t.Fatalf("initial status = %s, want scheduled", rr.Status)
	}
	clk.Advance(2 * time.Hour)
	sch.Pump()
	if rr, _ := sch.GetReservation(id); rr.Status != StatusActive {
		t.Fatalf("at start status = %s, want active", rr.Status)
	}
	clk.Advance(2 * time.Hour)
	sch.Pump()
	if rr, _ := sch.GetReservation(id); rr.Status != StatusCompleted {
		t.Fatalf("at end status = %s, want completed", rr.Status)
	}
	acts, comps := exec.snap()
	if len(acts) != 1 || acts[0] != id || len(comps) != 1 || comps[0] != id {
		t.Fatalf("executor calls activate=%v complete=%v", acts, comps)
	}
	var types []string
	for _, ev := range log.Events() {
		types = append(types, ev.Type)
	}
	if !contains(types, EventActivated) || !contains(types, EventCompleted) {
		t.Fatalf("lifecycle events missing: %v", types)
	}
}

func TestLifecyclePastIntervalActivatesAndCompletes(t *testing.T) {
	clk := NewFakeClock(hour(10))
	exec := newRecordingExecutor()
	sch := New(WithClock(clk), WithExecutor(exec))
	mustAddResource(t, sch, "R", Dims{"cpu": 1})
	b, err := sch.CommitBatch(&BatchRequest{ID: "b", Items: []BatchItem{
		fixed("x", "R", 2, 4, Dims{"cpu": 1}),
	}})
	if err != nil {
		t.Fatal(err)
	}
	id := b.ReservationIDs[0]
	// CommitBatch pumps; the interval is entirely in the past.
	if rr, _ := sch.GetReservation(id); rr.Status != StatusCompleted {
		t.Fatalf("past interval after commit: %s, want completed", rr.Status)
	}
	acts, comps := exec.snap()
	if len(acts) != 1 || len(comps) != 1 {
		t.Fatalf("want 1 activate + 1 complete, got %v %v", acts, comps)
	}
}

func TestExecutorActivationFailure(t *testing.T) {
	clk := NewFakeClock(hour(0))
	exec := newRecordingExecutor()
	log := NewEventLog()
	sch := New(WithClock(clk), WithExecutor(exec), WithSink(log))
	mustAddResource(t, sch, "R", Dims{"cpu": 1})
	b, err := sch.CommitBatch(&BatchRequest{ID: "b", Items: []BatchItem{
		fixed("x", "R", 1, 3, Dims{"cpu": 1}),
	}})
	if err != nil {
		t.Fatal(err)
	}
	id := b.ReservationIDs[0]
	exec.failActivate[id] = errors.New("boom: downstream refused")

	clk.Advance(time.Hour)
	sch.Pump()
	rr, _ := sch.GetReservation(id)
	if rr.Status != StatusFailed {
		t.Fatalf("status = %s, want activation_failed", rr.Status)
	}
	var saw bool
	for _, ev := range log.Events() {
		if ev.Type == EventActivationError {
			saw = true
		}
	}
	if !saw {
		t.Fatal("activation_error event missing")
	}

	// Failed activation must still occupy capacity: a fixed placement
	// overlapping [1,3) still cannot commit.
	_, err = sch.CommitBatch(&BatchRequest{ID: "b2", Items: []BatchItem{
		fixed("y", "R", 2, 4, Dims{"cpu": 1}),
	}})
	if _, ok := AsBatchConflictError(err); !ok {
		t.Fatalf("failed reservation must keep capacity, got err=%v", err)
	}
}

func TestConcurrentBatchesAreSerialized(t *testing.T) {
	sch := New(WithClock(NewFakeClock(hour(0))))
	mustAddResource(t, sch, "R", Dims{"cpu": 1})
	var wg sync.WaitGroup
	results := make(chan error, 20)
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, err := sch.CommitBatch(&BatchRequest{
				ID: fmt.Sprintf("b-%d", i),
				Items: []BatchItem{
					fixed("x", "R", 0, 2, Dims{"cpu": 1}),
				},
			})
			results <- err
		}(i)
	}
	wg.Wait()
	close(results)
	committed, rejected := 0, 0
	for err := range results {
		if err == nil {
			committed++
		} else {
			rejected++
		}
	}
	if committed != 1 || rejected != 19 {
		t.Fatalf("exactly one of 20 racing identical batches must win, got committed=%d rejected=%d", committed, rejected)
	}
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

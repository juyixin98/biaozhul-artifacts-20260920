package shutdown_test

import (
	"context"
	"sync"
	"testing"
	"time"

	"gracefulshutdown/internal/clock"
	"gracefulshutdown/internal/lifecycle"
	"gracefulshutdown/internal/shutdown"
)

const (
	testDrain  = 500 * time.Millisecond
	testCancel = 300 * time.Millisecond
	testClose  = 200 * time.Millisecond
)

type rig struct {
	c     *shutdown.Coordinator
	fc    *clock.Fake
	armed chan struct{}
}

func newRig(t *testing.T) *rig {
	t.Helper()
	fc := clock.NewFake(time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC))
	armed := make(chan struct{}, 16)
	c := shutdown.New(shutdown.Config{
		DrainTimeout:  testDrain,
		CancelTimeout: testCancel,
		CloseTimeout:  testClose,
		Clk:           fc,
		TimerArmed:    func() { armed <- struct{}{} },
	})
	return &rig{c: c, fc: fc, armed: armed}
}

func waitPhase(t *testing.T, c *shutdown.Coordinator, want lifecycle.Phase) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if c.Phase() == want {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("phase never became %s (last: %s)", want, c.Phase())
}

// assertTimelineOrder checks the five phases appear in exactly the required
// order on the report timeline.
func assertTimelineOrder(t *testing.T, rep lifecycle.Report) {
	t.Helper()
	idx := 0
	for _, ev := range rep.Timeline {
		if idx < len(lifecycle.OrderedPhases) && ev.Phase == lifecycle.OrderedPhases[idx] {
			idx++
		}
	}
	if idx != len(lifecycle.OrderedPhases) {
		t.Fatalf("timeline missing phases; got phases: %v (matched %d/%d)",
			phaseNames(rep.Timeline), idx, len(lifecycle.OrderedPhases))
	}
}

func phaseNames(evs []lifecycle.Event) []lifecycle.Phase {
	out := make([]lifecycle.Phase, 0, len(evs))
	for _, ev := range evs {
		out = append(out, ev.Phase)
	}
	return out
}

func TestWorkCompletingInDrainFinishesWithoutCancellation(t *testing.T) {
	t.Parallel()
	r := newRig(t)

	h, ok := r.c.Begin("w1", "long")
	if !ok {
		t.Fatal("Begin rejected while RUNNING")
	}

	sub := r.c.SubscribeReport()
	r.c.NotifySignal()
	waitPhase(t, r.c, lifecycle.PhaseDraining)
	<-r.armed // drain timer armed; but the work will finish first

	r.c.Finish(h, lifecycle.OutcomeCompleted, "finished during drain")

	select {
	case rep := <-sub:
		assertTimelineOrder(t, rep)
		if rep.Completed != 1 || rep.Cancelled != 0 {
			t.Fatalf("completed=%d cancelled=%d, want 1/0", rep.Completed, rep.Cancelled)
		}
		if rep.InFlightAtClose != 0 {
			t.Fatalf("inFlightAtClose=%d, want 0", rep.InFlightAtClose)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("shutdown never completed")
	}
}

func TestDrainTimeoutCancelsInFlightWork(t *testing.T) {
	t.Parallel()
	r := newRig(t)

	h, ok := r.c.Begin("w1", "long")
	if !ok {
		t.Fatal("Begin rejected while RUNNING")
	}

	sub := r.c.SubscribeReport()
	r.c.NotifySignal()

	<-r.armed // drain timer armed
	r.fc.Advance(testDrain)
	waitPhase(t, r.c, lifecycle.PhaseCancelling)

	select {
	case <-r.c.Cancelled():
	case <-time.After(3 * time.Second):
		t.Fatal("work cancellation channel never closed in phase 3")
	}
	r.c.Finish(h, lifecycle.OutcomeCancelled, "cancelled after drain budget")

	var rep lifecycle.Report
	select {
	case rep = <-sub:
	case <-time.After(3 * time.Second):
		t.Fatal("shutdown never completed")
	}
	assertTimelineOrder(t, rep)
	if rep.Cancelled != 1 || rep.Completed != 0 {
		t.Fatalf("cancelled=%d completed=%d, want 1/0", rep.Cancelled, rep.Completed)
	}
	found := false
	for _, ev := range rep.Timeline {
		if ev.Note == "drain budget elapsed" {
			found = true
		}
	}
	if !found {
		t.Fatalf("timeline lacks drain-budget note: %+v", phaseNames(rep.Timeline))
	}
}

func TestCancelBudgetElapsesForWorkIgnoringCancellation(t *testing.T) {
	t.Parallel()
	r := newRig(t)

	// Work that never calls Finish: both budgets elapse; it is still tracked as
	// in-flight at close (the forceful connection drop is the app's job).
	h, ok := r.c.Begin("stuck", "long")
	if !ok {
		t.Fatal("Begin rejected while RUNNING")
	}
	_ = h

	sub := r.c.SubscribeReport()
	r.c.NotifySignal()

	<-r.armed
	r.fc.Advance(testDrain)
	waitPhase(t, r.c, lifecycle.PhaseCancelling)
	<-r.c.Cancelled()
	<-r.armed // cancel timer armed
	r.fc.Advance(testCancel)

	select {
	case rep := <-sub:
		if rep.InFlightAtClose != 1 {
			t.Fatalf("inFlightAtClose=%d, want 1 (work ignored cancellation)", rep.InFlightAtClose)
		}
		assertTimelineOrder(t, rep)
	case <-time.After(3 * time.Second):
		t.Fatal("shutdown never completed")
	}
}

func TestRepeatSignalsAreCountedAndEscalate(t *testing.T) {
	t.Parallel()
	r := newRig(t)

	h, ok := r.c.Begin("w1", "long")
	if !ok {
		t.Fatal("Begin rejected while RUNNING")
	}

	sub := r.c.SubscribeReport()
	r.c.NotifySignal()
	<-r.armed // drain timer armed — coordinator is waiting

	// Duplicate signals during drain must not panic/run a second sequence and
	// must skip the drain immediately (no clock advance).
	r.c.NotifySignal()
	r.c.NotifySignal()
	select {
	case <-r.c.Cancelled(): // phase 3 reached by escalation, without clock movement
	case <-time.After(3 * time.Second):
		t.Fatalf("escalation did not reach CANCELLING (phase=%s)", r.c.Phase())
	}
	r.c.Finish(h, lifecycle.OutcomeCancelled, "cancelled after escalation")

	select {
	case rep := <-sub:
		if rep.Signals != 3 {
			t.Fatalf("signals=%d, want 3", rep.Signals)
		}
		found := false
		for _, ev := range rep.Timeline {
			if ev.Note == "repeat signal: escalating to next phase" {
				found = true
			}
		}
		if !found {
			t.Fatal("timeline lacks repeat-signal escalation note")
		}
		assertTimelineOrder(t, rep)
	case <-time.After(3 * time.Second):
		t.Fatal("shutdown never completed")
	}
}

func TestNewWorkAndBackgroundRejectedAfterStopAccept(t *testing.T) {
	t.Parallel()
	r := newRig(t)

	h, ok := r.c.Begin("w1", "long") // keeps shutdown in the drain phase
	if !ok {
		t.Fatal("Begin rejected while RUNNING")
	}
	sub := r.c.SubscribeReport()
	r.c.NotifySignal()
	waitPhase(t, r.c, lifecycle.PhaseDraining) // STOP_ACCEPT is instantaneous

	if _, admitted := r.c.Begin("late", "long"); admitted {
		t.Fatal("foreground work admitted after STOP_ACCEPT")
	}
	if _, admitted := r.c.BeginBackground("late-job"); admitted {
		t.Fatal("background job spawned after STOP_ACCEPT")
	}

	// Expire the drain budget, then release the work in phase 3.
	<-r.armed
	r.fc.Advance(testDrain)
	<-r.c.Cancelled()
	r.c.Finish(h, lifecycle.OutcomeCancelled, "cancelled")

	rep := <-sub
	if rep.Rejected < 1 {
		t.Fatalf("rejected foreground=%d, want >=1", rep.Rejected)
	}
	if rep.RejectedBackgroundSpawns != 1 {
		t.Fatalf("rejectedBackgroundSpawns=%d, want 1", rep.RejectedBackgroundSpawns)
	}
	if rep.BackgroundSpawned != 0 || rep.BackgroundAfterStop != 0 {
		t.Fatalf("backgroundSpawned=%d afterStop=%d, want 0/0",
			rep.BackgroundSpawned, rep.BackgroundAfterStop)
	}
}

type stubResource struct {
	name  string
	order *[]string
	mu    *sync.Mutex
	block chan struct{} // optional: closed to let Close return
}

func (s stubResource) Name() string { return s.name }
func (s stubResource) Close(ctx context.Context) error {
	s.mu.Lock()
	*s.order = append(*s.order, s.name)
	s.mu.Unlock()
	if s.block != nil {
		select {
		case <-s.block:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}

func TestResourcesCloseInReverseRegistrationOrder(t *testing.T) {
	t.Parallel()
	r := newRig(t)
	var mu sync.Mutex
	var order []string
	r.c.RegisterResource(stubResource{name: "first", order: &order, mu: &mu})
	r.c.RegisterResource(stubResource{name: "second", order: &order, mu: &mu})
	r.c.RegisterResource(stubResource{name: "third", order: &order, mu: &mu})

	sub := r.c.SubscribeReport()
	r.c.NotifySignal()
	rep := <-sub

	mu.Lock()
	defer mu.Unlock()
	want := []string{"third", "second", "first"}
	if len(order) != 3 || order[0] != want[0] || order[1] != want[1] || order[2] != want[2] {
		t.Fatalf("close order=%v, want %v", order, want)
	}
	if len(rep.CloseOrder) != 3 {
		t.Fatalf("close records=%d, want 3", len(rep.CloseOrder))
	}
	for i, rec := range rep.CloseOrder {
		if rec.Order != i+1 || rec.Name != want[i] {
			t.Fatalf("close record %d = %+v, want order %d name %s", i, rec, i+1, want[i])
		}
	}
}

func TestResourceCloseTimeoutCancelsAndReportsError(t *testing.T) {
	t.Parallel()
	// Zero drain/cancel budgets: with no accepted work those waits return
	// immediately without arming timers, so the only armed timer is the close.
	fc := clock.NewFake(time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC))
	armed := make(chan struct{}, 8)
	c := shutdown.New(shutdown.Config{
		CloseTimeout: testClose, Clk: fc, TimerArmed: func() { armed <- struct{}{} },
	})
	block := make(chan struct{}) // never closed: resource hangs
	c.RegisterResource(stubResource{
		name: "slow", order: new([]string), mu: new(sync.Mutex), block: block,
	})

	sub := c.SubscribeReport()
	c.NotifySignal()
	<-armed // close timer armed
	fc.Advance(testClose)

	select {
	case rep := <-sub:
		if len(rep.CloseOrder) != 1 || rep.CloseOrder[0].Err == "" {
			t.Fatalf("close record=%+v, want a timeout error", rep.CloseOrder)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("shutdown never completed")
	}
	close(block)
}

func TestSignalIsIdempotentAndReturnsReport(t *testing.T) {
	t.Parallel()
	r := newRig(t)

	// Three signals in quick succession start and hurry the run; the sequence
	// must run exactly once and the report must count every signal.
	r.c.NotifySignal()
	r.c.NotifySignal()
	r.c.NotifySignal()

	select {
	case <-r.c.Done():
	case <-time.After(3 * time.Second):
		t.Fatalf("shutdown did not complete (phase=%s)", r.c.Phase())
	}
	rep := r.c.Report()
	if rep.Signals != 3 {
		t.Fatalf("signals=%d, want 3", rep.Signals)
	}
	if rep.Timeline[0].Phase != lifecycle.PhaseStopAccept {
		t.Fatalf("first timeline event=%s, want STOP_ACCEPT", rep.Timeline[0].Phase)
	}
}

func TestStopAcceptChannelClosesInPhaseOne(t *testing.T) {
	t.Parallel()
	r := newRig(t)
	phaseOne := make(chan struct{})
	r.c.OnStopAccept(func() { close(phaseOne) })

	r.c.NotifySignal()
	defer r.c.Signal()
	select {
	case <-r.c.StopAccepting():
	case <-time.After(3 * time.Second):
		t.Fatal("StopAccepting never closed")
	}
	select {
	case <-phaseOne:
	case <-time.After(3 * time.Second):
		t.Fatal("OnStopAccept hook never ran")
	}
}

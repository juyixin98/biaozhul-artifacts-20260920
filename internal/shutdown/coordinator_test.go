package shutdown

import (
	"context"
	"testing"
	"time"

	"graceful-shutdown/internal/clock"
	"graceful-shutdown/internal/fakesvc"
	"graceful-shutdown/internal/ledger"
	"graceful-shutdown/internal/resources"
)

func newHarness(t *testing.T, drainTimeout time.Duration) (*clock.Fake, *Coordinator, *fakesvc.FakeDB, *fakesvc.FakeQueue) {
	t.Helper()
	clk := clock.NewFake(time.Unix(1_700_000_000, 0))
	led := ledger.New()
	reg := resources.NewRegistry()
	db := fakesvc.NewFakeDB(clk)
	queue := fakesvc.NewFakeQueue(clk)
	reg.Register(db)
	reg.Register(queue)
	coord := New(clk, drainTimeout, led, reg)
	return clk, coord, db, queue
}

func waitPhase(t *testing.T, coord *Coordinator, want Phase) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if coord.State().Phase == want {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out waiting for phase %q, got %q", want, coord.State().Phase)
}

func waitDone(t *testing.T, coord *Coordinator) {
	t.Helper()
	select {
	case <-coord.Done():
	case <-time.After(2 * time.Second):
		t.Fatalf("shutdown did not complete; state=%+v", coord.State())
	}
}

// waitPending blocks until the fake clock has at least n registered timers,
// synchronising the test with goroutines that sleep on the fake clock.
func waitPending(t *testing.T, clk *clock.Fake, n int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for clk.Pending() < n && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if clk.Pending() < n {
		t.Fatalf("timed out waiting for %d pending timers, got %d", n, clk.Pending())
	}
}

func TestGracefulDrain_WorkCompletesBeforeTimeout(t *testing.T) {
	clk, coord, db, _ := newHarness(t, 200*time.Millisecond)

	// An accepted short request: fake-DB latency 50ms on the fake clock.
	id, rootCtx, ok := coord.TryAccept("/work?ms=50", "request")
	if !ok {
		t.Fatal("request should be accepted pre-shutdown")
	}
	go func() {
		if _, err := db.SlowQuery(rootCtx, "q", 50*time.Millisecond); err != nil {
			coord.Finish(id, ledger.OutcomeCancelled, err.Error())
			return
		}
		coord.Finish(id, ledger.OutcomeCompleted, "")
	}()

	coord.Shutdown()
	waitPhase(t, coord, PhaseDraining)
	// Pending timers: the coordinator's drain timer + the fake-DB query timer.
	waitPending(t, clk, 2)
	clk.Advance(60 * time.Millisecond) // query completes inside drain window
	waitDone(t, coord)

	st := coord.State()
	if st.DrainTimedOut {
		t.Errorf("drain should not have timed out: %+v", st)
	}
	if st.InFlight != 0 {
		t.Errorf("in-flight should be 0, got %d", st.InFlight)
	}
	if len(st.Ledger) != 1 || st.Ledger[0].Outcome != ledger.OutcomeCompleted {
		t.Errorf("expected single completed ledger entry, got %+v", st.Ledger)
	}
	// LIFO: fake-queue then fake-db.
	if len(st.CloseOrder) != 2 ||
		st.CloseOrder[0].Name != "fake-queue" || st.CloseOrder[1].Name != "fake-db" {
		t.Errorf("unexpected close order: %+v", st.CloseOrder)
	}
}

func TestDrainTimeout_CancelsLongRequest(t *testing.T) {
	clk, coord, db, _ := newHarness(t, 100*time.Millisecond)

	id, rootCtx, ok := coord.TryAccept("/work?ms=30s", "request")
	if !ok {
		t.Fatal("request should be accepted pre-shutdown")
	}
	finished := make(chan ledger.Outcome, 1)
	go func() {
		_, err := db.SlowQuery(rootCtx, "stuck", 30*time.Second)
		outcome := ledger.OutcomeCompleted
		if err != nil {
			outcome = ledger.OutcomeCancelled
		}
		coord.Finish(id, outcome, errString(err))
		finished <- outcome
	}()

	coord.Shutdown()
	waitPhase(t, coord, PhaseDraining)
	// Pending timers: drain timer + the stuck query's 30s timer.
	waitPending(t, clk, 2)
	clk.Advance(100 * time.Millisecond) // drain timer fires; cancel phase starts
	waitDone(t, coord)

	st := coord.State()
	if !st.DrainTimedOut {
		t.Errorf("drain should have timed out: %+v", st)
	}
	select {
	case outcome := <-finished:
		if outcome != ledger.OutcomeCancelled {
			t.Errorf("long request should be cancelled, got %s", outcome)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("long request never finished")
	}
	if got := st.Ledger[0].Outcome; got != ledger.OutcomeCancelled {
		t.Errorf("ledger outcome = %s, want cancelled", got)
	}
	if st.Ledger[0].Detail != context.Canceled.Error() {
		t.Errorf("ledger detail = %q, want context canceled", st.Ledger[0].Detail)
	}
}

func TestRejectsNewWorkAndTasksAfterStopAccepting(t *testing.T) {
	_, coord, _, _ := newHarness(t, time.Second)
	coord.Shutdown()

	// Stop-accepting is synchronous with the shutdown signal.
	if coord.Accepting() {
		t.Fatal("should not be accepting after shutdown")
	}
	if _, _, ok := coord.TryAccept("/work", "request"); ok {
		t.Error("new request must be rejected after stop-accepting")
	}
	if _, _, ok := coord.TryAccept("/task", "background-task"); ok {
		t.Error("new background task must be rejected after stop-accepting")
	}

	// Nothing in flight; drain completes immediately through to close.
	waitDone(t, coord)
	st := coord.State()
	if st.RejectedRequests != 1 || st.RejectedTasks != 1 {
		t.Errorf("reject counters wrong: %+v", st)
	}
}

func TestRepeatedShutdownSignalsIdempotent(t *testing.T) {
	clk, coord, _, _ := newHarness(t, 50*time.Millisecond)

	d1 := coord.Shutdown()
	if coord.Accepting() {
		t.Fatal("stop-accepting must be synchronous with the shutdown signal")
	}
	d2 := coord.Shutdown()
	d3 := coord.Shutdown()
	if d1 != d2 || d2 != d3 {
		t.Fatal("repeated Shutdown calls must return the same done channel")
	}

	clk.Advance(60 * time.Millisecond)
	waitDone(t, coord)

	st := coord.State()
	if st.ShutdownSignals != 3 {
		t.Errorf("signals = %d, want 3", st.ShutdownSignals)
	}
	wantPhases := []Phase{PhaseReceiving, PhaseDraining, PhaseCancelling, PhaseClosing, PhaseDone}
	if len(st.PhaseLog) != len(wantPhases) {
		t.Fatalf("phase log = %+v", st.PhaseLog)
	}
	for i, want := range wantPhases {
		if st.PhaseLog[i].Phase != want {
			t.Errorf("phase[%d] = %s, want %s", i, st.PhaseLog[i].Phase, want)
		}
	}
	// Calling again after completion must not panic or change anything.
	coord.Shutdown()
	if coord.State().ShutdownSignals != 4 {
		t.Errorf("signals = %d, want 4", coord.State().ShutdownSignals)
	}
}

func TestBackgroundTaskCannotStartAfterReceivingEnds(t *testing.T) {
	clk, coord, _, queue := newHarness(t, 500*time.Millisecond)

	// Pre-shutdown task completes during the drain window.
	taskID, taskCtx, ok := coord.TryAccept("/task", "background-task")
	if !ok {
		t.Fatal("background task should be accepted pre-shutdown")
	}
	go func() {
		err := queue.Publish(taskCtx, "job")
		if err != nil {
			coord.Finish(taskID, ledger.OutcomeCancelled, err.Error())
			return
		}
		coord.Finish(taskID, ledger.OutcomeCompleted, "")
	}()

	coord.Shutdown()
	waitPhase(t, coord, PhaseDraining)

	if _, _, ok := coord.TryAccept("/task", "background-task"); ok {
		t.Error("late background task must be rejected during drain")
	}

	// Pending timers: drain timer + the queue publish latency timer.
	waitPending(t, clk, 2)
	clk.Advance(20 * time.Millisecond) // publish latency elapses
	waitDone(t, coord)

	st := coord.State()
	if st.RejectedTasks != 1 {
		t.Errorf("rejected tasks = %d, want 1", st.RejectedTasks)
	}
	for _, e := range st.Ledger {
		if e.Kind == "background-task" && e.Outcome != ledger.OutcomeCompleted {
			t.Errorf("pre-shutdown task outcome = %s, want completed", e.Outcome)
		}
	}
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

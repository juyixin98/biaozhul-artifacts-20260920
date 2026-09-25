package scheduler

import (
	"context"
	"testing"
	"time"
)

var t0 = time.Date(2026, 9, 23, 12, 0, 0, 0, time.UTC)

// waitFor polls cond until it holds or the (real-time) timeout elapses.
func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out waiting for: %s", what)
}

func countEvents(log *EventLog, typ EventType) int {
	n := 0
	for _, e := range log.Events() {
		if e.Type == typ {
			n++
		}
	}
	return n
}

func newTestScheduler(clock *ManualClock) *Scheduler {
	return New(clock, SleepExecutor{Clock: clock}, NewEventLog(clock))
}

// TestAdmissionHandComputable verifies the conservative admission test
// against a task set whose feasibility is computed by hand:
//
//	t=0, nothing running.
//	J1: bound=10s, deadline=t0+30s  -> admit
//	J2: bound=15s, deadline=t0+20s  -> admit (EDF: J2 ends 15<=20, J1 ends 25<=30)
//	J3: bound=10s, deadline=t0+24s  -> REJECT (EDF: J2 15<=20, J3 25>24)
//	J4: bound=5s,  deadline=t0+40s  -> admit (EDF: J2 15, J1 25, J4 30<=40)
func TestAdmissionHandComputable(t *testing.T) {
	clock := NewManualClock(t0)
	s := newTestScheduler(clock) // worker not started: nothing runs

	submit := func(id string, bound, dl time.Duration) error {
		_, err := s.Submit(JobSpec{ID: id, ExecBound: bound, Deadline: t0.Add(dl)})
		return err
	}

	if err := submit("J1", 10*time.Second, 30*time.Second); err != nil {
		t.Fatalf("J1 should be admitted: %v", err)
	}
	if err := submit("J2", 15*time.Second, 20*time.Second); err != nil {
		t.Fatalf("J2 should be admitted: %v", err)
	}
	if err := submit("J3", 10*time.Second, 24*time.Second); err != ErrInfeasible {
		t.Fatalf("J3 should be rejected infeasible, got %v", err)
	}
	if err := submit("J4", 5*time.Second, 40*time.Second); err != nil {
		t.Fatalf("J4 should be admitted: %v", err)
	}

	j3, ok := s.Get("J3")
	if !ok || j3.State != StateRejectedInfeasible {
		t.Fatalf("J3 state = %v, want REJECTED_INFEASIBLE", j3)
	}

	st := s.Stats()
	if st.Submitted != 4 || st.Admitted != 3 || st.RejectedInfeasible != 1 {
		t.Fatalf("stats = %+v, want submitted=4 admitted=3 rejected=1", st)
	}
	// A rejected job must never touch the machine.
	if st.SlotAcquired != 0 || st.SlotReleased != 0 {
		t.Fatalf("rejected job touched the slot: %+v", st)
	}
	if got := countEvents(s.Log(), EventRejectedInfeasible); got != 1 {
		t.Fatalf("rejected_infeasible events = %d, want 1", got)
	}
}

// TestTimeoutDistinguishedFromInfeasible runs a job whose real execution
// (25s) exceeds its declared bound (10s): it must be admitted (the schedule
// is feasible w.r.t. the declared bound) and then killed at t0+10s with
// state TIMEOUT — a different outcome from REJECTED_INFEASIBLE.
func TestTimeoutDistinguishedFromInfeasible(t *testing.T) {
	clock := NewManualClock(t0)
	s := newTestScheduler(clock)
	s.Start()
	defer s.Stop()

	j, err := s.Submit(JobSpec{
		ID: "overrun", ExecBound: 10 * time.Second,
		Deadline: t0.Add(100 * time.Second), SimActual: 25 * time.Second,
	})
	if err != nil {
		t.Fatalf("job must be admitted: %v", err)
	}
	waitFor(t, "job running", func() bool {
		j, _ := s.Get("overrun")
		return j.State == StateRunning
	})
	// Both the executor's 25s timer and the scheduler's 10s timeout must be
	// armed before time moves.
	waitFor(t, "timers armed", func() bool { return clock.Pending() >= 2 })

	clock.Advance(10 * time.Second) // exactly the declared bound

	waitFor(t, "job timed out", func() bool {
		j, _ := s.Get("overrun")
		return j.State == StateTimeout
	})
	j, _ = s.Get("overrun")
	if !j.FinishedAt.Equal(t0.Add(10 * time.Second)) {
		t.Fatalf("timeout stop at %v, want %v", j.FinishedAt, t0.Add(10*time.Second))
	}

	st := s.Stats()
	if st.TimedOut != 1 || st.Completed != 0 || st.RejectedInfeasible != 0 {
		t.Fatalf("stats = %+v, want timed_out=1 only", st)
	}
	// A job that overran its bound counts as a deadline miss.
	if st.DeadlineMissed != 1 || st.DeadlineMet != 0 {
		t.Fatalf("deadline stats = %+v, want missed=1 met=0", st)
	}
	// Resource conservation: the single slot was acquired and released once.
	if st.SlotAcquired != 1 || st.SlotReleased != 1 || st.SlotInUse {
		t.Fatalf("slot not conserved: %+v", st)
	}
	if countEvents(s.Log(), EventTimeout) != 1 || countEvents(s.Log(), EventSlotReleased) != 1 {
		t.Fatalf("expected exactly one timeout and one slot_released event")
	}
}

// TestQueuedCancelResourceConservation cancels a job still waiting in the
// queue: it holds no resource, so nothing may be released for it, and a
// duplicate cancel must fail instead of releasing twice.
func TestQueuedCancelResourceConservation(t *testing.T) {
	clock := NewManualClock(t0)
	s := newTestScheduler(clock)
	s.Start()
	defer s.Stop()

	if _, err := s.Submit(JobSpec{ID: "A", ExecBound: 20 * time.Second, Deadline: t0.Add(100 * time.Second), SimActual: 10 * time.Second}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Submit(JobSpec{ID: "B", ExecBound: 5 * time.Second, Deadline: t0.Add(100 * time.Second), SimActual: 5 * time.Second}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "A running", func() bool {
		j, _ := s.Get("A")
		return j.State == StateRunning
	})
	// A's executor timer (10s) and timeout timer (20s) must be armed.
	waitFor(t, "timers armed", func() bool { return clock.Pending() >= 2 })

	if err := s.Cancel("B"); err != nil {
		t.Fatalf("cancel queued B: %v", err)
	}
	if err := s.Cancel("B"); err != ErrNotCancellable {
		t.Fatalf("second cancel of B = %v, want ErrNotCancellable", err)
	}
	if j, _ := s.Get("B"); j.State != StateCancelled {
		t.Fatalf("B state = %v, want CANCELLED", j.State)
	}

	clock.Advance(10 * time.Second) // A's real work finishes (bound was 20s)
	waitFor(t, "A completed", func() bool {
		j, _ := s.Get("A")
		return j.State == StateComplete
	})

	st := s.Stats()
	if st.Completed != 1 || st.Cancelled != 1 || st.DeadlineMet != 1 {
		t.Fatalf("stats = %+v, want completed=1 cancelled=1 met=1", st)
	}
	if st.SlotAcquired != 1 || st.SlotReleased != 1 || st.SlotInUse {
		t.Fatalf("slot not conserved: %+v", st)
	}
	// B never started and never held the slot.
	for _, e := range s.Log().Events() {
		if e.JobID == "B" && (e.Type == EventStarted || e.Type == EventSlotAcquired || e.Type == EventSlotReleased) {
			t.Fatalf("queued-cancelled B produced slot/start event: %+v", e)
		}
	}
	if got := countEvents(s.Log(), EventCancelled); got != 1 {
		t.Fatalf("cancelled events = %d, want 1 (duplicate cancel recorded nothing)", got)
	}
}

// TestCancelRunningReleasesSlotOnce cancels a running job and checks the
// slot comes back exactly once even if cancel is issued repeatedly.
func TestCancelRunningReleasesSlotOnce(t *testing.T) {
	clock := NewManualClock(t0)
	s := newTestScheduler(clock)
	s.Start()
	defer s.Stop()

	if _, err := s.Submit(JobSpec{ID: "C", ExecBound: 10 * time.Second, Deadline: t0.Add(100 * time.Second), SimActual: time.Hour}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "C running", func() bool {
		j, _ := s.Get("C")
		return j.State == StateRunning
	})

	if err := s.Cancel("C"); err != nil {
		t.Fatalf("cancel running C: %v", err)
	}
	waitFor(t, "C cancelled", func() bool {
		j, _ := s.Get("C")
		return j.State == StateCancelled
	})
	if err := s.Cancel("C"); err != ErrNotCancellable {
		t.Fatalf("second cancel of C = %v, want ErrNotCancellable", err)
	}

	st := s.Stats()
	if st.SlotAcquired != 1 || st.SlotReleased != 1 || st.SlotInUse {
		t.Fatalf("slot not conserved: %+v", st)
	}
	if got := countEvents(s.Log(), EventSlotReleased); got != 1 {
		t.Fatalf("slot_released events = %d, want exactly 1", got)
	}
}

// TestExecutorCancellationPropagates ensures a timed-out job's executor
// observes context cancellation (i.e. the machine is actually freed).
func TestExecutorCancellationPropagates(t *testing.T) {
	clock := NewManualClock(t0)
	sawCancel := make(chan struct{}, 1)
	exec := ExecutorFunc(func(ctx context.Context, j *Job) error {
		select {
		case <-ctx.Done():
			sawCancel <- struct{}{}
			return ctx.Err()
		case <-clock.After(time.Hour):
			return nil
		}
	})
	s := New(clock, exec, NewEventLog(clock))
	s.Start()
	defer s.Stop()

	if _, err := s.Submit(JobSpec{ID: "X", ExecBound: 5 * time.Second, Deadline: t0.Add(time.Minute)}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "X running", func() bool {
		j, _ := s.Get("X")
		return j.State == StateRunning
	})
	clock.Advance(5 * time.Second)
	select {
	case <-sawCancel:
	case <-time.After(5 * time.Second):
		t.Fatal("executor did not observe cancellation after timeout")
	}
}

package deadlineadm

import (
	"errors"
	"testing"
	"time"

	"deadlineadm/event"
)

// TestScheduler_BaselineEDF is the first hand-computed acceptance scenario:
// three unit-demand jobs on a unit machine execute strictly in deadline
// order; a fourth job that would push the last completion past a deadline is
// refused at submission (predicted infeasible), not at run time.
func TestScheduler_BaselineEDF(t *testing.T) {
	env := NewTestEnv(t, Config{Capacity: 1, Overrun: PolicyKillAtBudget})

	// All at t=0:
	//   j1 budget 30 deadline 100, honest payload sleep 30
	//   j2 budget 50 deadline 80,  honest payload sleep 50
	//   j3 budget 20 deadline 120, honest payload sleep 20
	mustSubmit(t, env, "j1", "sleep:30", 1, 100, 30)
	mustSubmit(t, env, "j2", "sleep:50", 1, 80, 50)
	mustSubmit(t, env, "j3", "sleep:20", 1, 120, 20)

	// j4: budget 40 deadline 110. EDF schedule would be
	//   j2 [0,50], j4 [50,90], j1 [90,120] -> j1 misses 100 -> reject.
	err := env.Submit("j4", "sleep:40", 1, 110, 40)
	if !errors.Is(err, ErrInfeasible) {
		t.Fatalf("j4 must be rejected infeasible, got %v", err)
	}

	env.RunToEnd(200)

	// Hand-computed timeline under online non-preemptive dispatch: j1 is
	// admitted first and starts immediately [0,30]; j2 then [30,80]; j3
	// [80,100]. All meet their deadlines.
	ends := map[string]int64{"j1": 30, "j2": 80, "j3": 100}
	for id, want := range ends {
		v, ok := env.Sched.Get(id)
		if !ok {
			t.Fatalf("missing job %s", id)
		}
		if v.Status != StatusCompleted {
			t.Errorf("%s status %s, want completed", id, v.Status)
		}
		if v.EndMs == nil || *v.EndMs != want {
			t.Errorf("%s ends at %v, want %d", id, v.EndMs, want)
		}
	}

	st := env.Sched.Stats()
	if st.Completed != 3 {
		t.Errorf("completed=%d want 3", st.Completed)
	}
	if st.Rejected != 1 {
		t.Errorf("rejected=%d want 1 (rejected submissions still counted via events)", st.Rejected)
	}
	if st.MetDeadline != 3 || st.MissedDeadline != 0 || st.Timeout != 0 {
		t.Errorf("deadline stats wrong: %+v", st)
	}
	if !st.Conserved {
		t.Errorf("resource ledger not balanced: acquired=%d released=%d inUse=%d",
			st.AcquiredTotal, st.ReleasedTotal, st.InUse)
	}

	// Structured events must tell the same story.
	ev := env.Sched.Events()
	counts := map[event.Type]int{}
	for _, e := range ev {
		counts[e.Type]++
	}
	if counts[event.Rejected] != 1 || counts[event.Started] != 3 || counts[event.Completed] != 3 {
		t.Errorf("event counts wrong: %+v", counts)
	}
}

// TestScheduler_RuntimeOverrunIsTimeout proves the distinction between the two
// failure kinds: a job that claims 20 ms but runs 60 is killed at the
// declared bound and counted as a runtime TIMEOUT, while the later EDF jobs
// still meet their deadlines because the kill preserved the schedule.
func TestScheduler_RuntimeOverrunIsTimeout(t *testing.T) {
	env := NewTestEnv(t, Config{Capacity: 1, Overrun: PolicyKillAtBudget})

	mustSubmit(t, env, "bad", "sleep:60", 1, 100, 20) // claims 20, runs 60
	mustSubmit(t, env, "a", "sleep:30", 1, 120, 30)
	mustSubmit(t, env, "b", "sleep:30", 1, 200, 30)

	env.RunToEnd(250)

	bad, _ := env.Sched.Get("bad")
	if bad.Status != StatusTimeout {
		t.Fatalf("bad status=%s want timeout", bad.Status)
	}
	if bad.Reason == "" {
		t.Error("timeout should carry a reason")
	}
	if bad.RanMs < 20 || bad.RanMs > 25 {
		t.Errorf("bad occupied %d ms before kill, want ~20 (declared bound)", bad.RanMs)
	}
	// bad was killed at t=20; then a [20,50], b [50,80] — both on time.
	wantEnd := map[string]int64{"a": 50, "b": 80}
	for id, w := range wantEnd {
		v, _ := env.Sched.Get(id)
		if v.Status != StatusCompleted {
			t.Errorf("%s status=%s want completed (cascade avoided by kill)", id, v.Status)
		}
		if v.EndMs == nil || *v.EndMs != w {
			t.Errorf("%s ends %v want %d", id, v.EndMs, w)
		}
	}

	st := env.Sched.Stats()
	if st.Timeout != 1 || st.Completed != 2 || st.DeadlineMissed != 0 {
		t.Errorf("stats wrong: %+v", st)
	}
	if st.MetDeadline != 2 || st.MissedDeadline != 0 {
		t.Errorf("deadline accounting wrong: %+v", st)
	}
	if !st.Conserved || st.InUse != 0 {
		t.Errorf("resource ledger wrong: %+v", st)
	}
}

// TestScheduler_ObserveModeCascade shows the alternative policy: an overrunning
// job is allowed past its declared bound and killed only at its deadline; it
// then counts as a deadline miss and can cascade a miss onto a later EDF job.
func TestScheduler_ObserveModeCascade(t *testing.T) {
	env := NewTestEnv(t, Config{Capacity: 1, Overrun: PolicyObserve})

	// All release at 0, honest later jobs claim 30 ms:
	//   bad: claims 30, deadline 50, actually runs 80
	//   a:   claims 30, deadline 80
	//   b:   claims 30, deadline 100
	// Under the declared claims admission accepts all three (EDF bad [0,30],
	// a [30,60], b [60,90] all feasible). At run time bad is allowed past its
	// 30 ms claim and is killed at its deadline 50 (deadline miss). The lost
	// 50 ms cascades: a runs [50,80] (exactly on time), b runs [80,110] and
	// misses deadline 100.
	mustSubmit(t, env, "bad", "sleep:80", 1, 50, 30)
	mustSubmit(t, env, "a", "sleep:30", 1, 80, 30)
	mustSubmit(t, env, "b", "sleep:30", 1, 100, 30)

	env.RunToEnd(200)

	bad, _ := env.Sched.Get("bad")
	if bad.Status != StatusDeadlineMissed {
		t.Fatalf("bad status=%s want deadline_missed", bad.Status)
	}
	a, _ := env.Sched.Get("a")
	b, _ := env.Sched.Get("b")
	if a.Status != StatusCompleted {
		t.Errorf("a status=%s want completed at 80<=80", a.Status)
	}
	if b.Status != StatusDeadlineMissed {
		t.Errorf("b status=%s want deadline_missed (cascade at 110>100)", b.Status)
	}

	st := env.Sched.Stats()
	if st.Timeout != 0 {
		t.Errorf("observe mode must not count budget overruns as timeouts: %+v", st)
	}
	if st.DeadlineMissed != 2 {
		t.Errorf("deadline_missed=%d want 2 (bad + cascaded b): %+v", st.DeadlineMissed, st)
	}
	if st.MissedDeadline != 2 {
		t.Errorf("missed_deadline=%d want 2", st.MissedDeadline)
	}
	if st.MetDeadline != 1 {
		t.Errorf("met_deadline=%d want 1 (a)", st.MetDeadline)
	}
	if !st.Conserved {
		t.Errorf("resource ledger unbalanced: %+v", st)
	}
}

// TestScheduler_QueuedCancelReleasesNothingTwice covers the core cancellation
// safety properties: canceling a job while it waits never touches resources;
// canceling a running job frees its demand exactly once when the executor
// result arrives; repeated cancels are errors with no extra release or event.
func TestScheduler_QueuedCancelReleasesNothingTwice(t *testing.T) {
	env := NewTestEnv(t, Config{Capacity: 1, Overrun: PolicyKillAtBudget})

	// long holds the machine [0,120]; two jobs queue behind it.
	mustSubmit(t, env, "long", "sleep:120", 1, 500, 120)
	mustSubmit(t, env, "q1", "sleep:10", 1, 500, 10)
	mustSubmit(t, env, "q2", "sleep:10", 1, 500, 10)

	env.AdvanceMS(5)
	st := env.Sched.Stats()
	if st.InUse != 1 || st.Queued != 2 || st.AcquiredTotal != 1 || st.ReleasedTotal != 0 {
		t.Fatalf("unexpected pre-cancel state: %+v", st)
	}

	// Cancel a queued job: no resources change.
	if err := env.Sched.Cancel("q1"); err != nil {
		t.Fatalf("cancel q1: %v", err)
	}
	q1, _ := env.Sched.Get("q1")
	if q1.Status != StatusCanceled {
		t.Fatalf("q1 status=%s", q1.Status)
	}
	st = env.Sched.Stats()
	if st.InUse != 1 || st.AcquiredTotal != 1 || st.ReleasedTotal != 0 || st.Queued != 1 {
		t.Fatalf("queued cancel must not touch resources: %+v", st)
	}

	// Second cancel on the same job is an error and changes nothing.
	before := env.Sched.Stats()
	err := env.Sched.Cancel("q1")
	if !errors.Is(err, ErrTerminal) {
		t.Fatalf("repeat cancel want ErrTerminal, got %v", err)
	}
	after := env.Sched.Stats()
	if before.ReleasedTotal != after.ReleasedTotal || before.Canceled != after.Canceled {
		t.Fatalf("repeat cancel altered state: before=%+v after=%+v", before, after)
	}

	// Cancel the running job: resources release exactly once, after the
	// executor confirms termination.
	if err := env.Sched.Cancel("long"); err != nil {
		t.Fatalf("cancel long: %v", err)
	}
	env.DrainKills() // await the asynchronous kill result and its pump cascade
	long, _ := env.Sched.Get("long")
	if long.Status != StatusCanceled {
		t.Fatalf("long status=%s want canceled", long.Status)
	}
	st = env.Sched.Stats()
	if st.AcquiredTotal != 2 || st.ReleasedTotal != 1 || st.InUse != 1 {
		// long acquired+released (1); q2 starts after the free-up (1) and runs.
		t.Fatalf("ledger right after kill: acquired=%d released=%d inUse=%d",
			st.AcquiredTotal, st.ReleasedTotal, st.InUse)
	}
	if !st.Conserved {
		t.Fatalf("resource invariant broken: %+v", st)
	}

	// Drain the remaining queued job and re-check final conservation.
	env.RunToEnd(600)
	st = env.Sched.Stats()
	if st.InUse != 0 || st.AcquiredTotal != st.ReleasedTotal {
		t.Fatalf("final ledger unbalanced: %+v", st)
	}
	if !st.Conserved {
		t.Fatalf("final conserved=false: %+v", st)
	}
	if st.Canceled != 2 {
		t.Errorf("canceled=%d want 2", st.Canceled)
	}

	// Exactly one canceled event per canceled job.
	cancelEvents := 0
	for _, e := range env.Sched.Events() {
		if e.Type == event.Canceled {
			cancelEvents++
		}
	}
	if cancelEvents != 2 {
		t.Errorf("canceled events=%d want 2 (no duplicates)", cancelEvents)
	}
}

// TestScheduler_QueuedDeadlineMiss covers a job whose deadline passes while
// it is still waiting: it is finalized as deadline_missed without ever
// acquiring resources.
func TestScheduler_QueuedDeadlineMiss(t *testing.T) {
	env := NewTestEnv(t, Config{Capacity: 1, Overrun: PolicyKillAtBudget})
	mustSubmit(t, env, "long", "sleep:100", 1, 500, 100)
	mustSubmit(t, env, "soon", "sleep:10", 1, 50, 10) // cannot start before t=100

	env.RunToEnd(200)

	soon, _ := env.Sched.Get("soon")
	if soon.Status != StatusDeadlineMissed {
		t.Fatalf("soon status=%s want deadline_missed", soon.Status)
	}
	if soon.StartMs != nil {
		t.Errorf("soon never started, but has start %v", soon.StartMs)
	}
	st := env.Sched.Stats()
	if st.AcquiredTotal != 1 || st.ReleasedTotal != 1 || st.InUse != 0 {
		t.Errorf("soon held no resources: %+v", st)
	}
	if st.MissedDeadline != 1 {
		t.Errorf("missed=%d want 1", st.MissedDeadline)
	}
}

// TestScheduler_ParallelConservation exercises capacity>1 with repeated
// submissions and completion interleaving, stressing the acquire/release
// ledger and the EDF pump.
func TestScheduler_ParallelConservation(t *testing.T) {
	env := NewTestEnv(t, Config{Capacity: 3, Overrun: PolicyKillAtBudget})
	mustSubmit(t, env, "a", "sleep:40", 2, 100, 40)
	mustSubmit(t, env, "b", "sleep:40", 2, 120, 40) // queued: a holds 2/3, b needs 2
	mustSubmit(t, env, "c", "sleep:20", 1, 100, 20) // fits next to a
	env.AdvanceMS(1)
	if st := env.Sched.Stats(); st.InUse != 3 || st.Queued != 1 {
		t.Fatalf("want inUse=3 queued=1, got %+v", st)
	}
	env.RunToEnd(200)
	for _, id := range []string{"a", "b", "c"} {
		v, _ := env.Sched.Get(id)
		if v.Status != StatusCompleted {
			t.Errorf("%s=%s", id, v.Status)
		}
	}
	st := env.Sched.Stats()
	if !st.Conserved || st.InUse != 0 || st.AcquiredTotal != 5 {
		t.Errorf("ledger wrong: %+v", st)
	}
}

// TestScheduler_FailedJobReleasesResources checks executor-reported failures.
func TestScheduler_FailedJobReleasesResources(t *testing.T) {
	env := NewTestEnv(t, Config{Capacity: 1})
	mustSubmit(t, env, "f", "fail:boom", 1, 100, 10)
	mustSubmit(t, env, "g", "sleep:10", 1, 100, 10)
	env.RunToEnd(150)
	f, _ := env.Sched.Get("f")
	if f.Status != StatusFailed {
		t.Fatalf("f=%s want failed", f.Status)
	}
	g, _ := env.Sched.Get("g")
	if g.Status != StatusCompleted || g.EndMs == nil || *g.EndMs != 10 {
		t.Errorf("g should run after failed f by t=10, got %+v", g)
	}
	st := env.Sched.Stats()
	if !st.Conserved || st.InUse != 0 {
		t.Errorf("ledger: %+v", st)
	}
}

// TestScheduler_CancelUnknownAndValidation covers edge errors.
func TestScheduler_CancelUnknownAndValidation(t *testing.T) {
	env := NewTestEnv(t, Config{Capacity: 2})
	if err := env.Sched.Cancel("ghost"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("ghost cancel: %v", err)
	}
	if err := env.Submit("", "sleep:1", 1, 10, 1); err == nil {
		t.Fatal("empty id should fail")
	}
	if err := env.Submit("big", "sleep:1", 3, 10, 1); !errors.Is(err, ErrInfeasible) {
		t.Fatalf("oversize demand: %v", err)
	}
	if err := env.Submit("tight", "sleep:1", 1, 5, 10); !errors.Is(err, ErrInfeasible) {
		t.Fatalf("budget>slack must be infeasible: %v", err)
	}
	if err := env.Submit("dup", "sleep:1", 1, 10, 1); err != nil {
		t.Fatal(err)
	}
	if err := env.Submit("dup", "sleep:1", 1, 10, 1); err == nil {
		t.Fatal("duplicate id should fail")
	}
}

func mustSubmit(t *testing.T, env *TestEnv, id, payload string, demand int, deadlineMs, budgetMs int64) {
	t.Helper()
	if err := env.Submit(id, payload, demand, deadlineMs, budgetMs); err != nil {
		t.Fatalf("submit %s: %v", id, err)
	}
}

// TestScheduler_EventsAreStructuredAndOrdered asserts event ordering and
// content shape on a short schedule.
func TestScheduler_EventsAreStructuredAndOrdered(t *testing.T) {
	env := NewTestEnv(t, Config{Capacity: 1})
	mustSubmit(t, env, "j", "sleep:10", 1, 50, 10)
	env.RunToEnd(60)
	ev := env.Sched.Events()
	var seq []event.Type
	var last int64
	for _, e := range ev {
		if e.Seq <= last {
			t.Fatalf("event seqs not strictly increasing: %d after %d", e.Seq, last)
		}
		last = e.Seq
		if e.TimeMs < 0 || e.JobID != "j" {
			t.Fatalf("bad event %+v", e)
		}
		seq = append(seq, e.Type)
	}
	want := []event.Type{event.Submitted, event.Admitted, event.Started, event.Completed}
	if len(seq) < len(want) {
		t.Fatalf("events %v want prefix %v", seq, want)
	}
	for i := range want {
		if seq[i] != want[i] {
			t.Fatalf("event[%d]=%s want %s (full=%v)", i, seq[i], want[i], seq)
		}
	}
	// Sanity: time package import retained for potential wall-clock tests.
	_ = time.Millisecond
}

package deadlineadm

import (
	"testing"
	"time"

	"deadlineadm/clock"
	"deadlineadm/executor"
)

// TestEnv bundles a scheduler driven by a fake clock with its deterministic
// driver. Intended for in-package tests.
type TestEnv struct {
	Sched *Scheduler
	Clk   *clock.FakeClock
	Exec  *executor.ScriptExecutor
	drv   *FakeDriver
	t     *testing.T
}

// NewTestEnv creates a started scheduler over a fake clock and script
// executor, anchored at the Unix epoch.
func NewTestEnv(t *testing.T, cfg Config) *TestEnv {
	t.Helper()
	clk := clock.NewFakeClockAt(time.Unix(0, 0).UTC())
	ex := executor.NewScriptExecutor(clk)
	sched := New(cfg, clk, ex, nil)
	sched.Start()
	t.Cleanup(sched.Stop)
	drv := NewFakeDriver(sched, clk)
	return &TestEnv{Sched: sched, Clk: clk, Exec: ex, drv: drv, t: t}
}

// SubmitAt is a test convenience: deadline/budget are integer simulated
// milliseconds relative to the Unix-epoch clock origin.
func (e *TestEnv) Submit(id, payload string, demand int, deadlineMs, budgetMs int64) error {
	t0 := time.Unix(0, 0).UTC()
	return e.Sched.Submit(Request{
		ID: id, Payload: payload, Demand: demand,
		Deadline: t0.Add(time.Duration(deadlineMs) * time.Millisecond),
		Budget:   time.Duration(budgetMs) * time.Millisecond,
	})
}

// AdvanceUntil fires every due event up to t, settling cascades.
func (e *TestEnv) AdvanceUntil(t time.Time) { e.drv.Advance(t) }

// AdvanceMS advances by d simulated milliseconds.
func (e *TestEnv) AdvanceMS(d int64) { e.drv.AdvanceMS(d) }

// DrainKills waits for directly-canceled running jobs to finalize.
func (e *TestEnv) DrainKills() {
	e.t.Helper()
	e.Sched.waitKills()
	e.Sched.sync()
}

// RunToEnd keeps firing events until none are pending or until simulated time
// passes limitMs. Meant for schedules known to terminate.
func (e *TestEnv) RunToEnd(limitMs int64) {
	e.t.Helper()
	e.drv.RunToEnd(limitMs)
}

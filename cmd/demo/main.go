// Command demo runs the hand-computed acceptance scenarios deterministically on
// a fake clock and prints, for each: the task set, the resulting event
// timeline, deadline statistics and the resource-conservation ledger.
//
// Run with:  go run ./cmd/demo
package main

import (
	"fmt"
	"strings"
	"time"

	"deadlineadm"
	"deadlineadm/clock"
	"deadlineadm/event"
	"deadlineadm/executor"
)

type job struct {
	id, payload string
	deadline    int64
	budget      int64
}

func main() {
	scenario1()
	fmt.Println()
	scenario2()
	fmt.Println()
	scenario3()
}

func scenario1() {
	fmt.Println(strings.Repeat("=", 78))
	fmt.Println("SCENARIO 1 - EDF + conservative admission (predicted infeasible)")
	fmt.Println(strings.Repeat("=", 78))
	fmt.Println(`
Unit machine, all release at t=0 (ms):
  j1 budget=30 deadline=100  honest sleep:30
  j2 budget=50 deadline= 80  honest sleep:50
  j3 budget=20 deadline=120  honest sleep:20
  j4 budget=40 deadline=110  honest sleep:40
Hand check (online, non-preemptive): j1 [0,30], j2 [30,80], j3 [80,100] all
meet their deadlines. Adding j4 cannot: there is no EDF order that completes
all four by their deadlines, so admission REFUSES j4 at submission (predicted
infeasible), before it ever runs.`)
	jobs := []job{
		{"j1", "sleep:30", 100, 30},
		{"j2", "sleep:50", 80, 50},
		{"j3", "sleep:20", 120, 20},
	}
	s, clk, drv := newScheduler(deadlineadm.PolicyKillAtBudget)
	defer s.Stop()
	for _, j := range jobs {
		submit(s, j)
	}
	submit(s, job{"j4", "sleep:40", 110, 40})
	drv.RunToEnd(200)
	printTimeline(s, clk)
	printStats(s)
}

func scenario2() {
	fmt.Println(strings.Repeat("=", 78))
	fmt.Println("SCENARIO 2 - runtime overrun is a TIMEOUT, distinct from a miss")
	fmt.Println(strings.Repeat("=", 78))
	fmt.Println(`
Unit machine, all release at t=0:
  bad budget=20 deadline=100  DECLARES 20 but actually runs 60 (sleep:60)
  a   budget=30 deadline=120  honest sleep:30
  b   budget=30 deadline=200  honest sleep:30
Admission trusts the declared 20 ms and accepts all three. At run time bad
exceeds its declared upper bound and is KILLED at t=20 -> runtime timeout (a
different outcome from "deadline missed"). The kill protects the rest of the
schedule: a runs [20,50] and b runs [50,80], both on time.`)
	jobs := []job{
		{"bad", "sleep:60", 100, 20},
		{"a", "sleep:30", 120, 30},
		{"b", "sleep:30", 200, 30},
	}
	s, clk, drv := newScheduler(deadlineadm.PolicyKillAtBudget)
	defer s.Stop()
	for _, j := range jobs {
		submit(s, j)
	}
	drv.RunToEnd(120)
	printTimeline(s, clk)
	printStats(s)
}

func scenario3() {
	fmt.Println(strings.Repeat("=", 78))
	fmt.Println("SCENARIO 3 - queued/running cancellation never double-releases")
	fmt.Println(strings.Repeat("=", 78))
	fmt.Println(`
Unit machine:
  long budget=120 deadline=500 honest sleep:120  (runs from t=0)
  q1   budget=10  deadline=500 honest sleep:10    (queued; CANCELED at t=5)
  q2   budget=10  deadline=500 honest sleep:10    (queued)
q1 never acquired resources, so its cancel frees none. Canceling q1 again is an
error that changes nothing. long is then canceled at t=5: its 1 unit is released
exactly once when termination is confirmed, which lets q2 run. Final ledger must
show acquired == released and one canceled event per canceled job.`)
	s, clk, drv := newScheduler(deadlineadm.PolicyKillAtBudget)
	defer s.Stop()
	mustSubmit(s, job{"long", "sleep:120", 500, 120})
	mustSubmit(s, job{"q1", "sleep:10", 500, 10})
	mustSubmit(s, job{"q2", "sleep:10", 500, 10})

	drv.AdvanceMS(5)
	fmt.Println("\n  at t=5: cancel queued q1")
	must(s.Cancel("q1"))
	if err := s.Cancel("q1"); err == nil {
		fmt.Println("  ERROR: second cancel of q1 should fail")
	} else {
		fmt.Printf("  second cancel q1 -> %v (no state change)\n", err)
	}
	fmt.Println("  at t=5: cancel running long")
	must(s.Cancel("long"))
	s.WaitKills()
	drv.RunToEnd(500)

	printTimeline(s, clk)
	printStats(s)
}

func newScheduler(policy deadlineadm.OverrunPolicy) (*deadlineadm.Scheduler, *clock.FakeClock, *deadlineadm.FakeDriver) {
	clk := clock.NewFakeClockAt(time.Unix(0, 0).UTC())
	ex := executor.NewScriptExecutor(clk)
	s := deadlineadm.New(deadlineadm.Config{Capacity: 1, Overrun: policy}, clk, ex, nil)
	s.Start()
	return s, clk, deadlineadm.NewFakeDriver(s, clk)
}

func submit(s *deadlineadm.Scheduler, j job) {
	err := s.Submit(req(j))
	if err != nil {
		fmt.Printf("  submit %-4s -> REJECTED: %v\n", j.id, err)
		return
	}
	fmt.Printf("  submit %-4s -> admitted (budget=%3d deadline=%3d payload=%q)\n",
		j.id, j.budget, j.deadline, j.payload)
}

func mustSubmit(s *deadlineadm.Scheduler, j job) { must(s.Submit(req(j))) }

func req(j job) deadlineadm.Request {
	return deadlineadm.Request{
		ID: j.id, Payload: j.payload, Demand: 1,
		Deadline: time.Unix(0, 0).UTC().Add(time.Duration(j.deadline) * time.Millisecond),
		Budget:   time.Duration(j.budget) * time.Millisecond,
	}
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}

func printTimeline(s *deadlineadm.Scheduler, clk *clock.FakeClock) {
	fmt.Println("\n  Structured event timeline (simulated time ms):")
	for _, e := range s.Events() {
		line := fmt.Sprintf("    t=%4d  %-16s job=%s", e.TimeMs, e.Type, e.JobID)
		if e.Reason != "" {
			line += "  " + e.Reason
		}
		fmt.Println(line)
	}
	_ = clk
	_ = event.Started
}

func printStats(s *deadlineadm.Scheduler) {
	st := s.Stats()
	fmt.Println("\n  Deadline statistics:")
	fmt.Printf("    completed       = %d\n", st.Completed)
	fmt.Printf("    deadline_missed = %d\n", st.DeadlineMissed)
	fmt.Printf("    timeout         = %d\n", st.Timeout)
	fmt.Printf("    canceled        = %d\n", st.Canceled)
	fmt.Printf("    failed          = %d\n", st.Failed)
	fmt.Printf("    rejected(submit)= %d\n", st.Rejected)
	fmt.Printf("    met deadline    = %d\n", st.MetDeadline)
	fmt.Printf("    missed deadline = %d (excludes timeout/canceled)\n", st.MissedDeadline)
	fmt.Println("  Resource ledger:")
	fmt.Printf("    capacity=%d in_use=%d acquired_total=%d released_total=%d\n",
		st.Capacity, st.InUse, st.AcquiredTotal, st.ReleasedTotal)
	verdict := "OK  (acquired == released + in_use)"
	if !st.Conserved {
		verdict = "BROKEN"
	}
	fmt.Printf("    conservation    = %v  %s\n", st.Conserved, verdict)

	fmt.Println("\n  Final job states:")
	for _, v := range s.List() {
		end := int64(-1)
		if v.EndMs != nil {
			end = *v.EndMs
		}
		fmt.Printf("    %-4s %-16s deadline=%3d end=%4d ran=%3d\n",
			v.ID, v.Status, v.DeadlineMs, end, v.RanMs)
	}
}

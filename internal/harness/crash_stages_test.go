package harness_test

import (
	"fmt"
	"testing"

	"twopc-sim/internal/harness"
)

// TestCrashAtEveryProtocolStage injects a crash at every named protocol
// milestone. For every stage the run must end in exactly one of:
// committed, aborted or blocked — never partial-commit — and the safety
// invariants must always hold.
func TestCrashAtEveryProtocolStage(t *testing.T) {
	cases := []struct {
		node, milestone string
		down            int64
	}{
		// coordinator stages
		{"coord", "c-begin-wal:T1", 40},
		{"coord", "c-prepare-sent:T1", 40},
		{"coord", "c-vote-recv:T1", 40},
		{"coord", "c-votes-complete:T1", 40},
		{"coord", "c-decision-wal:T1", 40},
		{"coord", "c-ack-recv:T1", 40},
		{"coord", "c-all-acked:T1", 40},
		// participant stages
		{"p1", "p-prepare-recv:T1", 40},
		{"p1", "p-prepared-wal:T1", 40},
		{"p1", "p-vote-sent:T1", 40},
		{"p1", "p-commit-recv:T1", 40},
		{"p1", "p-commit-wal:T1", 40},
		{"p2", "p-prepared-wal:T1", 40},
		{"p3", "p-commit-wal:T1", 40},
	}
	allowed := map[string]bool{"committed": true, "aborted": true, "blocked": true}
	for _, tc := range cases {
		tc := tc
		name := fmt.Sprintf("%s/%s", tc.node, tc.milestone)
		t.Run(name, func(t *testing.T) {
			s := baseSpec()
			s.Horizon = 600
			s.Crashes = []harness.Crash{{Node: tc.node, Milestone: tc.milestone, DownTicks: tc.down}}
			res := mustRun(t, s)
			assertNoInvariantErrors(t, res)
			out := statusOf(res, "T1")
			if !allowed[out.Status] {
				t.Fatalf("status=%q (committed=%v aborted=%v prepared=%v)",
					out.Status, out.CommittedNodes, out.AbortedNodes, out.PreparedNodes)
			}
		})
	}
}

// p-abort-wal requires an aborting transaction: force the NO vote policy and
// crash p1 exactly when it durably records the abort.
func TestCrashAtAbortStage(t *testing.T) {
	s := baseSpec()
	s.Horizon = 600
	s.Requests[0].Writes = toWrites("balance", "@@NO")
	s.Crashes = []harness.Crash{{Node: "p1", Milestone: "p-abort-wal:T1", DownTicks: 40}}
	res := mustRun(t, s)
	assertNoInvariantErrors(t, res)
	if got := statusOf(res, "T1").Status; got != "aborted" {
		t.Fatalf("status=%s want aborted", got)
	}
}

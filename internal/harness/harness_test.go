package harness_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"twopc-sim/internal/engine"
	"twopc-sim/internal/harness"
	"twopc-sim/internal/wal"
)

func toWrites(k, v string) []wal.KVWrite { return []wal.KVWrite{{Key: k, Value: v}} }

func mustRun(t *testing.T, s harness.Spec) harness.Result {
	t.Helper()
	res, err := harness.Run(s)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	return res
}

func baseSpec() harness.Spec {
	return harness.Spec{
		Seed:    42,
		Horizon: 400,
		Network: engine.NetConfig{BaseDelay: 1, Jitter: 1},
		Requests: []harness.Request{{
			TxnID:  "T1",
			At:     10,
			Writes: toWrites("balance", "100"),
		}},
	}
}

func milestoneCrash(node, m string, down int64) harness.Crash {
	return harness.Crash{Node: node, Milestone: m, DownTicks: down}
}

func statusOf(res harness.Result, txn string) harness.TxnOutcome {
	for _, x := range res.Transactions {
		if x.TxnID == txn {
			return x
		}
	}
	return harness.TxnOutcome{}
}

func assertNoInvariantErrors(t *testing.T, res harness.Result) {
	t.Helper()
	for _, e := range res.InvariantErrors {
		t.Errorf("invariant violation: %s", e)
	}
}

// TestHappyPath commits with no faults.
func TestHappyPath(t *testing.T) {
	res := mustRun(t, baseSpec())
	if got := statusOf(res, "T1").Status; got != "committed" {
		t.Fatalf("status=%s want committed", got)
	}
	if got := statusOf(res, "T1").ClientReported; got != "committed" {
		t.Fatalf("client=%q want committed", got)
	}
	assertNoInvariantErrors(t, res)
}

// TestNetworkFaults commits despite loss, duplication and reordering.
func TestNetworkFaults(t *testing.T) {
	s := baseSpec()
	s.Horizon = 800
	s.Network = engine.NetConfig{BaseDelay: 1, Jitter: 4, Loss: 0.35, Duplicate: 0.25}
	res := mustRun(t, s)
	if got := statusOf(res, "T1").Status; got != "committed" {
		t.Fatalf("status=%s want committed under network faults", got)
	}
	// The trace must actually exhibit the faults the test claims.
	var drops, dups int
	for _, r := range res.Trace {
		if r.Kind == "drop" {
			drops++
		}
		if r.Kind == "dup" {
			dups++
		}
	}
	if drops == 0 {
		t.Errorf("expected dropped messages in trace, got 0")
	}
	if dups == 0 {
		t.Errorf("expected duplicated messages in trace, got 0")
	}
	assertNoInvariantErrors(t, res)
}

// TestVoteNo forces a NO vote; the global outcome must be abort.
func TestVoteNo(t *testing.T) {
	s := baseSpec()
	s.Requests[0].Writes = toWrites("balance", "@@NO")
	res := mustRun(t, s)
	if got := statusOf(res, "T1").Status; got != "aborted" {
		t.Fatalf("status=%s want aborted", got)
	}
	assertNoInvariantErrors(t, res)
}

// TestParticipantCrashBeforePrepared: nothing durable survives; commit.
func TestParticipantCrashBeforePrepared(t *testing.T) {
	s := baseSpec()
	s.Crashes = []harness.Crash{milestoneCrash("p1", "p-prepare-recv:T1", 40)}
	res := mustRun(t, s)
	if got := statusOf(res, "T1").Status; got != "committed" {
		t.Fatalf("status=%s want committed", got)
	}
	assertNoInvariantErrors(t, res)
}

// TestParticipantCrashAfterPrepared: prepared participant must NOT abort
// unilaterally; it blocks/restarts, polls the coordinator, and commits.
func TestParticipantCrashAfterPrepared(t *testing.T) {
	s := baseSpec()
	s.Crashes = []harness.Crash{milestoneCrash("p1", "p-prepared-wal:T1", 50)}
	res := mustRun(t, s)
	if got := statusOf(res, "T1").Status; got != "committed" {
		t.Fatalf("status=%s want committed after prepared crash+restart", got)
	}
	assertNoInvariantErrors(t, res)
}

// TestParticipantCrashAfterCommit: commit survives, duplicate COMMIT acked.
func TestParticipantCrashAfterCommit(t *testing.T) {
	s := baseSpec()
	s.Crashes = []harness.Crash{milestoneCrash("p1", "p-commit-wal:T1", 50)}
	res := mustRun(t, s)
	if got := statusOf(res, "T1").Status; got != "committed" {
		t.Fatalf("status=%s want committed", got)
	}
	assertNoInvariantErrors(t, res)
}

// TestCoordCrashBeforeDecision: restart finds an undecided txn and aborts it.
func TestCoordCrashBeforeDecision(t *testing.T) {
	s := baseSpec()
	s.Crashes = []harness.Crash{milestoneCrash("coord", "c-votes-complete:T1", 40)}
	res := mustRun(t, s)
	if got := statusOf(res, "T1").Status; got != "aborted" {
		t.Fatalf("status=%s want aborted (presumed-abort at recovery)", got)
	}
	assertNoInvariantErrors(t, res)
}

// TestCoordCrashAfterDecision: durable COMMIT re-driven after restart.
func TestCoordCrashAfterDecision(t *testing.T) {
	s := baseSpec()
	s.Crashes = []harness.Crash{milestoneCrash("coord", "c-decision-wal:T1", 40)}
	res := mustRun(t, s)
	if got := statusOf(res, "T1").Status; got != "committed" {
		t.Fatalf("status=%s want committed from recovered decision", got)
	}
	assertNoInvariantErrors(t, res)
}

// TestBlockingCoordDown is the explicit blocking evidence: coordinator
// crashes after durable COMMIT and never returns. Participants stay
// PREPARED, keep polling, and must not abort.
func TestBlockingCoordDown(t *testing.T) {
	s := baseSpec()
	s.Horizon = 300
	s.Crashes = []harness.Crash{milestoneCrash("coord", "c-decision-wal:T1", 0)}
	res := mustRun(t, s)

	out := statusOf(res, "T1")
	if out.Status != "blocked" {
		t.Fatalf("status=%q want blocked", out.Status)
	}
	if out.CoordinatorUp {
		t.Errorf("coordinator should still be down")
	}
	if len(out.PreparedNodes) != 3 {
		t.Errorf("prepared nodes=%v want all 3 blocked", out.PreparedNodes)
	}
	if len(out.CommittedNodes) != 0 || len(out.AbortedNodes) != 0 {
		t.Errorf("no node may commit or abort: commit=%v abort=%v",
			out.CommittedNodes, out.AbortedNodes)
	}
	if len(res.Blocked) != 1 || res.Blocked[0].QueryCount == 0 {
		t.Fatalf("expected one blocked txn with repeated QUERY evidence, got %+v", res.Blocked)
	}
	assertNoInvariantErrors(t, res) // blocking, but never inconsistent
}

// TestBlockingParticipantDown: one participant never returns after voting
// YES; others commit, it stays PREPARED — blocked termination, still atomic.
func TestBlockingParticipantDown(t *testing.T) {
	s := baseSpec()
	s.Horizon = 300
	s.Crashes = []harness.Crash{milestoneCrash("p1", "p-vote-sent:T1", 0)}
	res := mustRun(t, s)

	out := statusOf(res, "T1")
	if out.Status != "blocked" {
		t.Fatalf("status=%q want blocked", out.Status)
	}
	if !contains(out.CommittedNodes, "p2") || !contains(out.CommittedNodes, "p3") {
		t.Errorf("p2/p3 should have durable-committed: %v", out.CommittedNodes)
	}
	if !contains(out.PreparedNodes, "p1") {
		t.Errorf("p1 should remain prepared, got prepared=%v", out.PreparedNodes)
	}
	if len(out.AbortedNodes) != 0 {
		t.Errorf("no node may abort a committing transaction: %v", out.AbortedNodes)
	}
	assertNoInvariantErrors(t, res)
}

// TestDeterminism: same seed twice => byte-identical traces; across seeds
// the safety invariants still hold.
func TestDeterminism(t *testing.T) {
	s := baseSpec()
	s.Network = engine.NetConfig{BaseDelay: 1, Jitter: 4, Loss: 0.3, Duplicate: 0.2}
	s.Horizon = 500
	s.Crashes = []harness.Crash{milestoneCrash("p2", "p-prepared-wal:T1", 60)}

	r1 := mustRun(t, s)
	r2 := mustRun(t, s)
	b1, _ := json.Marshal(r1.Trace)
	b2, _ := json.Marshal(r2.Trace)
	if string(b1) != string(b2) {
		t.Fatalf("non-deterministic traces for identical seed")
	}

	for seed := int64(1); seed <= 8; seed++ {
		s.Seed = seed
		res := mustRun(t, s)
		assertNoInvariantErrors(t, res)
	}
}

// TestScenarioFiles exercises the committed JSON request samples.
func TestScenarioFiles(t *testing.T) {
	want := map[string]string{
		"01-happy-commit.json":                      "committed",
		"02-network-loss-dup-reorder.json":          "committed",
		"03-vote-no-global-abort.json":              "aborted",
		"04-participant-crash-before-prepared.json": "committed",
		"05-participant-crash-after-prepared.json":  "committed",
		"06-coord-crash-before-decision.json":       "aborted",
		"07-coord-crash-after-decision.json":        "committed",
		"08-blocking-coord-down.json":               "blocked",
		"09-participant-crash-after-commit.json":    "committed",
		"10-blocking-participant-down.json":         "blocked",
	}
	for file, wantStatus := range want {
		file, wantStatus := file, wantStatus
		t.Run(file, func(t *testing.T) {
			dir, err := filepath.Abs("../../scenarios")
			if err != nil {
				t.Fatal(err)
			}
			spec, err := harness.LoadSpec(filepath.Join(dir, file))
			if err != nil {
				t.Fatal(err)
			}
			// Keep WAL evidence out of the repo temp space.
			d := t.TempDir()
			spec.DataDir = d
			res := mustRun(t, spec)
			if got := statusOf(res, "T1").Status; got != wantStatus {
				t.Fatalf("status=%s want %s", got, wantStatus)
			}
			assertNoInvariantErrors(t, res)
			if wantStatus == "blocked" && len(res.Blocked) == 0 {
				t.Fatal("expected blocking evidence in result")
			}
			// Every scenario must leave a WAL on disk for crashed nodes.
			if _, err := os.Stat(filepath.Join(d, "coord.log")); err != nil {
				t.Errorf("coordinator WAL missing: %v", err)
			}
		})
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

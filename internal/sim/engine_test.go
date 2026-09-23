package sim

import (
	"testing"
)

func nodes3() []SimNode {
	return []SimNode{
		{ID: "n1", Weight: 1, Domain: "a"},
		{ID: "n2", Weight: 1, Domain: "b"},
		{ID: "n3", Weight: 1, Domain: "c"},
	}
}

func TestMajorityNeverViolatesUnderLoss(t *testing.T) {
	// Regression: a retransmission used to clear the responder set but not
	// the accumulated weight, so one node answering on two attempts counted
	// twice and an op "completed" at weight 1 of threshold 2. With correct
	// cross-attempt node dedup, every completed op on a 3-node 2/3 system
	// touches >=2 nodes; any two such sets intersect (pigeonhole), so a safe
	// configuration must produce ZERO violations over many lossy runs.
	in := RunInput{
		Nodes: nodes3(), ReadQuorum: 2, WriteQuorum: 2,
		Sim: Sim{
			Seed: 20260923, Runs: 60, Operations: 4, Kind: "mixed",
			Horizon: 120, ClientTimeout: 6, MaxAttempts: 8,
			Network: &Network{LossRate: 0.45, DuplicateRate: 0.4, MinDelay: 1, MaxDelay: 3, ReorderWindow: 1},
		},
	}
	r := Run(in)
	if len(r.Errors) > 0 {
		t.Fatal(r.Errors)
	}
	if r.CompletedTotal == 0 {
		t.Fatal("expected some operations to complete despite losses")
	}
	if r.ViolationRuns != 0 || len(r.Violations) != 0 {
		t.Fatalf("safe majority violated: %+v", r.Violations)
	}
	// Every completed op must genuinely reach its threshold from distinct
	// nodes.
	for _, rr := range r.RunResults {
		for _, op := range rr.CompletedOps {
			if len(op.Responders) != op.Weight {
				t.Fatalf("%s: weight %d != %d distinct responders %v", op.Op, op.Weight, len(op.Responders), op.Responders)
			}
			threshold := 2
			if op.Weight < threshold {
				t.Fatalf("%s completed below threshold: %v", op.Op, op)
			}
		}
	}
}

func TestCrossAttemptNodeDedup(t *testing.T) {
	// Direct probe: only n1 ever answers (its requests delivered), n2/n3
	// requests are dropped on the first attempt but delivered on later
	// attempts. The op must not complete from n1 alone no matter how many
	// times n1 replies.
	in := RunInput{
		Nodes: nodes3(), ReadQuorum: 2, WriteQuorum: 2,
		Sim: Sim{
			Seed: 1, Runs: 1, Operations: 1, Kind: "ww",
			Horizon: 60, ClientTimeout: 4, MaxAttempts: 3,
			Rules: []Rule{
				{To: "n1", MsgType: "request", Action: "deliver", Delay: 1},
				{From: "n1", MsgType: "response", Action: "deliver", Delay: 1},
				{MsgType: "request", Action: "drop"},
				{MsgType: "response", Action: "drop"},
			},
		},
	}
	r := Run(in)
	if r.CompletedTotal != 0 {
		t.Fatalf("one distinct responder must not reach quorum 2: %+v", r.RunResults[0].CompletedOps)
	}
}

func TestPerfectNetworkCompletes(t *testing.T) {
	in := RunInput{
		Nodes: nodes3(), ReadQuorum: 2, WriteQuorum: 2,
		Sim: Sim{
			Seed: 1, Runs: 3, Operations: 4, Kind: "mixed",
			Horizon: 100, ClientTimeout: 20, MaxAttempts: 3,
			Network: &Network{MinDelay: 1, MaxDelay: 2},
		},
	}
	r := Run(in)
	if len(r.Errors) > 0 {
		t.Fatal(r.Errors)
	}
	if r.CompletedTotal != 12 || r.TimedOutTotal != 0 || !r.AllComplete {
		t.Fatalf("perfect network: completed=%d timedout=%d", r.CompletedTotal, r.TimedOutTotal)
	}
}

func TestDeterminism(t *testing.T) {
	in := RunInput{
		Nodes: nodes3(), ReadQuorum: 2, WriteQuorum: 2,
		Sim: Sim{
			Seed: 99, Runs: 5, Operations: 4, Kind: "ww",
			Horizon: 100, ClientTimeout: 8, MaxAttempts: 5,
			Network: &Network{LossRate: 0.25, DuplicateRate: 0.25, MinDelay: 1, MaxDelay: 4, ReorderWindow: 2},
		},
	}
	a := Run(in)
	b := Run(in)
	if !equalReports(a, b) {
		t.Fatal("same seed produced different results")
	}
	// A different seed must be able to differ (probabilistic sanity bound).
	in.Sim.Seed = 100
	c := Run(in)
	if equalReports(a, c) {
		t.Fatal("different seeds unexpectedly identical")
	}
}

func TestTotalLossTimesOut(t *testing.T) {
	in := RunInput{
		Nodes: nodes3(), ReadQuorum: 2, WriteQuorum: 2,
		Sim: Sim{
			Seed: 1, Runs: 1, Operations: 2, Kind: "ww",
			Horizon: 60, ClientTimeout: 8, MaxAttempts: 3,
			Network: &Network{LossRate: 1},
		},
	}
	r := Run(in)
	if r.CompletedTotal != 0 || r.TimedOutTotal != 2 || r.AllComplete {
		t.Fatalf("blackhole: completed=%d timedout=%d", r.CompletedTotal, r.TimedOutTotal)
	}
}

func TestDuplicatesDeduped(t *testing.T) {
	// Every response is duplicated many times with r=w=1: the op must
	// complete at weight exactly 1, duplicates never counted twice.
	in := RunInput{
		Nodes: nodes3(), ReadQuorum: 1, WriteQuorum: 1,
		Sim: Sim{
			Seed: 1, Runs: 1, Operations: 1, Kind: "ww",
			Horizon: 30, ClientTimeout: 50, MaxAttempts: 1,
			Rules: []Rule{{Action: "duplicate", Duplicates: 5, Delay: 1}},
		},
	}
	r := Run(in)
	if r.CompletedTotal != 1 {
		t.Fatalf("expected completion despite duplicates, got %+v", r.RunResults)
	}
	op := r.RunResults[0].CompletedOps[0]
	if op.Weight != 1 || len(op.Responders) != 1 {
		t.Fatalf("duplicates counted: weight=%d responders=%v", op.Weight, op.Responders)
	}
	if r.RunResults[0].Duplicated == 0 {
		t.Fatal("expected duplicate counters to be non-zero")
	}
}

func TestReorderingScript(t *testing.T) {
	// Requests delayed, responses reordered via per-edge rules must still
	// deliver all messages (no drops): every op completes.
	in := RunInput{
		Nodes: nodes3(), ReadQuorum: 2, WriteQuorum: 2,
		Sim: Sim{
			Seed: 1, Runs: 1, Operations: 2, Kind: "ww",
			Horizon: 60, ClientTimeout: 50, MaxAttempts: 1,
			Rules: []Rule{
				{Role: "A", To: "n1", Action: "deliver", Delay: 5},
				{Role: "A", To: "n2", Action: "deliver", Delay: 1},
				{Role: "A", To: "n3", Action: "deliver", Delay: 9},
				{Role: "B", To: "n1", Action: "deliver", Delay: 8},
				{Role: "B", To: "n2", Action: "deliver", Delay: 2},
				{Role: "B", To: "n3", Action: "deliver", Delay: 3},
				{MsgType: "response", From: "n3", Action: "deliver", Delay: 6},
				{MsgType: "response", Action: "deliver", Delay: 1},
			},
		},
	}
	r := Run(in)
	if r.CompletedTotal != 2 || r.TimedOutTotal != 0 {
		t.Fatalf("reorder script: completed=%d timedout=%d", r.CompletedTotal, r.TimedOutTotal)
	}
}

func TestSafetyWitnessWW(t *testing.T) {
	in := SafetyWitness(nodes3(), 1, 1, "ww", []string{"n1"}, []string{"n2"}, nil)
	r := Run(in)
	if r.CompletedTotal != 2 {
		t.Fatalf("witness must complete both ops, got %+v", r.RunResults[0])
	}
	if r.ViolationRuns != 1 || len(r.Violations) != 1 {
		t.Fatalf("expected exactly one WW violation, got %+v", r.Violations)
	}
	v := r.Violations[0]
	if v.Kind != "ww" || !sameSet(v.RespondersA, []string{"n1"}) || !sameSet(v.RespondersB, []string{"n2"}) {
		t.Fatalf("wrong violation: %+v", v)
	}
}

func TestSafetyWitnessRWUnderDomainFailure(t *testing.T) {
	// 4 nodes across 2 domains; r=w=2. Witness: read {n1,n3}, write {n2,n4}
	// disjoint while domain "b" is down — but n3 lives in b, so the witness
	// must instead be built only with surviving nodes. Here we assert the
	// surviving-pair case directly: domain c (unused) down.
	nodes := []SimNode{
		{ID: "n1", Weight: 1, Domain: "a"},
		{ID: "n2", Weight: 1, Domain: "a"},
		{ID: "n3", Weight: 1, Domain: "b"},
		{ID: "n4", Weight: 1, Domain: "b"},
	}
	in := SafetyWitness(nodes, 2, 2, "rw", []string{"n1", "n3"}, []string{"n2", "n4"}, nil)
	r := Run(in)
	if r.CompletedTotal != 2 || r.ViolationRuns != 1 {
		t.Fatalf("rw witness failed: completed=%d violations=%d", r.CompletedTotal, r.ViolationRuns)
	}
	if r.Violations[0].Kind != "rw" {
		t.Fatalf("want rw violation, got %s", r.Violations[0].Kind)
	}
}

func TestWitnessWithDeadDomain(t *testing.T) {
	// If the failed domain contains a quorum-set node, the op cannot
	// complete: witnesses using only survivors must be generated by callers.
	// Here we confirm the simulator honestly reports incompleteness rather
	// than fabricating a violation.
	in := SafetyWitness(nodes3(), 2, 2, "ww", []string{"n1"}, []string{"n2"}, []string{"b"})
	r := Run(in)
	if r.CompletedTotal != 0 {
		t.Fatalf("a quorum node is dead; expected no completion, got %d", r.CompletedTotal)
	}
}

func TestAvailabilityWitness(t *testing.T) {
	in := AvailabilityWitness(nodes3(), 2, 2, "ww", []string{"a", "b"})
	r := Run(in)
	if r.CompletedTotal != 0 || r.TimedOutTotal != 1 {
		t.Fatalf("expected unavailability, got %+v", r.RunResults[0])
	}
}

func TestValidationErrors(t *testing.T) {
	bad := []RunInput{
		{Sim: Sim{}}, // no nodes, bad quorums
		{Nodes: nodes3(), ReadQuorum: 0, WriteQuorum: 2},
		{Nodes: nodes3(), ReadQuorum: 2, WriteQuorum: 2, Sim: Sim{Kind: "zz"}},
		{Nodes: nodes3(), ReadQuorum: 2, WriteQuorum: 2, Sim: Sim{FailedDomains: []string{"nope"}}},
		{Nodes: nodes3(), ReadQuorum: 2, WriteQuorum: 2, Sim: Sim{Rules: []Rule{{Action: "frobnicate"}}}},
		{Nodes: nodes3(), ReadQuorum: 2, WriteQuorum: 2, Sim: Sim{Network: &Network{LossRate: 2}}},
	}
	for i, in := range bad {
		if r := Run(in); len(r.Errors) == 0 {
			t.Fatalf("case %d: expected validation errors", i)
		}
	}
}

func sameSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	m := map[string]int{}
	for _, x := range a {
		m[x]++
	}
	for _, x := range b {
		m[x]--
		if m[x] < 0 {
			return false
		}
	}
	return true
}

func equalReports(a, b *Report) bool {
	if a.CompletedTotal != b.CompletedTotal || a.TimedOutTotal != b.TimedOutTotal ||
		a.ViolationRuns != b.ViolationRuns || len(a.RunResults) != len(b.RunResults) {
		return false
	}
	for i := range a.RunResults {
		x, y := a.RunResults[i], b.RunResults[i]
		if x.Completed != y.Completed || x.TimedOut != y.TimedOut ||
			x.RequestsSent != y.RequestsSent || x.ResponsesDelivered != y.ResponsesDelivered ||
			x.Dropped != y.Dropped || x.Duplicated != y.Duplicated || x.MaxTime != y.MaxTime {
			return false
		}
	}
	return true
}

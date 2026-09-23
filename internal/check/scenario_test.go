package check

import (
	"encoding/json"
	"testing"

	"raftlab/internal/raft"
	"raftlab/internal/sim"
)

// TestFigure8CorrectVariant: correct Raft survives the Figure 8 scenario with
// no safety violation (the old-term entry is never committed by the majority
// rule; it is committed implicitly once a current-term entry is).
func TestFigure8CorrectVariant(t *testing.T) {
	actions, err := Figure8Actions(false)
	if err != nil {
		t.Fatalf("build figure-8 trace: %v", err)
	}
	r := NewRunner(Figure8Config(false))
	cl, res := r.Run(actions)
	if !res.OK() {
		t.Fatalf("correct raft violated %s: %s", res.Violation.Kind, res.Violation.Message)
	}
	// The current-term entry z must be committed at index 2 and y at index 1.
	s := cl.Node(5)
	if s.CommitIndex < 2 {
		t.Fatalf("expected commitIndex>=2 on n5, got %d", s.CommitIndex)
	}
	if e, _ := entryAt(s, 1); e.Command != "SET k y" {
		t.Fatalf("index 1 committed %q, want SET k y", e.Command)
	}
	for _, n := range cl.Nodes() {
		kv := cl.KV(n.ID)
		if v, ok := kv["k"]; !ok || v != "z" {
			t.Fatalf("node %d KV k=%q,%v; want z", n.ID, v, ok)
		}
	}
}

// TestFigure8BuggyVariant: the old-term majority-commit bug must produce a
// committed-prefix conflict in exactly this scenario.
func TestFigure8BuggyVariant(t *testing.T) {
	actions, err := Figure8Actions(true)
	if err != nil {
		t.Fatalf("build figure-8 trace: %v", err)
	}
	r := NewRunner(Figure8Config(true))
	_, res := r.Run(actions)
	if res.OK() {
		t.Fatal("buggy variant unexpectedly satisfied all invariants")
	}
	if res.Violation.Kind != ViolationCommittedPrefix {
		t.Fatalf("violation kind = %s, want %s (msg: %s)",
			res.Violation.Kind, ViolationCommittedPrefix, res.Violation.Message)
	}
	if res.Violation.Index != 1 {
		t.Fatalf("conflict at index %d, want 1", res.Violation.Index)
	}
	t.Logf("buggy violation: %s", res.Violation.Message)
}

// TestFigure8ReplayDeterminism: the recorded trace JSON replayed twice yields
// identical final states, and the violation reproduces for the buggy variant.
func TestFigure8ReplayDeterminism(t *testing.T) {
	for _, buggy := range []bool{false, true} {
		actions, err := Figure8Actions(buggy)
		if err != nil {
			t.Fatal(err)
		}
		b, err := json.Marshal(actions)
		if err != nil {
			t.Fatal(err)
		}
		var replay []Action
		if err := json.Unmarshal(b, &replay); err != nil {
			t.Fatal(err)
		}
		r := NewRunner(Figure8Config(buggy))
		_, res1 := r.Run(replay)
		cl2, res2 := r.Run(replay)
		if res1.OK() != res2.OK() {
			t.Fatalf("buggy=%v replay nondeterministic: %v vs %v", buggy, res1.OK(), res2.OK())
		}
		if !res1.OK() && (res1.Violation.Kind != res2.Violation.Kind ||
			res1.Violation.Index != res2.Violation.Index) {
			t.Fatalf("buggy=%v violation mismatch on replay", buggy)
		}
		// Every node snapshot must be stable across replays.
		var first []raft.Snapshot
		cl1, _ := r.Run(replay)
		first = cl1.Nodes()
		for i, s := range cl2.Nodes() {
			if snapshotsEqual(first[i], s) == false {
				t.Fatalf("buggy=%v node %d snapshot differs on replay", buggy, s.ID)
			}
		}
	}
}

func snapshotsEqual(a, b raft.Snapshot) bool {
	if a.Role != b.Role || a.Term != b.Term || a.CommitIndex != b.CommitIndex ||
		a.Alive != b.Alive || a.VotedFor != b.VotedFor || len(a.Log) != len(b.Log) {
		return false
	}
	for i := range a.Log {
		if a.Log[i] != b.Log[i] {
			return false
		}
	}
	return true
}

// TestEnumerationCorrectVariant: short random fault traces on 3-node correct
// Raft never violate the safety properties.
func TestEnumerationCorrectVariant(t *testing.T) {
	rep := Enumerate(EnumerationConfig{
		Cluster:      Default3NodeConfig(),
		MaxDepth:     3,
		MaxTraces:    4000,
		TicksPerStep: 4,
	})
	t.Logf("correct enumeration: traces=%d states=%d exhausted=%v violations=%d",
		rep.TracesRun, rep.StatesSeen, rep.Exhausted, len(rep.Violations))
	if len(rep.Violations) != 0 {
		for _, ce := range rep.Violations {
			t.Errorf("correct variant violation: %s", ce.Violation.Message)
		}
	}
}

// TestEnumerationBuggyVariantFindsCounterexample: on the buggy variant the
// enumerator finds at least one committed-prefix conflict (deterministically,
// via the Figure 8 seed prefix) and can shrink it.
func TestEnumerationBuggyVariantFindsCounterexample(t *testing.T) {
	seeds, err := Figure8Actions(true)
	if err != nil {
		t.Fatal(err)
	}
	cfg := Figure8Config(true)
	rep := Enumerate(EnumerationConfig{
		Cluster:      cfg,
		MaxDepth:     2,
		MaxTraces:    6000,
		TicksPerStep: 4,
		Seeds:        [][]Action{seeds},
	})
	t.Logf("buggy enumeration: traces=%d states=%d violations=%d",
		rep.TracesRun, rep.StatesSeen, len(rep.Violations))
	found := false
	for _, ce := range rep.Violations {
		if ce.Violation.Kind == ViolationCommittedPrefix {
			found = true
			if len(ce.Shrunk) >= len(ce.Trace) {
				t.Errorf("shrink did not reduce trace: %d -> %d", len(ce.Trace), len(ce.Shrunk))
			}
			t.Logf("shrunk counterexample length %d (was %d)", len(ce.Shrunk), len(ce.Trace))
			break
		}
	}
	if !found {
		t.Fatal("buggy enumeration with the figure-8 seed found no counterexample")
	}
}

// TestStaleMessagesHarmlessOnCorrectRaft drives a partition/restart cycle and
// then releases every delayed message: no stale packet may install an old
// leader or roll back committed state.
func TestStaleMessagesHarmlessOnCorrectRaft(t *testing.T) {
	actions := []Action{
		{Op: ActRun, Node: 20}, // elect leader, settle
		{Op: ActProposeLeader, Command: "SET k1 v1"},
		{Op: ActRun, Node: 8},
		{Op: ActPartition, Node: 1},
		{Op: ActRun, Node: 20}, // elect a new leader in the majority
		{Op: ActHeal},
		{Op: ActRestart, Node: 2},
		{Op: ActRun, Node: 10},
		{Op: ActReleaseStale},
		{Op: ActRun, Node: 20},
	}
	r := NewRunner(Default3NodeConfig())
	cl, res := r.Run(actions)
	if !res.OK() {
		t.Fatalf("violation: %s", res.Violation.Message)
	}
	for _, n := range cl.Nodes() {
		kv := cl.KV(n.ID)
		if v, ok := kv["k1"]; !ok || v != "v1" {
			t.Fatalf("node %d missing k1=v1 after stale release: %q", n.ID, v)
		}
	}
}

var _ = sim.MemoryStorageFactory

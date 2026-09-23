package sim

import (
	"encoding/json"
	"strings"
	"testing"
)

func intPtr(i int) *int { return &i }

// kinds extracts trace kinds in order.
func kinds(rep *Report) []string {
	out := []string{}
	for _, e := range rep.Trace {
		out = append(out, e.Kind)
	}
	return out
}

// findTraces returns trace entries of the given kind.
func findTraces(rep *Report, kind string) []TraceEntry {
	var out []TraceEntry
	for _, e := range rep.Trace {
		if e.Kind == kind {
			out = append(out, e)
		}
	}
	return out
}

// Acceptance 1: offline divergent writes converge to two concurrent siblings.
func TestAcceptanceOfflineWrites(t *testing.T) {
	two := intPtr(2)
	sc := Scenario{
		Name:  "offline-writes",
		Seed:  42,
		Nodes: []string{"A", "B"},
		Events: []EventSpec{
			{Type: "offline", Time: 0, Node: "B"},
			{Type: "write", Time: 1, Node: "A", Key: "cart", Value: json.RawMessage(`{"line":"a"}`)},
			{Type: "write", Time: 2, Node: "B", Key: "cart", Value: json.RawMessage(`{"line":"b"}`)},
			{Type: "online", Time: 3, Node: "B"},
			{Type: "send", Time: 4, From: "A", To: "B", Key: "cart"},
			{Type: "inspect", Time: 6, Node: "B", Key: "cart", ExpectSiblings: two,
				ExpectIDs: []string{"A:1", "B:1"}},
		},
	}
	rep, err := Run(sc)
	if err != nil {
		t.Fatal(err)
	}
	if !rep.AssertionsOK {
		t.Fatalf("assertions failed: %v", rep.AssertionErrs)
	}
	recv := findTraces(rep, "receive")
	if len(recv) != 1 || !recv[0].Accepted {
		t.Fatalf("expected one accepted receive, got %+v", recv)
	}
}

// Acceptance 2: duplicate transmission is idempotent; drop loses nothing on
// retransmission; reorder does not change the final convergent state.
func TestAcceptanceDuplicateDropReorder(t *testing.T) {
	one := intPtr(1)
	sc := Scenario{
		Name:    "dup-drop-reorder",
		Seed:    7,
		Nodes:   []string{"A", "B"},
		Network: NetworkPolicy{BaseDelay: 1},
		Events: []EventSpec{
			// Duplicated delivery: B must ingest once.
			{Type: "write", Time: 0, Node: "A", Key: "k", Value: json.RawMessage(`"v1"`),
				ExpectSiblings: nil},
			{Type: "send", Time: 1, From: "A", To: "B", Key: "k", ForceDup: true},
			// Dropped message: nothing arrives.
			{Type: "send", Time: 3, From: "A", To: "B", Key: "k", ForceDrop: true},
			// Reordered resend: a delayed duplicate of an already-known version
			// must not alter state.
			{Type: "send", Time: 5, From: "A", To: "B", Key: "k", ForceReorder: true,
				ForceDup: false},
			{Type: "inspect", Time: 10, Node: "B", Key: "k", ExpectSiblings: one,
				ExpectIDs: []string{"A:1"}},
		},
	}
	rep, err := Run(sc)
	if err != nil {
		t.Fatal(err)
	}
	if !rep.AssertionsOK {
		t.Fatalf("assertions failed: %v", rep.AssertionErrs)
	}
	recv := findTraces(rep, "receive")
	var accepted, dedup int
	for _, r := range recv {
		if r.Accepted {
			accepted++
		} else {
			dedup++
		}
	}
	if accepted != 1 {
		t.Fatalf("exactly one receive must be ingested, got accepted=%d total=%d (%+v)", accepted, len(recv), recv)
	}
	if dedup < 2 {
		t.Fatalf("duplicated/reordered copies must be deduplicated, got %d non-accepted receives", dedup)
	}
	// The dropped send must show Dropped=true and schedule no receive.
	sends := findTraces(rep, "send")
	var drops int
	for _, s := range sends {
		if s.Dropped {
			drops++
		}
	}
	if drops != 1 {
		t.Fatalf("expected exactly 1 dropped send, got %d", drops)
	}
}

// Acceptance 3: writes with old/incomplete context cannot overwrite concurrent
// versions; a merge carrying the full context succeeds.
func TestAcceptanceStaleContextRejected(t *testing.T) {
	two := intPtr(2)
	one := intPtr(1)
	sc := Scenario{
		Name:    "stale-context",
		Seed:    99,
		Nodes:   []string{"A", "B"},
		Network: NetworkPolicy{BaseDelay: 1},
		Events: []EventSpec{
			{Type: "write", Time: 0, Node: "A", Key: "k", Value: json.RawMessage(`"a"`)},
			{Type: "send", Time: 1, From: "A", To: "B", Key: "k"},
			// B advances locally: A:1 becomes a stale ancestor at B.
			{Type: "write", Time: 3, Node: "B", Key: "k", Value: json.RawMessage(`"b-after-a"`)},
			// Meanwhile A diverged offline and now heals.
			{Type: "write", Time: 4, Node: "A", Key: "k", Value: json.RawMessage(`"a2"`)},
			{Type: "send", Time: 5, From: "A", To: "B", Key: "k"},
			{Type: "inspect", Time: 7, Node: "B", Key: "k", ExpectSiblings: two,
				ExpectIDs: []string{"A:2", "B:1"}},

			// Client only saw B:1 (missing concurrent sibling A:2) -> rejected.
			{Type: "client_merge", Time: 8, Node: "B", Key: "k",
				Value:   json.RawMessage(`"clobber"`),
				Context: []ContextRef{{ID: "B:1", VC: map[string]int{"A": 1, "B": 1}}}},
			// Client carrying strictly stale A:1 while A:2 is live -> rejected.
			{Type: "client_merge", Time: 9, Node: "B", Key: "k",
				Value:   json.RawMessage(`"from-old-read"`),
				Context: []ContextRef{{ID: "A:1", VC: map[string]int{"A": 1}}}},
			{Type: "inspect", Time: 10, Node: "B", Key: "k", ExpectSiblings: two,
				ExpectIDs: []string{"A:2", "B:1"}},

			// Full-context merge covering both siblings -> accepted, collapses.
			{Type: "client_merge", Time: 11, Node: "B", Key: "k",
				Value: json.RawMessage(`"merged"`),
				Context: []ContextRef{
					{ID: "B:1", VC: map[string]int{"A": 1, "B": 1}},
					{ID: "A:2", VC: map[string]int{"A": 2}},
				}},
			{Type: "inspect", Time: 12, Node: "B", Key: "k", ExpectSiblings: one},
		},
	}
	rep, err := Run(sc)
	if err != nil {
		t.Fatal(err)
	}
	if !rep.AssertionsOK {
		t.Fatalf("assertions failed: %v", rep.AssertionErrs)
	}
	merges := findTraces(rep, "client_merge")
	if len(merges) != 3 {
		t.Fatalf("expected 3 client_merge traces, got %d", len(merges))
	}
	if merges[0].Accepted || !strings.Contains(merges[0].Error, "concurrent siblings") {
		t.Fatalf("first merge must be rejected as concurrent-not-covered: %+v", merges[0])
	}
	if merges[1].Accepted || !strings.Contains(merges[1].Error, "stale context") {
		t.Fatalf("second merge must be rejected as stale: %+v", merges[1])
	}
	if !merges[2].Accepted {
		t.Fatalf("full-context merge must be accepted: %+v", merges[2])
	}
}

// Determinism: same seed + scenario must yield byte-identical reports.
func TestDeterministic(t *testing.T) {
	sc := Scenario{
		Name:  "det",
		Seed:  123,
		Nodes: []string{"A", "B", "C"},
		Network: NetworkPolicy{DropProb: 0.3, DupProb: 0.3, ReorderProb: 0.3,
			BaseDelay: 1, Jitter: 2, ReorderDelay: 4},
		Events: []EventSpec{
			{Type: "write", Time: 0, Node: "A", Key: "k", Value: json.RawMessage(`"1"`)},
			{Type: "write", Time: 0, Node: "B", Key: "k", Value: json.RawMessage(`"2"`)},
			{Type: "send", Time: 1, From: "A", To: "B", Key: "k"},
			{Type: "send", Time: 1, From: "B", To: "A", Key: "k"},
			{Type: "send", Time: 2, From: "A", To: "C", Key: "k"},
			{Type: "send", Time: 2, From: "B", To: "C", Key: "k"},
		},
	}
	r1, err := Run(sc)
	if err != nil {
		t.Fatal(err)
	}
	r2, err := Run(sc)
	if err != nil {
		t.Fatal(err)
	}
	b1, _ := json.Marshal(r1)
	b2, _ := json.Marshal(r2)
	if string(b1) != string(b2) {
		t.Fatal("same seed produced different reports")
	}
	// Different seed is allowed to differ (not asserted, but ensure the
	// probabilistic path actually exercises drops/dups with no crash).
	sc.Seed = 987654321
	if _, err := Run(sc); err != nil {
		t.Fatal(err)
	}
}

// Offline destinations drop deliveries; a resend after going online converges.
func TestOfflineThenResend(t *testing.T) {
	one := intPtr(1)
	sc := Scenario{
		Name:    "offline-resend",
		Seed:    1,
		Nodes:   []string{"A", "B"},
		Network: NetworkPolicy{BaseDelay: 1},
		Events: []EventSpec{
			{Type: "write", Time: 0, Node: "A", Key: "k", Value: json.RawMessage(`"v"`)},
			{Type: "offline", Time: 0, Node: "B"},
			{Type: "send", Time: 1, From: "A", To: "B", Key: "k"},
			{Type: "online", Time: 5, Node: "B"},
			{Type: "send", Time: 6, From: "A", To: "B", Key: "k"},
			{Type: "inspect", Time: 8, Node: "B", Key: "k", ExpectSiblings: one,
				ExpectIDs: []string{"A:1"}},
		},
	}
	rep, err := Run(sc)
	if err != nil {
		t.Fatal(err)
	}
	if !rep.AssertionsOK {
		t.Fatalf("assertions failed: %v", rep.AssertionErrs)
	}
	recv := findTraces(rep, "receive")
	if len(recv) != 2 || !recv[0].Offline || recv[0].Dropped != true || !recv[1].Accepted {
		t.Fatalf("unexpected receive pattern: %+v", recv)
	}
}

// Partitioned edges block delivery; healing the partition allows convergence.
func TestPartitionHeal(t *testing.T) {
	two := intPtr(2)
	sc := Scenario{
		Name:    "partition-heal",
		Seed:    2,
		Nodes:   []string{"A", "B"},
		Network: NetworkPolicy{BaseDelay: 1},
		Events: []EventSpec{
			{Type: "partition", Time: 0, From: "A", To: "B"}, // disconnected
			{Type: "write", Time: 1, Node: "A", Key: "k", Value: json.RawMessage(`"a"`)},
			{Type: "write", Time: 1, Node: "B", Key: "k", Value: json.RawMessage(`"b"`)},
			{Type: "send", Time: 2, From: "A", To: "B", Key: "k"},
			{Type: "partition", Time: 4, From: "A", To: "B", Connect: true},
			{Type: "send", Time: 5, From: "A", To: "B", Key: "k"},
			{Type: "inspect", Time: 7, Node: "B", Key: "k", ExpectSiblings: two},
		},
	}
	rep, err := Run(sc)
	if err != nil {
		t.Fatal(err)
	}
	if !rep.AssertionsOK {
		t.Fatalf("assertions failed: %v", rep.AssertionErrs)
	}
}

func TestValidationErrors(t *testing.T) {
	if _, err := Run(Scenario{Name: "x", Nodes: nil}); err == nil {
		t.Fatal("empty nodes should be rejected")
	}
	if _, err := Run(Scenario{Name: "x", Nodes: []string{"A"}, Events: []EventSpec{
		{Type: "bogus", Time: 0, Node: "A"},
	}}); err == nil {
		t.Fatal("unknown event type should be rejected")
	}
	if _, err := Run(Scenario{Name: "x", Nodes: []string{"A"}, Events: []EventSpec{
		{Type: "write", Time: 0, Node: "X"},
	}}); err == nil {
		t.Fatal("unknown node should be rejected")
	}
}

// Across many seeds, three gossip rounds under a lossy/duplicating/reordering
// network must converge all three nodes to the full 3-sibling set. Every
// delivered offline write survives: convergence never silently drops one.
func TestProbabilisticConvergenceAcrossSeeds(t *testing.T) {
	three := intPtr(3)
	pairs := [][2]string{{"A", "B"}, {"A", "C"}, {"B", "A"}, {"B", "C"}, {"C", "A"}, {"C", "B"}}
	build := func(seed int64) Scenario {
		sc := Scenario{
			Name:  "prob",
			Seed:  seed,
			Nodes: []string{"A", "B", "C"},
			Network: NetworkPolicy{DropProb: 0.2, DupProb: 0.2, ReorderProb: 0.2,
				BaseDelay: 1, Jitter: 1, ReorderDelay: 3},
		}
		for _, n := range []string{"A", "B", "C"} {
			sc.Events = append(sc.Events, EventSpec{Type: "write", Time: 0, Node: n,
				Key: "k", Value: json.RawMessage(`"` + n + `"`)})
		}
		for _, at := range []float64{2, 8, 14} {
			for _, p := range pairs {
				sc.Events = append(sc.Events, EventSpec{Type: "send", Time: at,
					From: p[0], To: p[1], Key: "k"})
			}
		}
		for _, n := range []string{"A", "B", "C"} {
			sc.Events = append(sc.Events, EventSpec{Type: "inspect", Time: 20, Node: n,
				Key: "k", ExpectSiblings: three, ExpectIDs: []string{"A:1", "B:1", "C:1"}})
		}
		return sc
	}
	for seed := int64(1); seed <= 40; seed++ {
		rep, err := Run(build(seed))
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		if !rep.AssertionsOK {
			t.Fatalf("seed %d failed to converge: %v", seed, rep.AssertionErrs)
		}
	}
}

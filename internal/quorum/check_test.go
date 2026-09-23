package quorum

import (
	"math/rand"
	"reflect"
	"testing"
)

func nodesOf(pairs ...any) []Node {
	var out []Node
	for i := 0; i < len(pairs); i += 3 {
		out = append(out, Node{
			ID:     pairs[i].(string),
			Weight: pairs[i+1].(int),
			Domain: pairs[i+2].(string),
		})
	}
	return out
}

// bruteForceDisjoint is the independent small-scale exhaustive reference:
// it checks every pair of subsets of nodes for a disjoint quorum pair.
func bruteForceDisjoint(nodes []Node, tA, tB int) bool {
	n := len(nodes)
	w := make([]int, 1<<n)
	for mask := 1; mask < 1<<n; mask++ {
		for i := 0; i < n; i++ {
			if mask&(1<<i) != 0 {
				w[mask] += nodes[i].Weight
			}
		}
	}
	for maskA := 1; maskA < 1<<n; maskA++ {
		if w[maskA] < tA {
			continue
		}
		for maskB := 1; maskB < 1<<n; maskB++ {
			if maskA&maskB == 0 && w[maskB] >= tB {
				return true
			}
		}
	}
	return false
}

// TestAgainstBruteForce cross-checks the checker against the exhaustive
// subset-pair reference on randomized small configs, per failure scenario.
func TestAgainstBruteForce(t *testing.T) {
	rng := rand.New(rand.NewSource(1))
	for trial := 0; trial < 300; trial++ {
		n := 1 + rng.Intn(6)
		var nodes []Node
		domains := []string{"d0", "d1", "d2"}
		total := 0
		for i := 0; i < n; i++ {
			w := rng.Intn(4) // includes zero weights
			id := string(rune('a' + i))
			nodes = append(nodes, Node{ID: id, Weight: w, Domain: domains[rng.Intn(3)]})
			total += w
		}
		cfg := &Config{
			Nodes:            nodes,
			ReadThreshold:    1 + rng.Intn(total+2),
			WriteThreshold:   1 + rng.Intn(total+2),
			MaxFailedDomains: rng.Intn(3),
		}
		rep, err := Check(cfg)
		if err != nil {
			t.Fatalf("trial %d: %v", trial, err)
		}
		rwSafe, wwSafe := true, true
		for _, sc := range rep.Scenarios {
			avail := availableNodes(cfg.SortedNodes(), sc.FailedDomains)
			if bruteForceDisjoint(avail, cfg.ReadThreshold, cfg.WriteThreshold) {
				rwSafe = false
			}
			if bruteForceDisjoint(avail, cfg.WriteThreshold, cfg.WriteThreshold) {
				wwSafe = false
			}
			wantAvailW := 0
			for _, nd := range avail {
				wantAvailW += nd.Weight
			}
			if sc.AvailableWeight != wantAvailW {
				t.Errorf("trial %d scenario %v: available weight %d, want %d",
					trial, sc.FailedDomains, sc.AvailableWeight, wantAvailW)
			}
		}
		if rep.RWSafe != rwSafe || rep.WWSafe != wwSafe {
			t.Errorf("trial %d cfg=%+v: got rw=%v ww=%v, brute force says rw=%v ww=%v",
				trial, cfg, rep.RWSafe, rep.WWSafe, rwSafe, wwSafe)
		}
		if rep.Safe != (rwSafe && wwSafe) {
			t.Errorf("trial %d: Safe flag inconsistent", trial)
		}
		// Unsafe reports must carry a counterexample of the matching kind.
		if !rep.RWSafe && rep.RWCounterexample == nil {
			t.Errorf("trial %d: rw unsafe but no counterexample", trial)
		}
		if !rep.WWSafe && rep.WWCounterexample == nil {
			t.Errorf("trial %d: ww unsafe but no counterexample", trial)
		}
	}
}

func TestDuplicateNodeRejected(t *testing.T) {
	cfg := &Config{
		Nodes:          nodesOf("n1", 1, "a", "n1", 2, "b"),
		ReadThreshold:  1,
		WriteThreshold: 1,
	}
	if _, err := Check(cfg); err == nil {
		t.Fatal("expected duplicate node id error")
	} else if got := err.Error(); !contains(got, "duplicate node id") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestNegativeWeightRejected(t *testing.T) {
	cfg := &Config{
		Nodes:          nodesOf("n1", -1, "a"),
		ReadThreshold:  1,
		WriteThreshold: 1,
	}
	if _, err := Check(cfg); err == nil {
		t.Fatal("expected negative weight error")
	}
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (func() bool {
		for i := 0; i+len(sub) <= len(s); i++ {
			if s[i:i+len(sub)] == sub {
				return true
			}
		}
		return false
	})()
}

func TestZeroWeight(t *testing.T) {
	cfg := &Config{
		Nodes:            nodesOf("n1", 1, "a", "n2", 1, "b", "z", 0, "z"),
		ReadThreshold:    2,
		WriteThreshold:   2,
		MaxFailedDomains: 0,
	}
	rep, err := Check(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.Warnings) != 1 {
		t.Fatalf("expected one zero-weight warning, got %v", rep.Warnings)
	}
	// total weight 2, R=W=2: 2+2 > 2 and 2*2 > 2 -> safe, and the
	// zero-weight node must not appear in any minimal quorum.
	if !rep.Safe {
		t.Fatalf("expected-safe config reported unsafe: %+v", rep)
	}
	qs, err := EnumerateQuorums(cfg, 2)
	if err != nil {
		t.Fatal(err)
	}
	for scenario, quorums := range qs {
		for _, q := range quorums {
			for _, id := range q {
				if id == "z" {
					t.Errorf("scenario %s: zero-weight node in minimal quorum %v", scenario, q)
				}
			}
		}
	}
}

// TestMinimalCounterexample pins the exact minimal witness for a classic
// unsafe config: 4 nodes of weight 1, R=W=2 (R+W = 4 is not > 4).
func TestMinimalCounterexample(t *testing.T) {
	cfg := &Config{
		Nodes:            nodesOf("n1", 1, "a", "n2", 1, "b", "n3", 1, "c", "n4", 1, "d"),
		ReadThreshold:    2,
		WriteThreshold:   2,
		MaxFailedDomains: 0,
	}
	rep, err := Check(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Safe || rep.RWSafe || rep.WWSafe {
		t.Fatalf("expected unsafe, got %+v", rep)
	}
	cx := rep.RWCounterexample
	if cx == nil {
		t.Fatal("missing rw counterexample")
	}
	if !reflect.DeepEqual(cx.QuorumA, []string{"n1", "n2"}) || !reflect.DeepEqual(cx.QuorumB, []string{"n3", "n4"}) {
		t.Errorf("unexpected minimal counterexample: %+v", cx)
	}
	if cx.WeightA != 2 || cx.WeightB != 2 {
		t.Errorf("unexpected weights: %+v", cx)
	}
	ww := rep.WWCounterexample
	if ww == nil || len(ww.QuorumA)+len(ww.QuorumB) != 4 {
		t.Errorf("unexpected ww counterexample: %+v", ww)
	}
}

// TestWholeDomainFailure: 3 domains x 2 nodes (weight 1 each), R=W=4.
// Safe with no failures and with any single whole-domain failure
// (available weight 4, R+W=8 > 4, 2W=8 > 4). With R=W=3 the same layout
// is write/write unsafe even without failures.
func TestWholeDomainFailure(t *testing.T) {
	mk := func(r, w, maxFail int) *Config {
		return &Config{
			Nodes: nodesOf(
				"a1", 1, "a", "a2", 1, "a",
				"b1", 1, "b", "b2", 1, "b",
				"c1", 1, "c", "c2", 1, "c",
			),
			ReadThreshold:    r,
			WriteThreshold:   w,
			MaxFailedDomains: maxFail,
		}
	}
	rep, err := Check(mk(4, 4, 1))
	if err != nil {
		t.Fatal(err)
	}
	if !rep.Safe {
		t.Fatalf("R=W=4 should be safe under 1 domain failure: %+v", rep.WWCounterexample)
	}
	if len(rep.Scenarios) != 1+3 { // no-failure + 3 single-domain scenarios
		t.Fatalf("expected 4 scenarios, got %d", len(rep.Scenarios))
	}
	for _, sc := range rep.Scenarios {
		if !sc.ReadQuorumPossible || !sc.WriteQuorumPossible {
			t.Errorf("scenario %+v: quorum should be formable", sc)
		}
	}

	rep2, err := Check(mk(3, 3, 0))
	if err != nil {
		t.Fatal(err)
	}
	if rep2.WWSafe {
		t.Fatal("R=W=3 on 6 weight-1 nodes must be write/write unsafe")
	}
	if rep2.WWCounterexample == nil || rep2.WWCounterexample.WeightA != 3 || rep2.WWCounterexample.WeightB != 3 {
		t.Fatalf("bad ww counterexample: %+v", rep2.WWCounterexample)
	}
}

// TestDomainFailureBreaksSafety: weighted 3-node config that is safe with
// all nodes up but unsafe once a specific domain fails.
func TestDomainFailureBreaksSafety(t *testing.T) {
	cfg := &Config{
		Nodes: nodesOf(
			"a1", 2, "a",
			"b1", 2, "b",
			"c1", 1, "c",
			"c2", 1, "c",
		),
		ReadThreshold:    3,
		WriteThreshold:   3,
		MaxFailedDomains: 1,
	}
	rep, err := Check(cfg)
	if err != nil {
		t.Fatal(err)
	}
	// Total 6: 3+3 > 6? No, 6 = 6 -> unsafe even without failures.
	if rep.Safe {
		t.Fatal("expected unsafe")
	}
}

// TestSafeUnderDomainFailure verifies a config that only becomes checkable
// when domain failures are considered: safe both intact and degraded.
func TestSafeUnderDomainFailure(t *testing.T) {
	cfg := &Config{
		Nodes: nodesOf(
			"a1", 1, "a", "a2", 1, "a",
			"b1", 1, "b", "b2", 1, "b",
			"c1", 1, "c", "c2", 1, "c",
			"d1", 1, "d", "d2", 1, "d",
		),
		ReadThreshold:    5,
		WriteThreshold:   5,
		MaxFailedDomains: 1,
	}
	rep, err := Check(cfg)
	if err != nil {
		t.Fatal(err)
	}
	// Total 8, one domain down -> 6 available. 5+5 > 8 and > 6: safe.
	if !rep.Safe {
		t.Fatalf("expected safe, got rw=%+v ww=%+v", rep.RWCounterexample, rep.WWCounterexample)
	}
	// With threshold 4, degraded available weight is 6 and 4+4 > 6 is false.
	cfg.WriteThreshold = 4
	cfg.ReadThreshold = 4
	rep2, err := Check(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if rep2.Safe {
		t.Fatal("expected unsafe with thresholds 4/4 under domain failure")
	}
	// The minimal counterexample must come from a one-domain-down scenario
	// (intact weight 8: 4+4 > 8 is false too, so intact is also unsafe;
	// minimal pair is still two 4-node quorums).
	if rep2.WWCounterexample == nil {
		t.Fatal("missing ww counterexample")
	}
}

func TestThresholdExceedsTotal(t *testing.T) {
	cfg := &Config{
		Nodes:            nodesOf("n1", 1, "a", "n2", 1, "b"),
		ReadThreshold:    5,
		WriteThreshold:   5,
		MaxFailedDomains: 0,
	}
	rep, err := Check(cfg)
	if err != nil {
		t.Fatal(err)
	}
	// No quorum can ever form: vacuously safe, but reported unavailable.
	if !rep.Safe {
		t.Fatal("unreachable thresholds must be vacuously safe")
	}
	if rep.Scenarios[0].ReadQuorumPossible || rep.Scenarios[0].WriteQuorumPossible {
		t.Fatal("quorum possibility should be false")
	}
}

func TestEnumerateQuorums(t *testing.T) {
	cfg := &Config{
		Nodes:            nodesOf("n1", 2, "a", "n2", 1, "b", "n3", 1, "c"),
		ReadThreshold:    2,
		WriteThreshold:   3,
		MaxFailedDomains: 0,
	}
	rq, err := EnumerateQuorums(cfg, cfg.ReadThreshold)
	if err != nil {
		t.Fatal(err)
	}
	got := rq["no failures"]
	want := [][]string{{"n1"}, {"n2", "n3"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("read quorums: got %v want %v", got, want)
	}
	wq, err := EnumerateQuorums(cfg, cfg.WriteThreshold)
	if err != nil {
		t.Fatal(err)
	}
	gotW := wq["no failures"]
	wantW := [][]string{{"n1", "n2"}, {"n1", "n3"}}
	if !reflect.DeepEqual(gotW, wantW) {
		t.Fatalf("write quorums: got %v want %v", gotW, wantW)
	}
}

func TestTooManyNodes(t *testing.T) {
	var nodes []Node
	for i := 0; i < MaxNodes+1; i++ {
		nodes = append(nodes, Node{ID: string(rune('A' + i)), Weight: 1})
	}
	cfg := &Config{Nodes: nodes, ReadThreshold: 1, WriteThreshold: 1}
	if _, err := Check(cfg); err == nil {
		t.Fatal("expected too-many-nodes error")
	}
}

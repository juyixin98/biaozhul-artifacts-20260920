package sim

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"reflect"
	"testing"

	orsetsim "orsetsim/internal/orset"
)

func baseConfig() Config {
	return Config{
		Seed:  42,
		Nodes: []string{"n1", "n2", "n3"},
		Network: NetworkConfig{
			LossProb: 0, DuplicateProb: 0, ReorderProb: 0,
			MinDelay: 1, MaxDelay: 1,
		},
	}
}

func runCfg(t *testing.T, cfg Config) *Report {
	t.Helper()
	e, err := NewEngine(cfg)
	if err != nil {
		t.Fatalf("config invalid: %v", err)
	}
	return e.Run()
}

func TestPerfectNetworkConverges(t *testing.T) {
	cfg := baseConfig()
	cfg.Events = []Op{
		{Time: 1, Node: "n1", Op: "add", Element: "a"},
		{Time: 2, Node: "n1", Op: "sync"},
		{Time: 4, Node: "n2", Op: "remove", Element: "a"},
		{Time: 5, Node: "n2", Op: "sync"},
		{Time: 7, Node: "n1", Op: "sync"},
	}
	r := runCfg(t, cfg)
	if !r.Converged {
		t.Fatalf("did not converge: %v", r.Nodes)
	}
	if len(r.FinalValues) != 0 {
		t.Fatalf("final = %v, want empty set", r.FinalValues)
	}
}

// Determinism: same config (same seed) → byte-identical report.
func TestDeterministicReplay(t *testing.T) {
	cfg := noisyConfig()
	r1 := runCfg(t, cfg)
	r2 := runCfg(t, cfg)
	b1, _ := json.Marshal(r1)
	b2, _ := json.Marshal(r2)
	if !reflect.DeepEqual(b1, b2) {
		t.Fatal("same seed produced different reports")
	}
	// Different seed should usually produce a different trace under loss, but
	// still converge once traffic flushes.
	cfg2 := cfg
	cfg2.Seed = 99
	r3 := runCfg(t, cfg2)
	b3, _ := json.Marshal(r3)
	if reflect.DeepEqual(b1, b3) {
		t.Fatal("different seeds unexpectedly produced identical traces")
	}
}

func noisyConfig() Config {
	cfg := baseConfig()
	cfg.Network = NetworkConfig{
		LossProb: 0.3, DuplicateProb: 0.4, ReorderProb: 0.6,
		MinDelay: 1, MaxDelay: 3,
	}
	cfg.Events = []Op{
		{Time: 1, Node: "n1", Op: "add", Element: "a"},
		{Time: 2, Node: "n2", Op: "add", Element: "b"},
		{Time: 3, Node: "n3", Op: "add", Element: "c"},
		{Time: 4, Node: "n1", Op: "sync"},
		{Time: 5, Node: "n2", Op: "sync"},
		{Time: 6, Node: "n3", Op: "sync"},
		// repeated gossip rounds so dropped messages get retransmitted
		{Time: 20, Node: "n1", Op: "sync"},
		{Time: 22, Node: "n2", Op: "sync"},
		{Time: 24, Node: "n3", Op: "sync"},
		{Time: 40, Node: "n1", Op: "sync"},
		{Time: 42, Node: "n2", Op: "sync"},
		{Time: 44, Node: "n3", Op: "sync"},
		// two more all-to-all flush rounds after earlier rounds drain:
		// retransmits anything lost three times in a row
		{Time: 60, Node: "n1", Op: "sync"},
		{Time: 62, Node: "n2", Op: "sync"},
		{Time: 64, Node: "n3", Op: "sync"},
		{Time: 80, Node: "n1", Op: "sync"},
		{Time: 82, Node: "n2", Op: "sync"},
		{Time: 84, Node: "n3", Op: "sync"},
	}
	return cfg
}

// With drops, duplicates and reordering, enough gossip rounds must converge
// all three replicas to the union. This is the "重复同步收敛" acceptance test.
func TestLossyReorderedRepeatedSyncConverges(t *testing.T) {
	for _, seed := range []int64{1, 2, 3, 4, 5, 17, 42, 100, 2024} {
		cfg := noisyConfig()
		cfg.Seed = seed
		r := runCfg(t, cfg)
		if !r.Converged {
			t.Fatalf("seed %d: replicas did not converge after repeated sync:\n%s",
				seed, dumpNodes(r))
		}
		want := []string{"a", "b", "c"}
		if !reflect.DeepEqual(r.FinalValues, want) {
			t.Fatalf("seed %d: final = %v, want %v", seed, r.FinalValues, want)
		}
		if r.Counts["drop"] == 0 {
			t.Fatalf("seed %d: no drops occurred, test did not exercise loss", seed)
		}
		if r.Counts["duplicate"] == 0 {
			t.Fatalf("seed %d: no duplicates occurred, test did not exercise duplication", seed)
		}
		// Every non-dropped send delivers once; duplicates add extra copies.
		if r.Counts["deliver"] < r.Counts["send"]-r.Counts["drop"] {
			t.Fatalf("seed %d: deliveries %d < non-dropped sends %d",
				seed, r.Counts["deliver"], r.Counts["send"]-r.Counts["drop"])
		}
	}
}

// Explicit out-of-order delivery: a later full-state sync arrives before an
// earlier one. CRDT merge must converge regardless. Enumerate both arrival
// orders at the OR-Set level (the sim-level random version is tested above).
func TestOutOfOrderMergeEnumeration(t *testing.T) {
	mkPair := func() (*orsetsim.State, *orsetsim.State) {
		src := orsetsim.New()
		src.Add("n1", 1, "a") // snapshot v1: {a}

		v1 := src.Clone()
		src.Add("n1", 2, "b")
		src.Remove("a") // snapshot v2: {b}, tombstone A:1
		v2 := src.Clone()
		return v1, v2
	}

	for _, order := range [][]int{{0, 1}, {1, 0}} {
		dst := orsetsim.New()
		v1, v2 := mkPair()
		snaps := []*orsetsim.State{v1, v2}
		dst.Merge(snaps[order[0]])
		dst.Merge(snaps[order[1]])
		if !reflect.DeepEqual(dst.Values(), []string{"b"}) {
			t.Fatalf("delivery order %v: values = %v, want [b]", order, dst.Values())
		}
		if dst.Lookup("a") {
			t.Fatalf("delivery order %v: stale snapshot resurrected a", order)
		}
	}
}

// Concurrent adds against the same element with a remove in the middle must
// keep the concurrent add (acceptance: 并发新增保留) at the simulator level.
func TestSimConcurrentAddPreserved(t *testing.T) {
	cfg := baseConfig()
	cfg.Events = []Op{
		{Time: 1, Node: "n1", Op: "add", Element: "x"},
		// n1 and n2 both add x before either has heard from the other.
		{Time: 1, Node: "n2", Op: "add", Element: "x"},
		{Time: 3, Node: "n1", Op: "remove", Element: "x"}, // saw only its own tag
		{Time: 5, Node: "n1", Op: "sync"},
		{Time: 7, Node: "n2", Op: "sync"},
		{Time: 9, Node: "n3", Op: "sync", Target: "n2"},
		{Time: 11, Node: "n3", Op: "sync", Target: "n1"},
	}
	r := runCfg(t, cfg)
	if !r.Converged {
		t.Fatalf("did not converge: %s", dumpNodes(r))
	}
	if !reflect.DeepEqual(r.FinalValues, []string{"x"}) {
		t.Fatalf("concurrent add lost: final = %v, want [x]", r.FinalValues)
	}
}

// 100% reordered network (no loss) converges immediately.
func TestFullReorderConverges(t *testing.T) {
	cfg := baseConfig()
	cfg.Network.ReorderProb = 0.999
	cfg.Network.MaxDelay = 2
	cfg.Events = []Op{
		{Time: 1, Node: "n1", Op: "add", Element: "a"},
		{Time: 2, Node: "n1", Op: "sync"},
		{Time: 3, Node: "n2", Op: "add", Element: "b"},
		{Time: 4, Node: "n2", Op: "sync"},
		{Time: 30, Node: "n3", Op: "sync"},
	}
	r := runCfg(t, cfg)
	if !r.Converged || !reflect.DeepEqual(r.FinalValues, []string{"a", "b"}) {
		t.Fatalf("reorder-only run failed: converged=%v vals=%v\n%s",
			r.Converged, r.FinalValues, dumpNodes(r))
	}
	if r.Counts["reorder"] == 0 {
		t.Fatal("expected reordered messages")
	}
}

// Duplicate-only network: idempotent merge leaves exactly one effect.
func TestDuplicateDeliveryIdempotent(t *testing.T) {
	cfg := baseConfig()
	cfg.Network.DuplicateProb = 0.999
	cfg.Events = []Op{
		{Time: 1, Node: "n1", Op: "add", Element: "a"},
		{Time: 2, Node: "n1", Op: "sync"},
		{Time: 20, Node: "n2", Op: "sync"},
	}
	r := runCfg(t, cfg)
	if !r.Converged {
		t.Fatalf("dup run did not converge: %s", dumpNodes(r))
	}
	// Count tag occurrences in n2's state: must remain one per add despite
	// duplicated deliveries.
	for _, nf := range r.Nodes {
		for e, tags := range nf.State.Adds {
			seen := map[orsetsim.Tag]int{}
			for _, tg := range tags {
				seen[tg]++
				if seen[tg] > 1 {
					t.Fatalf("node %s element %s has duplicated tag %v", nf.Node, e, tg)
				}
			}
		}
	}
}

// Complete loss of the only sync means known divergence and converged=false.
func TestTotalLossDoesNotFalselyConverge(t *testing.T) {
	cfg := baseConfig()
	cfg.Network.LossProb = 0.999
	cfg.Events = []Op{
		{Time: 1, Node: "n1", Op: "add", Element: "a"},
		{Time: 2, Node: "n1", Op: "sync"},
	}
	r := runCfg(t, cfg)
	if r.Converged {
		t.Fatal("report must say converged=false when every message was lost")
	}
}

// Coordinated GC after all replicas observed the tombstone compacts state
// without changing queries.
func TestSimCoordinatedGC(t *testing.T) {
	cfg := baseConfig()
	cfg.Events = []Op{
		{Time: 1, Node: "n1", Op: "add", Element: "x"},
		{Time: 2, Node: "n1", Op: "sync"},
		{Time: 5, Node: "n1", Op: "remove", Element: "x"},
		{Time: 6, Node: "n1", Op: "sync"},
		{Time: 8, Node: "n2", Op: "sync"},
		{Time: 10, Node: "n3", Op: "sync"},
		// flush rounds: every node gossips twice, spaced so delayed messages
		// (this run uses loss=0, delay=1) are fully absorbed
		{Time: 20, Node: "n1", Op: "sync"},
		{Time: 20, Node: "n2", Op: "sync"},
		{Time: 20, Node: "n3", Op: "sync"},
		{Time: 30, Node: "n1", Op: "gc"},
	}
	r := runCfg(t, cfg)
	if !r.Converged || len(r.FinalValues) != 0 {
		t.Fatalf("gc precondition failed: converged=%v vals=%v", r.Converged, r.FinalValues)
	}
	for _, nf := range r.Nodes {
		if len(nf.State.Adds) != 0 || len(nf.State.Tombstones()) != 0 {
			t.Fatalf("node %s not compacted after GC: %s", nf.Node, nf.State.Canonical())
		}
	}
}

// Random fuzz: random ops + noisy network + repeated gossip rounds must
// always converge (all replicas equal) on every seed.
func TestFuzzConvergence(t *testing.T) {
	rng := rand.New(rand.NewSource(12345))
	for iter := 0; iter < 25; iter++ {
		cfg := baseConfig()
		cfg.Seed = int64(iter + 1)
		cfg.Network = NetworkConfig{
			LossProb: 0.4, DuplicateProb: 0.3, ReorderProb: 0.5,
			MinDelay: 1, MaxDelay: 3,
		}
		elems := []string{"a", "b", "c"}
		var evs []Op
		tick := uint64(1)
		for round := 0; round < 6; round++ {
			for _, n := range cfg.Nodes {
				e := elems[rng.Intn(len(elems))]
				op := "add"
				if rng.Intn(2) == 0 {
					op = "remove"
				}
				evs = append(evs, Op{Time: tick, Node: n, Op: op, Element: e})
				tick++
			}
			for _, n := range cfg.Nodes {
				evs = append(evs, Op{Time: tick, Node: n, Op: "sync"})
			}
			tick += 25 // let delayed/reordered messages drain before next round
		}
		// Dedicated convergence phase with no intervening writes: repeated
		// all-to-all gossip retransmits through the 40%-loss network.
		for flush := 0; flush < 5; flush++ {
			for _, n := range cfg.Nodes {
				evs = append(evs, Op{Time: tick, Node: n, Op: "sync"})
			}
			tick += 20
		}
		cfg.Events = evs
		r := runCfg(t, cfg)
		if !r.AllStatesEqual {
			t.Fatalf("fuzz iter %d: replicas did not converge:\n%s", iter, dumpNodes(r))
		}
		for _, v := range r.FinalValues {
			if v != "a" && v != "b" && v != "c" {
				t.Fatalf("fuzz iter %d: unexpected value %q", iter, v)
			}
		}
	}
}

func TestValidationErrors(t *testing.T) {
	bad := []Config{
		{Nodes: nil},
		{Nodes: []string{"a", "a"}},
		{Nodes: []string{"a"}, Network: NetworkConfig{MinDelay: 5, MaxDelay: 1}},
		{Nodes: []string{"a"}, Network: NetworkConfig{LossProb: 1}},
		{Nodes: []string{"a"}, Events: []Op{{Node: "b", Op: "add", Element: "x"}}},
		{Nodes: []string{"a"}, Events: []Op{{Node: "a", Op: "frobnicate"}}},
		{Nodes: []string{"a", "b"}, Events: []Op{{Node: "a", Op: "sync", Target: "a"}}},
		{Nodes: []string{"a", "b"}, Events: []Op{{Node: "a", Op: "sync", Target: "zzz"}}},
	}
	for i, cfg := range bad {
		e, err := NewEngine(cfg)
		if err == nil {
			_ = e
			t.Fatalf("bad config %d accepted", i)
		}
	}
}

func dumpNodes(r *Report) string {
	s := ""
	for _, nf := range r.Nodes {
		s += fmt.Sprintf("  %s: %s\n", nf.Node, nf.State.Canonical())
	}
	return s
}

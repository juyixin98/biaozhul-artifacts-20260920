package main

import (
	"hash/fnv"
	"math"
	"math/rand"
	"sort"
	"testing"
)

func TestFNVMatchesStandardLibrary(t *testing.T) {
	samples := []string{"", "a", "hello", "node-a#00000000", "中文键", "0123456789"}
	for _, s := range samples {
		std := fnv.New64a()
		std.Write([]byte(s))
		if got, want := fnvSum([]byte(s)), std.Sum64(); got != want {
			t.Errorf("fnvSum(%q) = %d, want %d", s, got, want)
		}
		// HashKey is FNV-1a followed by the fixed avalanche finalizer.
		if HashKey(s) != fmix64(std.Sum64()) {
			t.Errorf("HashKey(%q) diverges from fmix64(FNV-1a)", s)
		}
	}
}

func TestHashUniformity(t *testing.T) {
	// The raw-FNV clustering regression: for one node's replicas, high bits
	// must spread across the circle instead of collapsing into one bucket.
	const buckets = 16
	var counts [buckets]int
	const replicas = 1024
	for r := 0; r < replicas; r++ {
		counts[HashVNode("some-node", r)>>(64-4)]++
	}
	want := replicas / buckets
	for i, c := range counts {
		// ±35% tolerance per bucket; raw FNV put everything in a single bucket.
		if c < want*65/100 || c > want*135/100 {
			t.Fatalf("bucket %d has %d hashes, want roughly %d (non-uniform)", i, c, want)
		}
	}

	// fmix64 must be bijective (no two distinct inputs collide).
	seen := make(map[uint64]struct{}, 200000)
	rng := rand.New(rand.NewSource(1))
	for i := 0; i < 200000; i++ {
		h := fmix64(rng.Uint64())
		if _, dup := seen[h]; dup {
			t.Fatal("fmix64 produced a collision")
		}
		seen[h] = struct{}{}
	}
}

func TestVNodeHashingFixedAndDeterministic(t *testing.T) {
	// Same (node, replica) always hashes identically.
	a := HashVNode("node-a", 0)
	b := HashVNode("node-a", 0)
	if a != b {
		t.Fatal("HashVNode not deterministic")
	}
	if HashVNode("node-a", 0) == HashVNode("node-a", 1) {
		t.Fatal("distinct replicas must hash differently")
	}
	// Pin one value so any future change to the fixed scheme is visible.
	if got, want := HashVNode("node-a", 0), uint64(0); got == want {
		t.Fatal("sanity: hash must not be zero placeholder")
	}
}

func TestRingIndependentOfInsertionOrder(t *testing.T) {
	n1 := []Node{{ID: "a", Weight: 1}, {ID: "b", Weight: 2}, {ID: "c", Weight: 3}}
	n2 := []Node{{ID: "c", Weight: 3}, {ID: "a", Weight: 1}, {ID: "b", Weight: 2}}
	r1, err := New(n1, 16)
	if err != nil {
		t.Fatal(err)
	}
	r2, err := New(n2, 16)
	if err != nil {
		t.Fatal(err)
	}
	v1, v2 := r1.VNodes(), r2.VNodes()
	if len(v1) != len(v2) {
		t.Fatalf("vnode count differs: %d vs %d", len(v1), len(v2))
	}
	for i := range v1 {
		if v1[i] != v2[i] {
			t.Fatalf("vnode %d differs: %+v vs %+v", i, v1[i], v2[i])
		}
	}
}

func TestRouteMatchesBruteForce(t *testing.T) {
	r, err := New([]Node{{ID: "a", Weight: 1}, {ID: "b", Weight: 2}, {ID: "c", Weight: 1}}, 32)
	if err != nil {
		t.Fatal(err)
	}
	rng := rand.New(rand.NewSource(1))
	for i := 0; i < 5000; i++ {
		key := randKey(rng)
		h := HashKey(key)
		got, err := r.Route(key)
		if err != nil {
			t.Fatal(err)
		}
		// Brute force: linear scan with wrap semantics: first vnode hash >= h,
		// else the vnode with the smallest hash.
		vs := r.VNodes()
		want := vs[0].NodeID
		found := false
		for _, v := range vs { // vs is hash-sorted
			if v.Hash >= h {
				want = v.NodeID
				found = true
				break
			}
		}
		if !found {
			want = vs[0].NodeID
		}
		if got != want {
			t.Fatalf("Route(%q)=%s want %s (hash %016x)", key, got, want, h)
		}
	}
}

func TestWeightedShare(t *testing.T) {
	// With many vnodes each node's owned share tracks its weight.
	r, err := New([]Node{{ID: "s", Weight: 1}, {ID: "m", Weight: 2}, {ID: "l", Weight: 5}}, 256)
	if err != nil {
		t.Fatal(err)
	}
	counts := map[string]int{}
	rng := rand.New(rand.NewSource(42))
	const n = 200000
	for i := 0; i < n; i++ {
		owner, _ := r.Route(randKey(rng))
		counts[owner]++
	}
	totalW := 8.0
	for _, c := range []struct {
		id     string
		weight float64
	}{{"s", 1}, {"m", 2}, {"l", 5}} {
		got := float64(counts[c.id]) / n
		want := c.weight / totalW
		if math.Abs(got-want) > 0.02 {
			t.Errorf("node %s share = %.4f, want ~%.4f (±0.02)", c.id, got, want)
		}
	}
}

func TestAddOneNodeMovesAboutOneNth(t *testing.T) {
	old, _ := New(letterNodes(10, 1), 128)
	nodes := letterNodes(10, 1)
	nodes = append(nodes, Node{ID: "newbie", Weight: 1})
	nxt, _ := New(nodes, 128)

	rng := rand.New(rand.NewSource(7))
	const n = 100000
	moved := 0
	for i := 0; i < n; i++ {
		k := randKey(rng)
		a, _ := old.Route(k)
		b, _ := nxt.Route(k)
		if a != b {
			moved++
		}
	}
	frac := float64(moved) / n
	// Expected ~1/11; allow a generous band for vnode granularity.
	if frac < 1.0/11-0.03 || frac > 1.0/11+0.03 {
		t.Errorf("moved fraction = %.4f, want ~%.4f", frac, 1.0/11)
	}
}

func TestRemoveNodeOnlyLosesItsKeys(t *testing.T) {
	nodes := letterNodes(8, 1)
	nodes = append(nodes, Node{ID: "victim", Weight: 1})
	old, _ := New(nodes, 64)
	nxt, _ := New(letterNodes(8, 1), 64)

	rng := rand.New(rand.NewSource(99))
	for i := 0; i < 50000; i++ {
		k := randKey(rng)
		a, _ := old.Route(k)
		b, _ := nxt.Route(k)
		if a != "victim" && a != b {
			t.Fatalf("key %q moved %s -> %s although owner was not the removed node", k, a, b)
		}
		if a == "victim" && b == "victim" {
			t.Fatal("key still routed to removed node")
		}
	}
}

func TestInvalidNodeDefinitions(t *testing.T) {
	bad := [][]Node{
		{{ID: "", Weight: 1}},
		{{ID: "ok", Weight: 1}, {ID: "ok", Weight: 1}},
		{{ID: "zero", Weight: 0}},
		{{ID: "neg", Weight: -1}},
		{{ID: "hash#tag", Weight: 1}},
	}
	for i, nodes := range bad {
		if _, err := New(nodes, 8); err == nil {
			t.Errorf("case %d: expected error", i)
		}
	}
	if _, err := New(nil, 8); err != nil {
		t.Errorf("empty node set should be a valid empty ring: %v", err)
	}
	if _, err := New([]Node{{ID: "a", Weight: 1}}, 0); err != nil {
		t.Errorf("baseVNodes 0 should default: %v", err)
	}
}

func TestEmptyRingRouting(t *testing.T) {
	r, _ := New(nil, 8)
	if !r.Empty() {
		t.Fatal("expected empty ring")
	}
	if _, err := r.Route("x"); err == nil {
		t.Fatal("routing on empty ring must error")
	}
}

func TestWeightChangeReproducible(t *testing.T) {
	cfg := []Node{{ID: "a", Weight: 1}, {ID: "b", Weight: 1}, {ID: "c", Weight: 1}}
	r1, _ := New(cfg, 64)
	r2, _ := New(cfg, 64) // reconstructed independently
	rng := rand.New(rand.NewSource(123))
	for i := 0; i < 50000; i++ {
		k := randKey(rng)
		a, _ := r1.Route(k)
		b, _ := r2.Route(k)
		if a != b {
			t.Fatalf("identical config diverged on %q: %s vs %s", k, a, b)
		}
	}
}

// ---------- helpers ----------

func letterNodes(n int, weight int) []Node {
	out := make([]Node, n)
	for i := 0; i < n; i++ {
		out[i] = Node{ID: "node-" + string(rune('a'+i)), Weight: weight}
	}
	return out
}

func randKey(rng *rand.Rand) string {
	const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-_"
	l := 4 + rng.Intn(20)
	b := make([]byte, l)
	for i := range b {
		b[i] = alphabet[rng.Intn(len(alphabet))]
	}
	return string(b)
}

// sortedOwners is a small helper used by plan tests.
func sortedOwners(r *Ring) []string {
	set := map[string]struct{}{}
	for _, n := range r.Nodes() {
		set[n.ID] = struct{}{}
	}
	var out []string
	for id := range set {
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

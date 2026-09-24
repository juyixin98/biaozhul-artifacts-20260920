package ring

import (
	"encoding/hex"
	"math/rand"
	"reflect"
	"testing"
)

func TestHashFixedVectors(t *testing.T) {
	cases := []struct {
		in   string
		want string // first 8 bytes of SHA-256, hex
	}{
		{"", "e3b0c44298fc1c14"},
		{"hello", "2cf24dba5fb0a30e"},
		{"node-a#vn0", "d033136b7db2d6dd"},
	}
	for _, c := range cases {
		got := Hash([]byte(c.in))
		wantBytes, _ := hex.DecodeString(c.want)
		want := uint64(wantBytes[0])<<56 | uint64(wantBytes[1])<<48 |
			uint64(wantBytes[2])<<40 | uint64(wantBytes[3])<<32 |
			uint64(wantBytes[4])<<24 | uint64(wantBytes[5])<<16 |
			uint64(wantBytes[6])<<8 | uint64(wantBytes[7])
		if got != want {
			t.Errorf("Hash(%q) = %016x, want %s", c.in, got, c.want)
		}
	}
}

func TestVNodeKeyFormat(t *testing.T) {
	if got, want := VNodeKey("node-a", 7), "node-a#vn7"; got != want {
		t.Fatalf("VNodeKey = %q, want %q", got, want)
	}
}

func TestNewValidation(t *testing.T) {
	if _, err := New("x", nil, 0); err == nil {
		t.Error("expected error for empty nodes")
	}
	if _, err := New("x", []Node{{ID: "a"}, {ID: "a"}}, 0); err == nil {
		t.Error("expected error for duplicate node id")
	}
	if _, err := New("x", []Node{{ID: "a", Weight: -1}}, 0); err == nil {
		t.Error("expected error for negative weight")
	}
	if _, err := New("x", []Node{{ID: "", Weight: 1}}, 0); err == nil {
		t.Error("expected error for empty node id")
	}
}

func TestZeroWeightBecomesOne(t *testing.T) {
	r, err := New("x", []Node{{ID: "a", Weight: 0}, {ID: "b", Weight: 2}}, 16)
	if err != nil {
		t.Fatal(err)
	}
	var a, b int
	for _, v := range r.vnodes {
		switch v.NodeID {
		case "a":
			a++
		case "b":
			b++
		}
	}
	if a != 16 || b != 32 {
		t.Errorf("vnode counts a=%d b=%d, want 16/32", a, b)
	}
}

func TestDeterministicIndependentOfInputOrder(t *testing.T) {
	nodes := []Node{
		{ID: "delta", Weight: 3},
		{ID: "alpha", Weight: 1},
		{ID: "charlie", Weight: 2},
		{ID: "bravo", Weight: 1},
	}
	r1, err := New("r", nodes, 64)
	if err != nil {
		t.Fatal(err)
	}
	shuffled := []Node{nodes[2], nodes[0], nodes[3], nodes[1]}
	r2, err := New("r", shuffled, 64)
	if err != nil {
		t.Fatal(err)
	}
	if len(r1.vnodes) != len(r2.vnodes) {
		t.Fatalf("vnode count differs: %d vs %d", len(r1.vnodes), len(r2.vnodes))
	}
	for i := range r1.vnodes {
		if r1.vnodes[i] != r2.vnodes[i] {
			t.Fatalf("vnode %d differs: %+v vs %+v", i, r1.vnodes[i], r2.vnodes[i])
		}
	}
	if !reflect.DeepEqual(r1.Summary(), r2.Summary()) {
		t.Fatalf("summaries differ")
	}
}

func TestVNodesSorted(t *testing.T) {
	r, err := New("r", []Node{{ID: "a"}, {ID: "b"}, {ID: "c"}}, 8)
	if err != nil {
		t.Fatal(err)
	}
	for i := 1; i < len(r.vnodes); i++ {
		if !lessVNode(r.vnodes[i-1], r.vnodes[i]) {
			t.Fatalf("vnodes not strictly sorted at %d", i)
		}
	}
}

// ownerBySegments independently computes ownership from the segment model:
// vnode at distinct position p owns (prevDistinctPos, p], and the minimum
// vnode additionally owns the wrap segment (maxPos, 2^64) and [0, minPos].
func ownerBySegments(r *Ring, h uint64) string {
	pos := 0
	for pos < len(r.vnodes) && r.vnodes[pos].Position < h {
		pos++
	}
	if pos < len(r.vnodes) && r.vnodes[pos].Position == h {
		return r.vnodes[pos].NodeID // boundary point belongs to the vnode at p
	}
	if pos == len(r.vnodes) {
		return r.vnodes[0].NodeID // wrap segment -> minimum vnode
	}
	return r.vnodes[pos].NodeID // inside (prevPos, p] -> the vnode at p
}

func TestBoundaryOwnership(t *testing.T) {
	r, err := New("r", []Node{{ID: "a"}, {ID: "b"}, {ID: "c"}}, 1)
	if err != nil {
		t.Fatal(err)
	}
	// Exhaustively cross-check OwnerAt against the independent segment model
	// at every cut point and its immediate neighbors.
	checked := 0
	probe := func(h uint64) {
		want := ownerBySegments(r, h)
		if got := r.OwnerAt(h); got != want {
			t.Fatalf("OwnerAt(%016x) = %s, segment model says %s", h, got, want)
		}
		checked++
	}
	for _, v := range r.vnodes {
		probe(v.Position)
		if v.Position > 0 {
			probe(v.Position - 1)
		}
		if v.Position < ^uint64(0) {
			probe(v.Position + 1)
		}
	}
	probe(0)
	probe(^uint64(0))
	if checked < 3*len(r.vnodes) {
		t.Fatalf("only checked %d points", checked)
	}
}

func TestRoutingStableForManyKeys(t *testing.T) {
	r, err := New("r", []Node{{ID: "a"}, {ID: "b"}, {ID: "c"}, {ID: "d"}}, 64)
	if err != nil {
		t.Fatal(err)
	}
	rng := rand.New(rand.NewSource(42))
	known := map[string]string{}
	for i := 0; i < 50000; i++ {
		key := randomKey(rng)
		owner := r.Owner(key)
		if prev, ok := known[key]; ok && prev != owner {
			t.Fatalf("key %q changed owner %s -> %s", key, prev, owner)
		}
		known[key] = owner
		if r.Owner(key) != owner || r.OwnerAt(Hash([]byte(key))) != owner {
			t.Fatalf("owner lookup inconsistent for %q", key)
		}
	}
}

func TestWeightedDistribution(t *testing.T) {
	// Nodes with weight 1 and 3; expect roughly 25% / 75% of the key space.
	r, err := New("r", []Node{{ID: "light", Weight: 1}, {ID: "heavy", Weight: 3}}, 128)
	if err != nil {
		t.Fatal(err)
	}
	rng := rand.New(rand.NewSource(7))
	const n = 200000
	counts := map[string]int{}
	for i := 0; i < n; i++ {
		counts[r.Owner(randomKey(rng))]++
	}
	heavy := float64(counts["heavy"]) / n
	if heavy < 0.70 || heavy > 0.80 {
		t.Fatalf("heavy share = %.3f, want ~0.75", heavy)
	}
	light := float64(counts["light"]) / n
	if light < 0.20 || light > 0.30 {
		t.Fatalf("light share = %.3f, want ~0.25", light)
	}
}

func TestEqualWeightDistribution(t *testing.T) {
	r, err := New("r", []Node{{ID: "a"}, {ID: "b"}, {ID: "c"}, {ID: "d"}}, 128)
	if err != nil {
		t.Fatal(err)
	}
	rng := rand.New(rand.NewSource(99))
	const n = 300000
	counts := map[string]int{}
	for i := 0; i < n; i++ {
		counts[r.Owner(randomKey(rng))]++
	}
	for id, c := range counts {
		share := float64(c) / n
		if share < 0.21 || share > 0.29 {
			t.Fatalf("node %s share = %.3f, want ~0.25", id, share)
		}
	}
}

func randomKey(rng *rand.Rand) string {
	b := make([]byte, 4+rng.Intn(28))
	rng.Read(b)
	return hex.EncodeToString(b)
}

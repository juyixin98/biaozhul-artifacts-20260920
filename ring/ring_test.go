package ring

import (
	"strings"
	"testing"
)

func TestOwnerDeterministic(t *testing.T) {
	nodes := []NodeSpec{{ID: "a", Weight: 1}, {ID: "b", Weight: 1}, {ID: "c", Weight: 1}}
	r1, err := New(0, nodes, 64)
	if err != nil {
		t.Fatal(err)
	}
	r2, _ := New(0, []NodeSpec{
		{ID: "c", Weight: 1}, {ID: "a", Weight: 1}, {ID: "b", Weight: 1},
	}, 64)
	for _, k := range []string{"x", "y", "k1", "user:42", "中文键"} {
		if r1.Owner(k) != r2.Owner(k) {
			t.Fatalf("owner of %q depends on input order: %s vs %s", k, r1.Owner(k), r2.Owner(k))
		}
	}
}

func TestNewRejects(t *testing.T) {
	cases := []struct {
		name  string
		nodes []NodeSpec
		vn    int
	}{
		{"empty", nil, 64},
		{"zero vnodes", []NodeSpec{{ID: "a", Weight: 1}}, 0},
		{"empty id", []NodeSpec{{ID: "", Weight: 1}}, 64},
		{"duplicate id", []NodeSpec{{ID: "a", Weight: 1}, {ID: "a", Weight: 1}}, 64},
		{"zero weight", []NodeSpec{{ID: "a", Weight: 0}}, 64},
	}
	for _, tc := range cases {
		if _, err := New(0, tc.nodes, tc.vn); err == nil {
			t.Fatalf("%s: expected error", tc.name)
		}
	}
}

// TestWeightProportion checks virtual-node counts scale with weight and the
// observed key share is close to the weight share.
func TestWeightProportion(t *testing.T) {
	nodes := []NodeSpec{{ID: "lite", Weight: 1}, {ID: "heavy", Weight: 4}}
	r, _ := New(0, nodes, 256)
	if got := r.VNodes(); got != 1280 {
		t.Fatalf("vnode count = %d, want 1280", got)
	}
	counts := map[string]int{}
	for i := 0; i < 40000; i++ {
		counts[r.Owner(keyN(i))]++
	}
	ratio := float64(counts["heavy"]) / float64(counts["lite"])
	if ratio < 3.6 || ratio > 4.4 {
		t.Fatalf("weight ratio %.2f not near 4.0 (lite=%d heavy=%d)", ratio, counts["lite"], counts["heavy"])
	}
}

// TestAddOneNodeMovesExpectedFraction: adding one of N+1 nodes should move
// roughly 1/(N+1) of keys — the core balanced-hashing guarantee.
func TestAddOneNodeMovesExpectedFraction(t *testing.T) {
	const vn = 256
	old, _ := New(0, []NodeSpec{
		{ID: "n1", Weight: 1}, {ID: "n2", Weight: 1}, {ID: "n3", Weight: 1},
	}, vn)
	nw, _ := New(1, []NodeSpec{
		{ID: "n1", Weight: 1}, {ID: "n2", Weight: 1}, {ID: "n3", Weight: 1}, {ID: "n4", Weight: 1},
	}, vn)
	keys := make([]string, 6000)
	for i := range keys {
		keys[i] = keyN(i)
	}
	moves := old.Diff(nw, keys)
	moved := 0
	for _, m := range moves {
		if m.Migrates {
			moved++
		}
	}
	frac := float64(moved) / float64(len(keys))
	if frac < 0.20 || frac > 0.30 {
		t.Fatalf("moved fraction %.3f outside ~0.25", frac)
	}
	// Every move must go TO the new node or redistribute; the key invariant is
	// only that unmoved keys keep their owner.
	for _, m := range moves {
		if !m.Migrates && m.From != m.To {
			t.Fatal("non-migrate report with differing owners")
		}
	}
}

// TestNoKeysMoveWhenUnchanged verifies a rebuild of the same set moves nothing.
func TestNoKeysMoveWhenUnchanged(t *testing.T) {
	specs := []NodeSpec{{ID: "a", Weight: 2}, {ID: "b", Weight: 3}}
	r1, _ := New(0, specs, 64)
	r2, _ := New(1, specs, 64)
	keys := make([]string, 500)
	for i := range keys {
		keys[i] = keyN(i)
	}
	for _, m := range r1.Diff(r2, keys) {
		if m.Migrates {
			t.Fatalf("key %s moved %s->%s under unchanged topology", m.Key, m.From, m.To)
		}
	}
}

// TestRemoveNodeRedistributesToSurvivors verifies every key that the removed
// node owned moves to a surviving node; no key is assigned to the removed node
// on the new ring.
func TestRemoveNodeRedistributesToSurvivors(t *testing.T) {
	old, _ := New(0, []NodeSpec{
		{ID: "n1", Weight: 1}, {ID: "n2", Weight: 1}, {ID: "n3", Weight: 1}, {ID: "n4", Weight: 1},
	}, 128)
	nw, _ := New(1, []NodeSpec{
		{ID: "n1", Weight: 1}, {ID: "n3", Weight: 1}, {ID: "n4", Weight: 1},
	}, 128)
	keys := make([]string, 4000)
	for i := range keys {
		keys[i] = keyN(i)
	}
	removedOwned := 0
	for _, m := range old.Diff(nw, keys) {
		if m.To == "n2" {
			t.Fatalf("key %s assigned to removed node n2", m.Key)
		}
		if m.From == "n2" {
			removedOwned++
			if !m.Migrates {
				t.Fatalf("key %s stayed on removed node", m.Key)
			}
		}
	}
	if removedOwned == 0 {
		t.Fatal("test had no keys owned by the removed node; enlarge the sample")
	}
}

func keyN(i int) string {
	n := i
	var b strings.Builder
	b.WriteString("key-")
	for j := 0; j < 5; j++ {
		b.WriteByte(byte('a' + n%26))
		n /= 26
	}
	return b.String()
}

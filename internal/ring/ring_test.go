package ring

import "testing"

func nodes(ids ...string) []Node {
	out := make([]Node, len(ids))
	for i, id := range ids {
		out[i] = Node{ID: id, Weight: 1}
	}
	return out
}

func TestOwnershipStableOnScaleOut(t *testing.T) {
	old := Build(nodes("n1", "n2", "n3"), 128)
	newR := Build(append(nodes("n1", "n2", "n3"), Node{ID: "n4", Weight: 1}), 128)

	const keys = 20000
	moved := 0
	for i := 0; i < keys; i++ {
		k := "k-" + itoa(i)
		if old.Owner(k) != newR.Owner(k) {
			moved++
		}
	}
	frac := float64(moved) / keys
	// Adding a 4th equal-weight node should move roughly 1/4 of keys; require
	// a loose, realistic band around that.
	if frac < 0.10 || frac > 0.45 {
		t.Fatalf("moved fraction %.3f outside [0.10,0.45] (moved=%d/%d)", frac, moved, keys)
	}
}

func TestOwnersCoverAllKeys(t *testing.T) {
	r := Build(nodes("a", "b", "c"), 64)
	owns := map[string]int{}
	for i := 0; i < 5000; i++ {
		owns[r.Owner("x"+itoa(i))]++
	}
	if len(owns) != 3 {
		t.Fatalf("expected all 3 nodes to own keys, got %d nodes: %v", len(owns), owns)
	}
}

func TestWeightsSkewOwnership(t *testing.T) {
	r := Build([]Node{{ID: "light", Weight: 1}, {ID: "heavy", Weight: 4}}, 128)
	owns := map[string]int{}
	for i := 0; i < 20000; i++ {
		owns[r.Owner("w"+itoa(i))]++
	}
	ratio := float64(owns["heavy"]) / float64(owns["light"])
	if ratio < 2.0 || ratio > 6.0 {
		t.Fatalf("weight ratio 4 not reflected in ownership: heavy/light=%.2f (%v)", ratio, owns)
	}
}

func TestPlanAddNodeTargetsOnlyNewNode(t *testing.T) {
	old := Build(nodes("n1", "n2", "n3"), 128)
	newR := Build(append(nodes("n1", "n2", "n3"), Node{ID: "n4"}), 128)
	tasks := Plan(old, newR)
	if len(tasks) == 0 {
		t.Fatal("expected migration tasks on scale-out")
	}
	for _, tk := range tasks {
		if tk.Dst != "n4" {
			t.Fatalf("task %s moves data to %s, want n4", tk.ID(), tk.Dst)
		}
		if tk.Src == "n4" {
			t.Fatalf("task %s migrates from the new node", tk.ID())
		}
	}
}

func TestPlanRemoveNodeTakesOnlyFromRemoved(t *testing.T) {
	old := Build(nodes("n1", "n2", "n3"), 128)
	newR := Build(nodes("n2", "n3"), 128)
	tasks := Plan(old, newR)
	if len(tasks) == 0 {
		t.Fatal("expected migration tasks on scale-in")
	}
	for _, tk := range tasks {
		if tk.Src != "n1" {
			t.Fatalf("task %s migrates from %s, want n1", tk.ID(), tk.Src)
		}
		if tk.Dst == "n1" {
			t.Fatalf("task %s moves data onto removed node", tk.ID())
		}
	}
}

// TestPlanCoversEveryMovedKey cross-checks the geometric plan against direct
// ownership comparison: a plan task with matching (src,dst) must contain every
// key whose owner changes.
func TestPlanCoversEveryMovedKey(t *testing.T) {
	cases := [][][]Node{
		{nodes("n1", "n2", "n3"), append(nodes("n1", "n2", "n3"), Node{ID: "n4"})},
		{nodes("n1", "n2", "n3"), nodes("n2", "n3")},
		{nodes("n1", "n2", "n3", "n4"), []Node{{ID: "n1"}, {ID: "n4"}, {ID: "n5", Weight: 2}}},
	}
	for ci, c := range cases {
		old := Build(c[0], 96)
		newR := Build(c[1], 96)
		tasks := Plan(old, newR)
		var moved, covered int
		for i := 0; i < 4000; i++ {
			k := "k" + itoa(i)
			h := HashKey(k)
			o, nw := old.Owner(k), newR.Owner(k)
			if o == nw {
				continue
			}
			moved++
			for _, tk := range tasks {
				if tk.Src == o && tk.Dst == nw && tk.Contains(h) {
					covered++
					break
				}
			}
		}
		if moved == 0 {
			t.Fatalf("case %d: no keys moved at all", ci)
		}
		if covered != moved {
			t.Fatalf("case %d: %d/%d moved keys covered by plan", ci, covered, moved)
		}
	}
}

func TestWrapInterval(t *testing.T) {
	tk := Task{Start: 100, End: 10} // wraps
	if tk.Contains(50) {
		t.Fatal("middle should not be inside wrapping interval")
	}
	if !tk.Contains(150) || !tk.Contains(5) || !tk.Contains(10) {
		t.Fatal("wrapping endpoints mis-handled")
	}
	if tk.Contains(100) {
		t.Fatal("start is exclusive")
	}
	tk2 := Task{Start: 10, End: 100}
	if !tk2.Contains(100) || tk2.Contains(10) {
		t.Fatal("non-wrapping boundary semantics wrong")
	}
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var b [20]byte
	p := len(b)
	for i > 0 {
		p--
		b[p] = byte('0' + i%10)
		i /= 10
	}
	if neg {
		p--
		b[p] = '-'
	}
	return string(b[p:])
}

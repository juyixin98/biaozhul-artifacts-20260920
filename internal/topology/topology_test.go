package topology

import "testing"

func TestBuildCostMatrixDefaultsAndOrdering(t *testing.T) {
	c := &Cluster{
		Devices: []Device{
			{ID: "b", MemoryMB: 80, NumaNode: 1},
			{ID: "a", MemoryMB: 80, NumaNode: 0},
			{ID: "c", MemoryMB: 80, NumaNode: 0},
		},
		DefaultSameNuma:  1,
		DefaultCrossNuma: 10,
	}
	order, idx, m, err := c.BuildCostMatrix()
	if err != nil {
		t.Fatal(err)
	}
	if order[0] != "a" || order[1] != "b" || order[2] != "c" {
		t.Fatalf("order = %v, want sorted [a b c]", order)
	}
	// a,c same NUMA => 1; a,b and b,c cross NUMA => 10.
	if got := m[idx["a"]][idx["c"]]; got != 1 {
		t.Errorf("a-c = %d, want 1", got)
	}
	if got := m[idx["a"]][idx["b"]]; got != 10 {
		t.Errorf("a-b = %d, want 10", got)
	}
	if m[0][0] != 0 {
		t.Errorf("diagonal = %d, want 0", m[0][0])
	}
	// Symmetry.
	for i := range m {
		for j := range m {
			if m[i][j] != m[j][i] {
				t.Fatalf("matrix not symmetric at (%d,%d)", i, j)
			}
		}
	}
}

func TestExplicitLinksOverrideDefaults(t *testing.T) {
	c := &Cluster{
		Devices: []Device{
			{ID: "a", MemoryMB: 80, NumaNode: 0},
			{ID: "b", MemoryMB: 80, NumaNode: 1},
		},
		Links:            []Link{{A: "b", B: "a", Cost: 3}}, // reversed order must still work
		DefaultSameNuma:  1,
		DefaultCrossNuma: 10,
	}
	_, idx, m, err := c.BuildCostMatrix()
	if err != nil {
		t.Fatal(err)
	}
	if m[idx["a"]][idx["b"]] != 3 {
		t.Errorf("explicit link ignored: got %d, want 3", m[idx["a"]][idx["b"]])
	}
}

func TestValidationErrors(t *testing.T) {
	cases := []struct {
		name string
		c    *Cluster
	}{
		{"no devices", &Cluster{}},
		{"empty id", &Cluster{Devices: []Device{{ID: "", MemoryMB: 8}}}},
		{"duplicate id", &Cluster{Devices: []Device{
			{ID: "a", MemoryMB: 8}, {ID: "a", MemoryMB: 8}}}},
		{"negative capacity", &Cluster{Devices: []Device{{ID: "a", MemoryMB: -1}}}},
		{"negative used", &Cluster{Devices: []Device{{ID: "a", MemoryMB: 8, UsedMemory: -1}}}},
		{"used over capacity", &Cluster{Devices: []Device{{ID: "a", MemoryMB: 8, UsedMemory: 9}}}},
		{"negative numa", &Cluster{Devices: []Device{{ID: "a", MemoryMB: 8, NumaNode: -1}}}},
		{"negative default", &Cluster{Devices: []Device{{ID: "a", MemoryMB: 8}}, DefaultSameNuma: -1}},
		{"unknown link endpoint", &Cluster{
			Devices:          []Device{{ID: "a", MemoryMB: 8}},
			Links:            []Link{{A: "a", B: "z"}},
			DefaultSameNuma:  1,
			DefaultCrossNuma: 2}},
		{"self link", &Cluster{
			Devices:          []Device{{ID: "a", MemoryMB: 8}},
			Links:            []Link{{A: "a", B: "a"}},
			DefaultSameNuma:  1,
			DefaultCrossNuma: 2}},
		{"negative link cost", &Cluster{
			Devices:          []Device{{ID: "a", MemoryMB: 8}, {ID: "b", MemoryMB: 8}},
			Links:            []Link{{A: "a", B: "b", Cost: -1}},
			DefaultSameNuma:  1,
			DefaultCrossNuma: 2}},
		{"conflicting links", &Cluster{
			Devices:         []Device{{ID: "a", MemoryMB: 8}, {ID: "b", MemoryMB: 8}},
			Links:           []Link{{A: "a", B: "b", Cost: 1}, {A: "b", B: "a", Cost: 2}},
			DefaultSameNuma: 1, DefaultCrossNuma: 2}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, _, _, err := tc.c.BuildCostMatrix(); err == nil {
				t.Fatal("expected error, got nil")
			}
		})
	}
}

func TestConsistentDuplicateLinksAllowed(t *testing.T) {
	c := &Cluster{
		Devices:          []Device{{ID: "a", MemoryMB: 8}, {ID: "b", MemoryMB: 8}},
		Links:            []Link{{A: "a", B: "b", Cost: 4}, {A: "b", B: "a", Cost: 4}},
		DefaultSameNuma:  1,
		DefaultCrossNuma: 2,
	}
	if _, _, _, err := c.BuildCostMatrix(); err != nil {
		t.Fatalf("identical duplicate links should be accepted, got %v", err)
	}
}

func TestFreeMemory(t *testing.T) {
	d := Device{MemoryMB: 100, UsedMemory: 30}
	if d.FreeMemory() != 70 {
		t.Fatalf("free = %d", d.FreeMemory())
	}
}

package topology

import "testing"

func TestBuildValidation(t *testing.T) {
	valid := Spec{
		Devices: []Device{
			{ID: "gpu0", MemoryMB: 8192, NUMANode: 0},
			{ID: "gpu1", MemoryMB: 8192, NUMANode: 1},
		},
	}
	if _, err := Build(valid); err != nil {
		t.Fatalf("合法输入应通过: %v", err)
	}

	cases := []struct {
		name string
		mut  func(*Spec)
	}{
		{"空设备列表", func(s *Spec) { s.Devices = nil }},
		{"空设备ID", func(s *Spec) { s.Devices[0].ID = "" }},
		{"重复设备ID", func(s *Spec) { s.Devices[1].ID = "gpu0" }},
		{"显存为零", func(s *Spec) { s.Devices[0].MemoryMB = 0 }},
		{"显存为负", func(s *Spec) { s.Devices[0].MemoryMB = -1 }},
		{"NUMA为负", func(s *Spec) { s.Devices[0].NUMANode = -1 }},
		{"链路引用未知设备", func(s *Spec) { s.Links = []Link{{A: "gpu0", B: "ghost", Cost: 1}} }},
		{"链路自环", func(s *Spec) { s.Links = []Link{{A: "gpu0", B: "gpu0", Cost: 1}} }},
		{"链路代价为负", func(s *Spec) { s.Links = []Link{{A: "gpu0", B: "gpu1", Cost: -1}} }},
		{"跨NUMA默认代价小于同NUMA", func(s *Spec) {
			s.DefaultSameNUMACost = 10
			s.DefaultCrossNUMACost = 1
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			spec := valid
			spec.Devices = append([]Device(nil), valid.Devices...)
			tc.mut(&spec)
			if _, err := Build(spec); err == nil {
				t.Fatal("应拒绝但未拒绝")
			}
		})
	}
}

func TestCostMatrixDefaultsAndOverrides(t *testing.T) {
	cl, err := Build(Spec{
		Devices: []Device{
			{ID: "a", MemoryMB: 1024, NUMANode: 0},
			{ID: "b", MemoryMB: 1024, NUMANode: 0},
			{ID: "c", MemoryMB: 1024, NUMANode: 1},
		},
		Links: []Link{{A: "a", B: "c", Cost: 3}},
	})
	if err != nil {
		t.Fatal(err)
	}
	ia, _ := cl.Index("a")
	ib, _ := cl.Index("b")
	ic, _ := cl.Index("c")

	if got := cl.Cost(ia, ib); got != DefaultSameNUMA {
		t.Errorf("同 NUMA 默认代价应为 %d，实际 %d", DefaultSameNUMA, got)
	}
	if got := cl.Cost(ib, ic); got != DefaultCrossNUMA {
		t.Errorf("跨 NUMA 默认代价应为 %d，实际 %d", DefaultCrossNUMA, got)
	}
	if got := cl.Cost(ia, ic); got != 3 {
		t.Errorf("显式链路应覆盖默认值，实际 %d", got)
	}
	if got := cl.Cost(ic, ia); got != 3 {
		t.Errorf("链路应对称，实际 %d", got)
	}
	if got := cl.Cost(ia, ia); got != 0 {
		t.Errorf("自身代价应为 0，实际 %d", got)
	}
}

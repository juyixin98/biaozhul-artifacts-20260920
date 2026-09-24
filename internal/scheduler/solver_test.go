package scheduler

import (
	"math/rand"
	"sort"
	"testing"

	"github.com/example/gpu-placement/internal/topology"
)

// bruteForce 是测试内独立实现的“最笨但显然正确”的枚举器：
// 用位掩码遍历所有大小为 k 的子集，直接按定义计算目标值，
// 用与生产代码不同的代码路径交叉验证求解器。
func bruteForce(cl *topology.Cluster, eligible []int, k int) (bestIdx []int, bestCost, bestCross int) {
	m := len(eligible)
	found := false
	for mask := 0; mask < (1 << m); mask++ {
		if popcount(mask) != k {
			continue
		}
		var idx []int
		for i := 0; i < m; i++ {
			if mask&(1<<i) != 0 {
				idx = append(idx, eligible[i])
			}
		}
		sort.Ints(idx)
		cost, cross := 0, 0
		for a := 0; a < len(idx); a++ {
			for b := a + 1; b < len(idx); b++ {
				cost += cl.Cost(idx[a], idx[b])
				if cl.NUMANode(idx[a]) != cl.NUMANode(idx[b]) {
					cross++
				}
			}
		}
		if !found || cost < bestCost ||
			(cost == bestCost && cross < bestCross) ||
			(cost == bestCost && cross == bestCross && lexLess(cl, idx, bestIdx)) {
			found = true
			bestIdx, bestCost, bestCross = idx, cost, cross
		}
	}
	return bestIdx, bestCost, bestCross
}

func popcount(x int) int {
	c := 0
	for x != 0 {
		x &= x - 1
		c++
	}
	return c
}

func lexLess(cl *topology.Cluster, a, b []int) bool {
	for i := 0; i < len(a) && i < len(b); i++ {
		if cl.DeviceID(a[i]) != cl.DeviceID(b[i]) {
			return cl.DeviceID(a[i]) < cl.DeviceID(b[i])
		}
	}
	return false
}

func mustCluster(t *testing.T, spec topology.Spec) *topology.Cluster {
	t.Helper()
	cl, err := topology.Build(spec)
	if err != nil {
		t.Fatalf("Build 失败: %v", err)
	}
	return cl
}

func allIndices(cl *topology.Cluster) []int {
	idx := make([]int, cl.N())
	for i := range idx {
		idx[i] = i
	}
	return idx
}

// TestExhaustiveMatchesIndependentBruteForce 验收核心：
// 固定小集群上，求解器结果必须与独立位掩码枚举完全一致
// （代价、跨 NUMA 对数、以及确定性裁决后的具体设备集合）。
func TestExhaustiveMatchesIndependentBruteForce(t *testing.T) {
	cl := mustCluster(t, topology.Spec{
		Devices: []topology.Device{
			{ID: "gpu0", MemoryMB: 24576, NUMANode: 0},
			{ID: "gpu1", MemoryMB: 24576, NUMANode: 0},
			{ID: "gpu2", MemoryMB: 24576, NUMANode: 0},
			{ID: "gpu3", MemoryMB: 24576, NUMANode: 1},
			{ID: "gpu4", MemoryMB: 24576, NUMANode: 1},
			{ID: "gpu5", MemoryMB: 24576, NUMANode: 1},
		},
		Links: []topology.Link{
			{A: "gpu0", B: "gpu1", Cost: 0}, // NVLink 直连，优于默认
			{A: "gpu3", B: "gpu4", Cost: 0},
		},
		DefaultSameNUMACost:  1,
		DefaultCrossNUMACost: 10,
	})

	eligible := allIndices(cl)
	for k := 1; k <= cl.N(); k++ {
		got := solve(cl, eligible, k)
		wantIdx, wantCost, wantCross := bruteForce(cl, eligible, k)

		if !got.exhaustive {
			t.Fatalf("k=%d: 小集群应走穷举路径", k)
		}
		if got.cost != wantCost {
			t.Errorf("k=%d: 代价 %d != 穷举最优 %d", k, got.cost, wantCost)
		}
		if got.crossPairs != wantCross {
			t.Errorf("k=%d: 跨 NUMA 对数 %d != %d", k, got.crossPairs, wantCross)
		}
		if !equalSet(cl, got.idx, wantIdx) {
			t.Errorf("k=%d: 设备集合 %v != 期望 %v", k, idsOf(cl, got.idx), idsOf(cl, wantIdx))
		}
		if got.evaluated != combCountLE(cl.N(), k, 1<<62) {
			t.Errorf("k=%d: 评估候选数 %d != C(%d,%d)", k, got.evaluated, cl.N(), k)
		}
	}
}

// TestRandomSmallClustersMatchBruteForce 用固定种子的随机拓扑做
// 性质测试：任何小集群、任意 k，求解器都必须等于独立穷举。
func TestRandomSmallClustersMatchBruteForce(t *testing.T) {
	rng := rand.New(rand.NewSource(20260924))
	for trial := 0; trial < 60; trial++ {
		n := 2 + rng.Intn(6) // 2..7 台设备
		numas := 1 + rng.Intn(3)
		spec := topology.Spec{
			DefaultSameNUMACost:  1 + rng.Intn(3),
			DefaultCrossNUMACost: 5 + rng.Intn(20),
		}
		for i := 0; i < n; i++ {
			spec.Devices = append(spec.Devices, topology.Device{
				ID:       "d" + string(rune('a'+i)),
				MemoryMB: 8192 * (1 + rng.Intn(4)),
				NUMANode: rng.Intn(numas),
			})
		}
		// 随机加一些显式链路（含跨 NUMA 的 NVLink 捷径）。
		for i := 0; i < n; i++ {
			for j := i + 1; j < n; j++ {
				if rng.Intn(3) == 0 {
					spec.Links = append(spec.Links, topology.Link{
						A:    spec.Devices[i].ID,
						B:    spec.Devices[j].ID,
						Cost: rng.Intn(15),
					})
				}
			}
		}
		cl := mustCluster(t, spec)
		eligible := allIndices(cl)
		for k := 1; k <= n; k++ {
			got := solve(cl, eligible, k)
			wantIdx, wantCost, wantCross := bruteForce(cl, eligible, k)
			if got.cost != wantCost || got.crossPairs != wantCross || !equalSet(cl, got.idx, wantIdx) {
				t.Fatalf("trial=%d n=%d k=%d: got {ids=%v cost=%d cross=%d}, want {ids=%v cost=%d cross=%d}\nspec=%+v",
					trial, n, k,
					idsOf(cl, got.idx), got.cost, got.crossPairs,
					idsOf(cl, wantIdx), wantCost, wantCross, spec)
			}
		}
	}
}

// TestCrossNUMAPenalty 跨 NUMA 惩罚：4 卡 2 NUMA，2 卡任务必须
// 落在同一 NUMA 内；显式 NVLink 跨 NUMA 链路则必须被优先选中。
func TestCrossNUMAPenalty(t *testing.T) {
	base := topology.Spec{
		Devices: []topology.Device{
			{ID: "gpu0", MemoryMB: 24576, NUMANode: 0},
			{ID: "gpu1", MemoryMB: 24576, NUMANode: 0},
			{ID: "gpu2", MemoryMB: 24576, NUMANode: 1},
			{ID: "gpu3", MemoryMB: 24576, NUMANode: 1},
		},
		DefaultSameNUMACost:  1,
		DefaultCrossNUMACost: 10,
	}

	t.Run("默认代价下同NUMA优先", func(t *testing.T) {
		s := New()
		if err := s.Configure(base); err != nil {
			t.Fatal(err)
		}
		d := s.Allocate(TaskRequest{TaskID: "t", Replicas: 2, MemoryPerReplicaMB: 8192})
		if d.Place == nil {
			t.Fatalf("应成功放置，实际拒绝: %+v", d.Reject)
		}
		if d.Place.CrossNUMAPairs != 0 || d.Place.SameNUMAPairs != 1 {
			t.Errorf("应全部落在同一 NUMA: %+v", d.Place)
		}
		if d.Place.Cost != 1 {
			t.Errorf("同 NUMA 单对代价应为 1，实际 %d", d.Place.Cost)
		}
		got := []string{d.Place.Assignments[0].DeviceID, d.Place.Assignments[1].DeviceID}
		if got[0] != "gpu0" || got[1] != "gpu1" {
			t.Errorf("确定性裁决应选字典序最小的 {gpu0,gpu1}，实际 %v", got)
		}
	})

	t.Run("显式NVLink跨NUMA链路被优先选中", func(t *testing.T) {
		spec := base
		spec.Links = []topology.Link{{A: "gpu0", B: "gpu3", Cost: 0}}
		s := New()
		if err := s.Configure(spec); err != nil {
			t.Fatal(err)
		}
		d := s.Allocate(TaskRequest{TaskID: "t", Replicas: 2, MemoryPerReplicaMB: 8192})
		if d.Place == nil {
			t.Fatalf("应成功放置，实际拒绝: %+v", d.Reject)
		}
		if d.Place.Cost != 0 || d.Place.CrossNUMAPairs != 1 {
			t.Errorf("应选择 0 代价的跨 NUMA NVLink 对: %+v", d.Place)
		}
		got := map[string]bool{}
		for _, a := range d.Place.Assignments {
			got[a.DeviceID] = true
		}
		if !got["gpu0"] || !got["gpu3"] {
			t.Errorf("应选 {gpu0,gpu3}，实际 %v", got)
		}
	})
}

// TestFragmentedMemory 显存碎片：总量够、卡数够，但单卡放不下。
func TestFragmentedMemory(t *testing.T) {
	s := New()
	err := s.Configure(topology.Spec{
		Devices: []topology.Device{
			{ID: "gpu0", MemoryMB: 24576, NUMANode: 0},
			{ID: "gpu1", MemoryMB: 24576, NUMANode: 0},
		},
	})
	if err != nil {
		t.Fatal(err)
	}

	// t1 占走 gpu0 的 12GB（确定性裁决选 gpu0），gpu0 剩 12GB。
	d1 := s.Allocate(TaskRequest{TaskID: "t1", Replicas: 1, MemoryPerReplicaMB: 12288})
	if d1.Place == nil {
		t.Fatalf("t1 应成功: %+v", d1.Reject)
	}
	if d1.Place.Assignments[0].DeviceID != "gpu0" {
		t.Fatalf("t1 应落在 gpu0，实际 %s", d1.Place.Assignments[0].DeviceID)
	}

	// t2 需要 2×16GB：总剩余 12+24=36GB >= 32GB，设备数 2 >= 2，
	// 但单卡 >= 16GB 的只有 gpu1 → FRAGMENTED_MEMORY。
	d2 := s.Allocate(TaskRequest{TaskID: "t2", Replicas: 2, MemoryPerReplicaMB: 16384})
	if d2.Reject == nil {
		t.Fatalf("t2 应被拒绝，实际成功: %+v", d2.Place)
	}
	if d2.Reject.Reason != ReasonFragmentedMemory {
		t.Fatalf("拒绝原因应为 FRAGMENTED_MEMORY，实际 %s", d2.Reject.Reason)
	}
	ctx := d2.Reject.Context
	if ctx.EligibleDevices != 1 || ctx.TotalFreeMemoryMB != 36864 || ctx.RequiredTotalMemoryMB != 32768 {
		t.Errorf("拒绝上下文数据不符: %+v", ctx)
	}

	// 释放 t1 后碎片消失，t2 应能放下。
	if !s.Release("t1") {
		t.Fatal("释放 t1 失败")
	}
	d3 := s.Allocate(TaskRequest{TaskID: "t2", Replicas: 2, MemoryPerReplicaMB: 16384})
	if d3.Place == nil {
		t.Fatalf("释放后 t2 应成功: %+v", d3.Reject)
	}
}

// TestInfeasibleTasks 无解任务的拒绝原因。
func TestInfeasibleTasks(t *testing.T) {
	s := New()
	if err := s.Configure(topology.Spec{
		Devices: []topology.Device{
			{ID: "gpu0", MemoryMB: 8192, NUMANode: 0},
			{ID: "gpu1", MemoryMB: 8192, NUMANode: 0},
		},
	}); err != nil {
		t.Fatal(err)
	}

	cases := []struct {
		name string
		req  TaskRequest
		want RejectReason
	}{
		{"卡数超过集群规模", TaskRequest{TaskID: "a", Replicas: 3, MemoryPerReplicaMB: 1024}, ReasonNotEnoughDevices},
		{"总显存不足", TaskRequest{TaskID: "b", Replicas: 2, MemoryPerReplicaMB: 16384}, ReasonInsufficientMemory},
		{"非法参数", TaskRequest{TaskID: "c", Replicas: 0, MemoryPerReplicaMB: 1024}, ReasonInvalidTask},
		{"空任务名", TaskRequest{TaskID: "", Replicas: 1, MemoryPerReplicaMB: 1024}, ReasonInvalidTask},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := s.Allocate(tc.req)
			if d.Reject == nil || d.Reject.Reason != tc.want {
				t.Fatalf("期望拒绝 %s，实际 %+v", tc.want, d)
			}
		})
	}

	// 重名任务。
	d := s.Allocate(TaskRequest{TaskID: "dup", Replicas: 1, MemoryPerReplicaMB: 1024})
	if d.Place == nil {
		t.Fatalf("首次分配应成功: %+v", d.Reject)
	}
	d = s.Allocate(TaskRequest{TaskID: "dup", Replicas: 1, MemoryPerReplicaMB: 1024})
	if d.Reject == nil || d.Reject.Reason != ReasonDuplicateTask {
		t.Fatalf("重名应拒绝 DUPLICATE_TASK，实际 %+v", d)
	}
}

// TestHeuristicNeverWorseThanOptimal 启发式路径（强制小阈值触发）
// 的结果代价必须 >= 穷举最优（最优是下界），且在小规模上应经常相等。
func TestHeuristicNeverWorseThanOptimal(t *testing.T) {
	old := ExhaustiveCandidateLimit
	ExhaustiveCandidateLimit = 1 // 强制走启发式
	defer func() { ExhaustiveCandidateLimit = old }()

	rng := rand.New(rand.NewSource(7))
	equal, total := 0, 0
	const trials = 60
	for trial := 0; trial < trials; trial++ {
		n := 3 + rng.Intn(5)
		spec := topology.Spec{
			DefaultSameNUMACost:  1,
			DefaultCrossNUMACost: 4 + rng.Intn(10),
		}
		for i := 0; i < n; i++ {
			spec.Devices = append(spec.Devices, topology.Device{
				ID:       "d" + string(rune('a'+i)),
				MemoryMB: 8192,
				NUMANode: rng.Intn(2),
			})
		}
		cl := mustCluster(t, spec)
		eligible := allIndices(cl)
		for k := 1; k <= n; k++ {
			total++
			got := solve(cl, eligible, k)
			// k=n 时只有唯一候选 C(n,n)=1，仍走穷举属正常。
			if got.exhaustive && k != n {
				t.Fatalf("阈值为 1 且 k<n 时不应走穷举 (n=%d k=%d)", n, k)
			}
			_, optCost, _ := bruteForce(cl, eligible, k)
			if got.cost < optCost {
				t.Fatalf("启发式代价 %d 低于穷举最优 %d，说明穷举实现有误", got.cost, optCost)
			}
			if got.cost == optCost {
				equal++
			}
		}
	}
	t.Logf("启发式达到最优的比例: %d/%d 个子问题", equal, total)
}

func equalSet(cl *topology.Cluster, a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if cl.DeviceID(a[i]) != cl.DeviceID(b[i]) {
			return false
		}
	}
	return true
}

func idsOf(cl *topology.Cluster, idx []int) []string {
	out := make([]string, len(idx))
	for i, x := range idx {
		out[i] = cl.DeviceID(x)
	}
	return out
}

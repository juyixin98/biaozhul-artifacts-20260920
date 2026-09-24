package solver

import (
	"math/rand"
	"strconv"
	"strings"
	"testing"

	"topology-aware-gpu-scheduler/internal/topology"
)

// ---- helpers ---------------------------------------------------------------

func dev(id string, mem, used, numa int64) topology.Device {
	return topology.Device{ID: id, MemoryMB: mem, UsedMemory: used, NumaNode: int(numa)}
}

func link(a, b string, cost int64) topology.Link {
	return topology.Link{A: a, B: b, Cost: cost}
}

func cluster8GPUs() *topology.Cluster {
	// 8 devices, two NUMA nodes, all 80 GB free.
	devs := []topology.Device{
		dev("gpu0", 80_000, 0, 0),
		dev("gpu1", 80_000, 0, 0),
		dev("gpu2", 80_000, 0, 0),
		dev("gpu3", 80_000, 0, 0),
		dev("gpu4", 80_000, 0, 1),
		dev("gpu5", 80_000, 0, 1),
		dev("gpu6", 80_000, 0, 1),
		dev("gpu7", 80_000, 0, 1),
	}
	return &topology.Cluster{
		Devices:          devs,
		DefaultSameNuma:  1,
		DefaultCrossNuma: 10,
	}
}

// referenceSolve is an INDEPENDENT brute-force implementation used only by the
// tests: it enumerates every k-subset of eligible devices from the domain
// model (not sharing any code with the production enumerator) and returns the
// lexicographically smallest minimum-cost subset and its cost.
func referenceSolve(t *testing.T, c *topology.Cluster, replicas int, mem int64) ([]string, int64) {
	t.Helper()
	order, _, matrix, err := c.BuildCostMatrix()
	if err != nil {
		t.Fatalf("topology error: %v", err)
	}
	var eligible []int
	for i, id := range order {
		d, _ := c.DeviceAt(id)
		if d.FreeMemory() >= mem {
			eligible = append(eligible, i)
		}
	}
	if len(eligible) < replicas {
		return nil, -1
	}
	bestCost := int64(-1)
	var bestSet []int
	var combo []int
	var rec func(start int)
	rec = func(start int) {
		if len(combo) == replicas {
			cost := int64(0)
			for a := 0; a < len(combo); a++ {
				for b := a + 1; b < len(combo); b++ {
					cost += matrix[eligible[combo[a]]][eligible[combo[b]]]
				}
			}
			cp := append([]int(nil), combo...)
			if bestCost == -1 || cost < bestCost {
				bestCost = cost
				bestSet = cp
			}
			return
		}
		for i := start; i < len(eligible); i++ {
			combo = append(combo, i)
			rec(i + 1)
			combo = combo[:len(combo)-1]
		}
	}
	rec(0)
	ids := make([]string, len(bestSet))
	for i, pi := range bestSet {
		ids[i] = order[eligible[pi]]
	}
	return ids, bestCost
}

func placementIDs(r *Result) []string {
	ids := make([]string, len(r.Placement))
	for i, p := range r.Placement {
		ids[i] = p.DeviceID
	}
	return ids
}

// ---- acceptance cases ------------------------------------------------------

func TestOptimalPrefersSingleNuma(t *testing.T) {
	c := cluster8GPUs()
	res, err := Solve(c, Request{Replicas: 4, MemoryPerReplica: 20_000})
	if err != nil {
		t.Fatal(err)
	}
	if res.Status != StatusOptimal {
		t.Fatalf("status = %s, want optimal (%s)", res.Status, res.Message)
	}
	// 4 replicas within one NUMA node => all 6 pairs same-NUMA @1 => cost 6.
	if res.TotalCost != 6 {
		t.Errorf("total cost = %d, want 6", res.TotalCost)
	}
	if res.CrossNumaPairs != 0 {
		t.Errorf("cross-numa pairs = %d, want 0", res.CrossNumaPairs)
	}
	want := []string{"gpu0", "gpu1", "gpu2", "gpu3"}
	got := placementIDs(res)
	if len(got) != 4 {
		t.Fatalf("placement size = %d", len(got))
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("placement[%d] = %s, want %s (lexicographic tie-break)", i, got[i], want[i])
		}
	}
	// Cross-check with the independent brute-force reference.
	refIDs, refCost := referenceSolve(t, c, 4, 20_000)
	if refCost != res.TotalCost {
		t.Errorf("reference cost = %d, solver cost = %d", refCost, res.TotalCost)
	}
	for i := range refIDs {
		if refIDs[i] != got[i] {
			t.Errorf("reference device = %s, solver = %s", refIDs[i], got[i])
		}
	}
}

func TestMemoryFragmentation(t *testing.T) {
	// Every device has free memory, but none fits the 25 GB replica, and the
	// aggregate free memory is more than enough: classic fragmentation.
	c := &topology.Cluster{
		Devices: []topology.Device{
			dev("gpu0", 40_000, 30_000, 0), // 10 GB free
			dev("gpu1", 40_000, 20_000, 0), // 20 GB free
			dev("gpu2", 40_000, 25_000, 0), // 15 GB free
		},
		DefaultSameNuma:  1,
		DefaultCrossNuma: 10,
	}
	res, err := Solve(c, Request{Replicas: 2, MemoryPerReplica: 25_000})
	if err != nil {
		t.Fatal(err)
	}
	if res.Status != StatusInfeasible || res.Reason != ReasonMemoryTooLarge {
		t.Fatalf("status=%s reason=%s, want infeasible/%s", res.Status, res.Reason, ReasonMemoryTooLarge)
	}
	if res.LargestFreeMB != 20_000 {
		t.Errorf("largest free = %d, want 20000", res.LargestFreeMB)
	}
	if res.EligibleCount != 0 {
		t.Errorf("eligible count = %d, want 0", res.EligibleCount)
	}
	if !strings.Contains(res.Message, "25000") {
		t.Errorf("message should mention required memory 25000: %q", res.Message)
	}
}

func TestInsufficientEligibleDevices(t *testing.T) {
	c := cluster8GPUs()
	// Only gpu0 and gpu1 have a full 80 GB free block; the rest are fragmented.
	c.Devices[2].UsedMemory = 70_000
	c.Devices[3].UsedMemory = 70_000
	c.Devices[4].UsedMemory = 70_000
	c.Devices[5].UsedMemory = 70_000
	c.Devices[6].UsedMemory = 70_000
	c.Devices[7].UsedMemory = 70_000
	res, err := Solve(c, Request{Replicas: 4, MemoryPerReplica: 20_000})
	if err != nil {
		t.Fatal(err)
	}
	if res.Status != StatusInfeasible || res.Reason != ReasonInsufficientDevices {
		t.Fatalf("status=%s reason=%s, want infeasible/%s", res.Status, res.Reason, ReasonInsufficientDevices)
	}
	if res.EligibleCount != 2 {
		t.Errorf("eligible = %d, want 2", res.EligibleCount)
	}
}

func TestCrossNumaPenaltyBreaksTie(t *testing.T) {
	// 4 devices, 2 NUMA nodes; the cheap same-NUMA pairs exist in both nodes.
	// For 2 replicas the optimum is any same-NUMA pair (cost 1 vs 10),
	// lexicographically gpu0+gpu1.
	c := &topology.Cluster{
		Devices: []topology.Device{
			dev("gpu0", 80_000, 0, 0),
			dev("gpu1", 80_000, 0, 0),
			dev("gpu2", 80_000, 0, 1),
			dev("gpu3", 80_000, 0, 1),
		},
		DefaultSameNuma:  1,
		DefaultCrossNuma: 10,
	}
	res, err := Solve(c, Request{Replicas: 2, MemoryPerReplica: 20_000})
	if err != nil {
		t.Fatal(err)
	}
	if res.TotalCost != 1 || res.CrossNumaPairs != 0 {
		t.Errorf("cost=%d cross=%d, want 1/0", res.TotalCost, res.CrossNumaPairs)
	}
	got := placementIDs(res)
	if got[0] != "gpu0" || got[1] != "gpu1" {
		t.Errorf("placement = %v, want [gpu0 gpu1]", got)
	}
}

func TestExplicitLinksOverrideNumaDefaults(t *testing.T) {
	c := cluster8GPUs()
	// Declare the cross-NUMA pair gpu0<->gpu4 cheaper than the same-NUMA
	// default; a 2-replica task must then pick that explicit cheap edge.
	c.Links = []topology.Link{link("gpu0", "gpu4", 0)}
	res, err := Solve(c, Request{Replicas: 2, MemoryPerReplica: 20_000})
	if err != nil {
		t.Fatal(err)
	}
	if res.TotalCost != 0 {
		t.Errorf("cost = %d, want 0 from explicit link", res.TotalCost)
	}
	got := placementIDs(res)
	if got[0] != "gpu0" || got[1] != "gpu4" {
		t.Errorf("placement = %v, want [gpu0 gpu4]", got)
	}
	if res.Pairs[0].CrossNuma != true {
		t.Errorf("pair should be flagged cross-NUMA despite cost 0")
	}
}

func TestSingleReplica(t *testing.T) {
	c := cluster8GPUs()
	res, err := Solve(c, Request{Replicas: 1, MemoryPerReplica: 20_000})
	if err != nil {
		t.Fatal(err)
	}
	if res.Status != StatusOptimal || res.TotalCost != 0 || res.Strategy != "trivial-single-device" {
		t.Fatalf("status=%s cost=%d strategy=%s", res.Status, res.TotalCost, res.Strategy)
	}
	if placementIDs(res)[0] != "gpu0" {
		t.Errorf("single replica should land on smallest ID, got %v", placementIDs(res))
	}
}

func TestInvalidRequests(t *testing.T) {
	c := cluster8GPUs()
	if _, err := Solve(c, Request{Replicas: 0, MemoryPerReplica: 1}); err == nil {
		t.Error("replicas=0 should error")
	}
	if _, err := Solve(c, Request{Replicas: 2, MemoryPerReplica: -1}); err == nil {
		t.Error("negative memory should error")
	}
	bad := &topology.Cluster{Devices: nil}
	if _, err := Solve(bad, Request{Replicas: 1, MemoryPerReplica: 1}); err == nil {
		t.Error("empty cluster should error")
	}
}

// ---- brute-force vs branch-and-bound cross-validation ----------------------

// randomCluster builds a random topology with per-device free memory and a
// fully random cost matrix expressed through explicit links.
func randomCluster(rng *rand.Rand, n int) *topology.Cluster {
	devs := make([]topology.Device, n)
	for i := 0; i < n; i++ {
		used := rng.Int63n(60_000)
		devs[i] = topology.Device{
			ID:       "g" + pad(i),
			MemoryMB: 80_000,
			// Free blocks are deliberately irregular to exercise fragmentation.
			UsedMemory: used,
			NumaNode:   i % 3,
		}
	}
	var links []topology.Link
	for i := 0; i < n; i++ {
		for j := i + 1; j < n; j++ {
			links = append(links, topology.Link{
				A: devs[i].ID, B: devs[j].ID,
				Cost: rng.Int63n(20),
			})
		}
	}
	return &topology.Cluster{
		Devices:          devs,
		Links:            links,
		DefaultSameNuma:  1,
		DefaultCrossNuma: 10,
	}
}

func pad(i int) string {
	if i < 10 {
		return "0" + strconv.Itoa(i)
	}
	return strconv.Itoa(i)
}

func TestBranchAndBoundMatchesExhaustive(t *testing.T) {
	rng := rand.New(rand.NewSource(42))
	for iter := 0; iter < 200; iter++ {
		n := 6 + rng.Intn(9) // 6..14 devices
		c := randomCluster(rng, n)
		k := 1 + rng.Intn(5) // 1..5 replicas
		mem := int64(1 + rng.Intn(40_000))

		// Exhaustive path.
		ex, err := Solve(c, Request{Replicas: k, MemoryPerReplica: mem})
		if err != nil {
			t.Fatalf("iter %d: %v", iter, err)
		}
		// Force the branch-and-bound path by disabling enumeration.
		bb, err := Solve(c, Request{
			Replicas:         k,
			MemoryPerReplica: mem,
			ExhaustiveLimit:  0,
			MaxSearchStates:  5_000_000,
		})
		if err != nil {
			t.Fatalf("iter %d: %v", iter, err)
		}

		if ex.Status == StatusInfeasible {
			if bb.Status != StatusInfeasible {
				t.Fatalf("iter %d: exhaustive says infeasible, bb says %s", iter, bb.Status)
			}
			continue
		}
		if bb.Status != StatusOptimal {
			t.Fatalf("iter %d: bb status = %s (%s)", iter, bb.Status, bb.Message)
		}
		if ex.TotalCost != bb.TotalCost {
			t.Fatalf("iter %d (n=%d k=%d mem=%d): exhaustive cost %d != bb cost %d",
				iter, n, k, mem, ex.TotalCost, bb.TotalCost)
		}
		// Lexicographic tie-break must agree too.
		exIDs, bbIDs := placementIDs(ex), placementIDs(bb)
		for i := range exIDs {
			if exIDs[i] != bbIDs[i] {
				t.Fatalf("iter %d: exhaustive %v != bb %v (equal cost %d)",
					iter, exIDs, bbIDs, ex.TotalCost)
			}
		}
		// And match the independent reference enumerator.
		refIDs, refCost := referenceSolve(t, c, k, mem)
		if refCost != ex.TotalCost {
			t.Fatalf("iter %d: reference cost %d != solver cost %d", iter, refCost, ex.TotalCost)
		}
		for i := range refIDs {
			if refIDs[i] != exIDs[i] {
				t.Fatalf("iter %d: reference %v != solver %v", iter, refIDs, exIDs)
			}
		}
	}
}

func TestBudgetExhaustionReturnsBestEffort(t *testing.T) {
	// Random asymmetric costs on 30 devices make the greedy incumbent weak,
	// so a tiny node-visit budget is exhausted before optimality is proven.
	rng := rand.New(rand.NewSource(7))
	c := randomCluster(rng, 30)
	// Make all devices eligible so the search space is large (mem=1).
	for i := range c.Devices {
		c.Devices[i].UsedMemory = 0
		c.Devices[i].MemoryMB = 80_000
	}
	const budget int64 = 500
	res, err := Solve(c, Request{
		Replicas:         8,
		MemoryPerReplica: 1,
		ExhaustiveLimit:  1, // force branch-and-bound
		MaxSearchStates:  budget,
	})
	if err != nil {
		t.Fatal(err)
	}
	if res.Status != StatusBestEffort || res.Reason != ReasonSearchBudgetExceeded {
		t.Fatalf("status=%s reason=%s, want best_effort/%s", res.Status, res.Reason, ReasonSearchBudgetExceeded)
	}
	if len(res.Placement) != 8 {
		t.Fatalf("best-effort must still return a feasible placement of 8, got %d", len(res.Placement))
	}
	if res.StatesExplored > budget {
		t.Errorf("states explored = %d exceeds budget %d", res.StatesExplored, budget)
	}
	// The best-effort result must not be worse than the proven optimum
	// obtained with an unlimited budget on the same instance.
	opt, err := Solve(c, Request{
		Replicas:         8,
		MemoryPerReplica: 1,
		ExhaustiveLimit:  1,
		MaxSearchStates:  100_000_000,
	})
	if err != nil {
		t.Fatal(err)
	}
	if opt.Status != StatusOptimal {
		t.Fatalf("control run status = %s, want optimal", opt.Status)
	}
	if res.TotalCost < opt.TotalCost {
		t.Errorf("best-effort cost %d is below proven optimum %d", res.TotalCost, opt.TotalCost)
	}
}

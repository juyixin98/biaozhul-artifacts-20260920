// Package solver finds a cost-minimal set of devices for one GPU task subject
// to hard constraints:
//
//   - each replica is placed on a distinct device (at most one replica of the
//     task per device);
//   - the device's free memory must be at least the task's per-replica memory
//     request (no over-commitment, no splitting a replica across devices);
//   - exactly Request.Replicas devices must be selected.
//
// Among every feasible selection it minimizes the task-internal
// communication cost: the sum of interconnect costs over all pairs of
// selected devices (the all-reduce / all-to-all group cost).
//
// Two exact strategies are provided:
//
//   - exhaustive enumeration of all C(n,k) combinations when that number is
//     small enough (default 500 000); this is the reference used by the
//     acceptance tests;
//   - depth-first branch-and-bound for larger pools, with a bounded search
//     budget. When the budget is exhausted the best solution found so far is
//     returned marked non-optimal (status "best_effort"); it still satisfies
//     every hard constraint.
package solver

import (
	"fmt"

	"topology-aware-gpu-scheduler/internal/topology"
)

// Rejection / status reason codes returned to clients.
const (
	// StatusOptimal: a feasible placement was found and proven cost-minimal.
	StatusOptimal = "optimal"
	// StatusBestEffort: feasible placement found, but optimality unproven
	// because the search budget was exhausted.
	StatusBestEffort = "best_effort"
	// StatusInfeasible: no feasible placement exists.
	StatusInfeasible = "infeasible"

	// ReasonMemoryTooLarge: no single device has enough free memory for one
	// replica (fragmentation / oversized request).
	ReasonMemoryTooLarge = "MEMORY_TOO_LARGE"
	// ReasonInsufficientDevices: at least one device fits, but fewer than
	// Replicas such devices exist.
	ReasonInsufficientDevices = "INSUFFICIENT_ELIGIBLE_DEVICES"
	// ReasonSearchBudgetExceeded is a non-fatal note attached to best-effort
	// results.
	ReasonSearchBudgetExceeded = "SEARCH_BUDGET_EXCEEDED"
)

// defaultExhaustiveLimit enumerates combinations exhaustively while C(n,k)
// does not exceed this value. 500 000 combinations take well under a second.
const defaultExhaustiveLimit = 500_000

// defaultSearchStates bounds branch-and-bound node visits.
const defaultSearchStates = 2_000_000

// Request describes one task placement request.
type Request struct {
	Name             string
	Replicas         int
	MemoryPerReplica int64
	MaxSearchStates  int64 // <= 0 means defaultSearchStates
	ExhaustiveLimit  int   // <= 0 means defaultExhaustiveLimit
}

// Placement assigns one task replica to one device.
type Placement struct {
	ReplicaIndex int    `json:"replica_index"`
	DeviceID     string `json:"device_id"`
	NumaNode     int    `json:"numa_node"`
}

// Pair reports the cost between two devices of the placement group.
type Pair struct {
	From      string `json:"from"`
	To        string `json:"to"`
	Cost      int64  `json:"cost"`
	CrossNuma bool   `json:"cross_numa"`
}

// DeviceFree is a per-device free-memory diagnostic returned with the result.
type DeviceFree struct {
	DeviceID string `json:"device_id"`
	NumaNode int    `json:"numa_node"`
	FreeMB   int64  `json:"free_mb"`
	Eligible bool   `json:"eligible"`
}

// Result is the solver outcome.
type Result struct {
	// Status is one of StatusOptimal / StatusBestEffort / StatusInfeasible.
	Status string
	// Reason is the machine-readable rejection code (only when infeasible)
	// or ReasonSearchBudgetExceeded for best-effort results.
	Reason string
	// Message is a human-readable explanation.
	Message string

	// Replicas and MemoryPerReplica echo the request.
	Replicas         int   `json:"replicas"`
	MemoryPerReplica int64 `json:"memory_per_replica_mb"`

	// Solution.
	Placement      []Placement `json:"placement"`
	Pairs          []Pair      `json:"pairs"`
	TotalCost      int64       `json:"total_communication_cost"`
	CrossNumaPairs int         `json:"cross_numa_pair_count"`

	// Diagnostics.
	Strategy       string       `json:"strategy"`
	StatesExplored int64        `json:"states_explored"`
	Combinations   int64        `json:"combinations_total"`
	TotalDevices   int          `json:"total_devices"`
	EligibleCount  int          `json:"eligible_devices"`
	LargestFreeMB  int64        `json:"largest_free_mb"`
	FreeMemory     []DeviceFree `json:"free_memory"`
}

// Solve computes the placement for req on cluster c.
//
// A non-nil error indicates an invalid topology/request (client error); an
// infeasible-but-valid request yields a Result with Status == StatusInfeasible
// and a nil error.
func Solve(c *topology.Cluster, req Request) (*Result, error) {
	if req.Replicas < 1 {
		return nil, fmt.Errorf("replicas must be >= 1, got %d", req.Replicas)
	}
	if req.MemoryPerReplica < 0 {
		return nil, fmt.Errorf("memory_per_replica_mb must be >= 0")
	}

	order, _, matrix, err := c.BuildCostMatrix()
	if err != nil {
		return nil, err
	}

	res := &Result{
		Status:           StatusInfeasible,
		Replicas:         req.Replicas,
		MemoryPerReplica: req.MemoryPerReplica,
		TotalDevices:     len(order),
		FreeMemory:       make([]DeviceFree, 0, len(order)),
	}

	// Eligibility under the memory hard constraint.
	eligible := make([]int, 0, len(order))
	var largestFree int64
	for _, id := range order {
		d, _ := c.DeviceAt(id)
		free := d.FreeMemory()
		if free > largestFree {
			largestFree = free
		}
		ok := free >= req.MemoryPerReplica
		res.FreeMemory = append(res.FreeMemory, DeviceFree{
			DeviceID: id, NumaNode: d.NumaNode, FreeMB: free, Eligible: ok,
		})
		if ok {
			eligible = append(eligible, indexOf(order, id))
		}
	}
	res.LargestFreeMB = largestFree
	res.EligibleCount = len(eligible)

	// Hard-constraint rejection 1: memory fragmentation / oversized request.
	if largestFree < req.MemoryPerReplica {
		res.Reason = ReasonMemoryTooLarge
		res.Message = fmt.Sprintf(
			"no device has %d MB free memory; largest contiguous free block is %d MB (a replica cannot be split across devices)",
			req.MemoryPerReplica, largestFree)
		return res, nil
	}

	// Hard-constraint rejection 2: not enough eligible devices.
	if len(eligible) < req.Replicas {
		res.Reason = ReasonInsufficientDevices
		res.Message = fmt.Sprintf(
			"task needs %d devices with >= %d MB free, but only %d of %d devices qualify",
			req.Replicas, req.MemoryPerReplica, len(eligible), len(order))
		return res, nil
	}

	k := req.Replicas
	n := len(eligible)

	// Build the restricted pool matrix (pool indices follow ascending IDs).
	pool := make([][]int64, n)
	for i := range pool {
		pool[i] = make([]int64, n)
		for j := range pool {
			pool[i][j] = matrix[eligible[i]][eligible[j]]
		}
	}

	// k == 1: no group-internal communication; the smallest ID is the
	// lexicographic optimum.
	if k == 1 {
		res.Status = StatusOptimal
		res.Strategy = "trivial-single-device"
		res.StatesExplored = 1
		res.Combinations = int64(n)
		chosen := []int{0}
		fillSolution(res, c, order, eligible, chosen, pool)
		return res, nil
	}

	limit := req.ExhaustiveLimit
	if limit <= 0 {
		limit = defaultExhaustiveLimit
	}
	total := binomial(n, k, float64(limit)+1)
	// Diagnostic only: cap at 2^53 so the float->int64 conversion stays exact.
	res.Combinations = binomial(n, k, float64(1<<53))

	if total <= int64(limit) {
		chosen := exhaustive(pool, k, res)
		res.Strategy = "exhaustive-enumeration"
		res.Status = StatusOptimal
		fillSolution(res, c, order, eligible, chosen, pool)
		return res, nil
	}

	// Branch-and-bound for large pools.
	budget := req.MaxSearchStates
	if budget <= 0 {
		budget = defaultSearchStates
	}
	chosen, optimal, states := branchAndBound(pool, k, budget)
	res.Strategy = "branch-and-bound"
	res.StatesExplored = states
	if optimal {
		res.Status = StatusOptimal
	} else {
		res.Status = StatusBestEffort
		res.Reason = ReasonSearchBudgetExceeded
		res.Message = fmt.Sprintf("search budget of %d node visits exhausted before optimality was proven; the returned placement is feasible but not proven optimal", budget)
	}
	fillSolution(res, c, order, eligible, chosen, pool)
	return res, nil
}

// fillSolution populates the placement, pair list and cost fields of a result
// from chosen pool indices.
func fillSolution(res *Result, c *topology.Cluster, order []string, eligible, chosen []int, pool [][]int64) {
	k := len(chosen)
	res.Placement = make([]Placement, 0, k)
	for r, pi := range chosen {
		gi := eligible[pi]
		d, _ := c.DeviceAt(order[gi])
		res.Placement = append(res.Placement, Placement{
			ReplicaIndex: r,
			DeviceID:     d.ID,
			NumaNode:     d.NumaNode,
		})
	}
	res.Pairs = make([]Pair, 0, k*(k-1)/2)
	var total int64
	cross := 0
	for a := 0; a < k; a++ {
		da, _ := c.DeviceAt(res.Placement[a].DeviceID)
		for b := a + 1; b < k; b++ {
			db, _ := c.DeviceAt(res.Placement[b].DeviceID)
			cost := pool[chosen[a]][chosen[b]]
			isCross := da.NumaNode != db.NumaNode
			if isCross {
				cross++
			}
			total += cost
			res.Pairs = append(res.Pairs, Pair{
				From: da.ID, To: db.ID, Cost: cost, CrossNuma: isCross,
			})
		}
	}
	res.TotalCost = total
	res.CrossNumaPairs = cross
}

func indexOf(order []string, id string) int {
	for i, v := range order {
		if v == id {
			return i
		}
	}
	return -1
}

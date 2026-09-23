// Package ring implements a weighted consistent-hash ring with virtual nodes.
//
// A node with weight w owns vnodes*w virtual nodes placed on a 64-bit FNV-1a
// circle. Ownership of a key is the first vnode clockwise from (or on) the
// key's hash. All operations are pure and deterministic.
package ring

import (
	"fmt"
	"hash/fnv"
	"sort"
)

// Node describes one physical node and its relative weight.
type Node struct {
	ID     string `json:"id"`
	Weight int    `json:"weight"`
}

type point struct {
	hash   uint64
	nodeID string
}

// Ring is an immutable-ish set of vnodes. Rebuild one with Build when the
// membership changes; the simulator keeps the old and new ring side by side
// during a migration.
type Ring struct {
	VnodesPerNode int
	nodes         map[string]int // id -> effective weight
	points        []point        // sorted by hash
}

// fmix64 is MurmurHash3's 64-bit finalizer. FNV-1a avalanches poorly for
// short, suffix-similar strings ("n1#0".."n1#127" all collide into one narrow
// band), so the raw FNV value is unusable as a ring position; this fixed
// bijection spreads it uniformly. It is applied identically to vnodes and
// keys, so ring lookups stay consistent.
func fmix64(x uint64) uint64 {
	x ^= x >> 33
	x *= 0xff51afd7ed558ccd
	x ^= x >> 33
	x *= 0xc4ceb9fe1a85ec53
	x ^= x >> 33
	return x
}

// hashString returns the avalanched FNV-1a hash of s.
func hashString(s string) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(s))
	return fmix64(h.Sum64())
}

// HashKey returns the position of a key on the circle.
func HashKey(key string) uint64 { return hashString(key) }

func vnodeHash(nodeID string, i int) uint64 {
	return hashString(fmt.Sprintf("%s#%d", nodeID, i))
}

// Build constructs a ring. Non-positive weights are treated as 1.
func Build(nodes []Node, vnodesPerNode int) *Ring {
	r := &Ring{
		VnodesPerNode: vnodesPerNode,
		nodes:         make(map[string]int, len(nodes)),
	}
	for _, n := range nodes {
		w := n.Weight
		if w <= 0 {
			w = 1
		}
		r.nodes[n.ID] = w
		for i := 0; i < vnodesPerNode*w; i++ {
			r.points = append(r.points, point{hash: vnodeHash(n.ID, i), nodeID: n.ID})
		}
	}
	sort.Slice(r.points, func(i, j int) bool {
		if r.points[i].hash != r.points[j].hash {
			return r.points[i].hash < r.points[j].hash
		}
		return r.points[i].nodeID < r.points[j].nodeID
	})
	return r
}

// Empty reports whether the ring has any vnodes.
func (r *Ring) Empty() bool { return len(r.points) == 0 }

// NodeIDs returns the member IDs sorted by ID.
func (r *Ring) NodeIDs() []string {
	ids := make([]string, 0, len(r.nodes))
	for id := range r.nodes {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// Weight returns the effective weight of a node (0 if absent).
func (r *Ring) Weight(id string) int { return r.nodes[id] }

// OwnerOfHash returns the node responsible for a hash position.
func (r *Ring) OwnerOfHash(h uint64) string {
	if len(r.points) == 0 {
		return ""
	}
	i := sort.Search(len(r.points), func(i int) bool { return r.points[i].hash >= h })
	if i == len(r.points) {
		i = 0
	}
	return r.points[i].nodeID
}

// Owner returns the node responsible for a key.
func (r *Ring) Owner(key string) string { return r.OwnerOfHash(HashKey(key)) }

// VnodeCount exposes the number of points (mainly for diagnostics/tests).
func (r *Ring) VnodeCount() int { return len(r.points) }

// Task is one key-range transfer from Src to Dst. The interval is
// (Start, End]; Start > End denotes the interval wrapping around zero.
type Task struct {
	Start uint64 `json:"start"` // exclusive
	End   uint64 `json:"end"`   // inclusive
	Src   string `json:"src"`
	Dst   string `json:"dst"`
}

// ID identifies a task; it is stable across coordinator restarts because it is
// derived purely from the geometric interval and endpoints.
func (t Task) ID() string {
	return fmt.Sprintf("%016x-%016x-%s-%s", t.Start, t.End, t.Src, t.Dst)
}

// Contains reports whether hash h falls in the task's (Start, End] interval,
// honoring wrap-around when Start > End.
func (t Task) Contains(h uint64) bool {
	if t.Start <= t.End {
		return h > t.Start && h <= t.End
	}
	return h > t.Start || h <= t.End
}

// Plan computes the migration tasks needed to turn oldR into newR.
//
// It walks the merged set of vnode positions: for each maximal interval
// (p_i, p_{i+1}] between consecutive vnodes of either ring it compares the
// owner under the old and new ring and emits one task exactly when ownership
// changes. The final interval wraps around the circle.
func Plan(oldR, newR *Ring) []Task {
	if oldR == nil || oldR.Empty() {
		return nil
	}
	seen := make(map[uint64]bool)
	var hs []uint64
	for _, p := range oldR.points {
		if !seen[p.hash] {
			seen[p.hash] = true
			hs = append(hs, p.hash)
		}
	}
	for _, p := range newR.points {
		if !seen[p.hash] {
			seen[p.hash] = true
			hs = append(hs, p.hash)
		}
	}
	sort.Slice(hs, func(i, j int) bool { return hs[i] < hs[j] })

	var tasks []Task
	n := len(hs)
	for i := 0; i < n; i++ {
		start := hs[i]
		end := hs[(i+1)%n]
		oldOwner := oldR.OwnerOfHash(end)
		newOwner := newR.OwnerOfHash(end)
		if oldOwner != newOwner {
			tasks = append(tasks, Task{Start: start, End: end, Src: oldOwner, Dst: newOwner})
		}
	}
	return tasks
}

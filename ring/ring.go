// Package ring implements a weighted consistent-hash ring with virtual nodes.
//
// A Ring is an immutable view of a cluster topology. Topology changes (scale
// out / scale in) build a new Ring with an incremented Version. Two rings can
// be Diff-ed to find, for every key, whether ownership moves and to which node.
//
// Hashing uses FNV-1a 64 bit: no external dependency, deterministic across
// processes and platforms.
package ring

import (
	"fmt"
	"hash/fnv"
	"sort"
)

// NodeSpec declares one physical node and its relative weight.
type NodeSpec struct {
	ID     string `json:"id"`
	Weight int    `json:"weight"`
}

// vnode is one token on the circle.
type vnode struct {
	token uint64
	// index into Ring.nodes
	nodeIdx int
}

// Ring is an immutable consistent-hash topology.
type Ring struct {
	// Version is the topology epoch; 0 is the initial ring.
	Version int
	// vnodesPerWeight maps weight 1 to this many virtual tokens.
	vnodesPerWeight int
	nodes           []NodeSpec
	tokens          []vnode // sorted by token
}

func hashString(s string) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(s))
	// FNV-1a maps similar short strings into clustered regions of the circle;
	// a splitmix64 finalizer avalanches the bits while staying deterministic.
	x := h.Sum64()
	x ^= x >> 30
	x *= 0xbf58476d1ce4e5b9
	x ^= x >> 27
	x *= 0x94d049bb133111eb
	x ^= x >> 31
	return x
}

// New builds a ring from node specs. Nodes are sorted by ID so the same set of
// nodes always yields the same ring regardless of input order.
func New(version int, nodes []NodeSpec, vnodesPerWeight int) (*Ring, error) {
	if vnodesPerWeight <= 0 {
		return nil, fmt.Errorf("vnodesPerWeight must be positive, got %d", vnodesPerWeight)
	}
	if len(nodes) == 0 {
		return nil, fmt.Errorf("ring must contain at least one node")
	}
	ns := make([]NodeSpec, len(nodes))
	copy(ns, nodes)
	sort.Slice(ns, func(i, j int) bool { return ns[i].ID < ns[j].ID })
	seen := map[string]bool{}
	tokens := make([]vnode, 0)
	for idx, n := range ns {
		if n.ID == "" {
			return nil, fmt.Errorf("node %d has empty id", idx)
		}
		if seen[n.ID] {
			return nil, fmt.Errorf("duplicate node id %q", n.ID)
		}
		seen[n.ID] = true
		w := n.Weight
		if w <= 0 {
			return nil, fmt.Errorf("node %q weight must be positive, got %d", n.ID, w)
		}
		count := w * vnodesPerWeight
		for v := 0; v < count; v++ {
			tokens = append(tokens, vnode{
				token:   hashString(fmt.Sprintf("%s#%d", n.ID, v)),
				nodeIdx: idx,
			})
		}
	}
	// Sort tokens; on the (astronomically unlikely) FNV collision keep both but
	// order deterministically by node index so construction stays stable.
	sort.Slice(tokens, func(i, j int) bool {
		if tokens[i].token != tokens[j].token {
			return tokens[i].token < tokens[j].token
		}
		return tokens[i].nodeIdx < tokens[j].nodeIdx
	})
	return &Ring{
		Version:         version,
		vnodesPerWeight: vnodesPerWeight,
		nodes:           ns,
		tokens:          tokens,
	}, nil
}

// Nodes returns the node specs sorted by ID.
func (r *Ring) Nodes() []NodeSpec {
	out := make([]NodeSpec, len(r.nodes))
	copy(out, r.nodes)
	return out
}

// HasNode reports whether id belongs to this ring.
func (r *Ring) HasNode(id string) bool {
	for _, n := range r.nodes {
		if n.ID == id {
			return true
		}
	}
	return false
}

// Owner returns the node responsible for key on this ring.
func (r *Ring) Owner(key string) string {
	h := hashString(key)
	i := sort.Search(len(r.tokens), func(i int) bool {
		return r.tokens[i].token >= h
	})
	if i == len(r.tokens) {
		i = 0
	}
	return r.nodes[r.tokens[i].nodeIdx].ID
}

// KeyMovement describes where one key moves between two rings.
type KeyMovement struct {
	Key      string
	From     string // owner on the old ring
	To       string // owner on the new ring
	Migrates bool   // false when ownership is unchanged
}

// Diff computes ownership movement for keys between r (old) and next (new).
// Keys owned by a node absent from the new ring are reported with Migrates
// true and To set; such keys MUST be moved before the old node is removed.
func (r *Ring) Diff(next *Ring, keys []string) []KeyMovement {
	out := make([]KeyMovement, len(keys))
	for i, k := range keys {
		old := r.Owner(k)
		newOwner := next.Owner(k)
		out[i] = KeyMovement{Key: k, From: old, To: newOwner, Migrates: old != newOwner}
	}
	return out
}

// VNodes returns the total number of virtual tokens on the ring.
func (r *Ring) VNodes() int { return len(r.tokens) }

// Package main contains a deterministic weighted consistent hash ring with
// virtual nodes (ring.go), exact rebalancing plans (plan.go) and the HTTP
// front-end (server.go).
//
// Everything that influences routing is fixed and version-independent:
//
//   - Hash function: 64-bit FNV-1a (the same constants as hash/fnv) followed
//     by the fixed Murmur3 fmix64 avalanche finalizer. Raw FNV-1a clusters for
//     keys differing only near the end (e.g. virtual-node replica suffixes);
//     the bijection finalizer spreads those outputs. The key space is the
//     circle [0, 2^64-1].
//   - Virtual node count per physical node: Weight * BaseVNodes (integers).
//   - Virtual node key format: "<nodeID>#<zero-padded-replica-index>".
//   - Vnode ordering: (hash ascending, nodeID lexicographic, replica ascending),
//     so hash collisions have a deterministic total order.
//   - Ownership: key hash h belongs to the first vnode whose hash >= h,
//     wrapping around. Each vnode therefore owns the half-open arc
//     (predecessor.hash, vnode.hash] along the forward circle.
package main

import (
	"errors"
	"sort"
	"strconv"
	"strings"
)

// Space is the size of the hash circle: 2^64 (untyped constant, too large to
// fit in uint64; consumers that need it as an integer use math/big, e.g.
// new(big.Int).Lsh(big.NewInt(1), 64)).
const Space = 1 << 64

// DefaultBaseVNodes is the number of virtual nodes created per unit of weight.
const DefaultBaseVNodes = 64

// MaxNodeID bounds node identifier length in bytes; '#' is reserved because it
// separates node id from replica index in virtual node keys.
const MaxNodeID = 256

// Node is a physical member of the ring. Weight is a positive integer number
// of capacity units; every unit yields BaseVNodes virtual nodes.
type Node struct {
	ID     string `json:"id"`
	Weight int    `json:"weight"`
}

// VNode is one virtual node position on the ring.
type VNode struct {
	Hash    uint64 `json:"hash"`
	NodeID  string `json:"node_id"`
	Replica int    `json:"replica"`
}

// MarshalJSON emits the numeric hash plus a zero-padded 16-digit hex value so
// 64-bit positions stay readable for JSON consumers (e.g. JavaScript).
func (v VNode) MarshalJSON() ([]byte, error) {
	return []byte(`{"hash":` + strconv.FormatUint(v.Hash, 10) +
		`,"hash_hex":"0x` + toHex(v.Hash) +
		`","node_id":` + quote(v.NodeID) +
		`,"replica":` + strconv.Itoa(v.Replica) + `}`), nil
}

// Ring is an immutable snapshot of a consistent hash configuration.
type Ring struct {
	baseVNodes int
	nodes      []Node  // sorted by ID, deduplicated
	vnodes     []VNode // sorted by (Hash, NodeID, Replica)
}

// New builds a ring from the given nodes. Weight*baseVNodes must fit in an int
// for every node, node ids must be non-empty, contain no '#' and be unique.
// baseVNodes < 1 is replaced with DefaultBaseVNodes.
func New(nodes []Node, baseVNodes int) (*Ring, error) {
	if baseVNodes < 1 {
		baseVNodes = DefaultBaseVNodes
	}
	sorted := make([]Node, len(nodes))
	copy(sorted, nodes)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].ID < sorted[j].ID })

	seen := make(map[string]struct{}, len(sorted))
	total := 0
	for _, n := range sorted {
		if n.ID == "" {
			return nil, errors.New("node id must not be empty")
		}
		if len(n.ID) > MaxNodeID {
			return nil, errors.New("node id too long (max 256 bytes): " + n.ID)
		}
		if strings.Contains(n.ID, "#") {
			return nil, errors.New("node id must not contain '#': " + n.ID)
		}
		if n.Weight < 1 {
			return nil, errors.New("node weight must be >= 1: " + n.ID)
		}
		if _, dup := seen[n.ID]; dup {
			return nil, errors.New("duplicate node id: " + n.ID)
		}
		seen[n.ID] = struct{}{}
		count, overflow := mulCheck(n.Weight, baseVNodes)
		if overflow {
			return nil, errors.New("weight * baseVNodes overflows for node: " + n.ID)
		}
		if count < 1 {
			return nil, errors.New("node must own at least one virtual node: " + n.ID)
		}
		total += count
		if total < 0 {
			return nil, errors.New("total virtual node count overflows")
		}
	}

	vs := make([]VNode, 0, total)
	for _, n := range sorted {
		count := n.Weight * baseVNodes
		for r := 0; r < count; r++ {
			vs = append(vs, VNode{
				Hash:    HashVNode(n.ID, r),
				NodeID:  n.ID,
				Replica: r,
			})
		}
	}
	sortVNodes(vs)
	return &Ring{baseVNodes: baseVNodes, nodes: sorted, vnodes: vs}, nil
}

func mulCheck(a, b int) (int, bool) {
	p := a * b
	if a != 0 && p/a != b {
		return 0, true
	}
	return p, false
}

// BaseVNodes returns the per-weight-unit virtual node multiplier.
func (r *Ring) BaseVNodes() int { return r.baseVNodes }

// Nodes returns the physical nodes sorted by ID.
func (r *Ring) Nodes() []Node {
	out := make([]Node, len(r.nodes))
	copy(out, r.nodes)
	return out
}

// VNodes returns all virtual nodes in ring order. The slice must not be
// modified by the caller through mutation; callers treat it as read-only.
func (r *Ring) VNodes() []VNode {
	out := make([]VNode, len(r.vnodes))
	copy(out, r.vnodes)
	return out
}

// NodeWeight returns the weight of nodeID, or 0 if the node is absent.
func (r *Ring) NodeWeight(nodeID string) int {
	i := sort.Search(len(r.nodes), func(i int) bool { return r.nodes[i].ID >= nodeID })
	if i < len(r.nodes) && r.nodes[i].ID == nodeID {
		return r.nodes[i].Weight
	}
	return 0
}

// Empty reports whether the ring has no members.
func (r *Ring) Empty() bool { return len(r.vnodes) == 0 }

// Route returns the owning physical node id for key.
func (r *Ring) Route(key string) (string, error) {
	return r.RouteHash(HashKey(key))
}

// RouteHash returns the owning physical node id for a precomputed hash.
func (r *Ring) RouteHash(h uint64) (string, error) {
	if r.Empty() {
		return "", errors.New("ring has no nodes")
	}
	i := sort.Search(len(r.vnodes), func(i int) bool { return r.vnodes[i].Hash >= h })
	if i == len(r.vnodes) {
		i = 0
	}
	return r.vnodes[i].NodeID, nil
}

func sortVNodes(vs []VNode) {
	sort.Slice(vs, func(a, b int) bool {
		x, y := vs[a], vs[b]
		if x.Hash != y.Hash {
			return x.Hash < y.Hash
		}
		if x.NodeID != y.NodeID {
			return x.NodeID < y.NodeID
		}
		return x.Replica < y.Replica
	})
}

// ownerAt is RouteHash without the empty-ring error, used by the planner which
// handles empty rings explicitly.
func (r *Ring) ownerAt(h uint64) string {
	i := sort.Search(len(r.vnodes), func(i int) bool { return r.vnodes[i].Hash >= h })
	if i == len(r.vnodes) {
		i = 0
	}
	return r.vnodes[i].NodeID
}

// ---------- hashing (fixed FNV-1a, 64-bit) ----------

const (
	fnvOffset64 = 14695981039346656037
	fnvPrime64  = 1099511628211
)

// fnvSum computes FNV-1a 64 without allocating. It is bit-for-bit compatible
// with hash/fnv's New64a, which the test suite verifies.
func fnvSum(b []byte) uint64 {
	h := uint64(fnvOffset64)
	for _, c := range b {
		h ^= uint64(c)
		h *= fnvPrime64
	}
	return h
}

// HashKey returns the fixed hash of a routing key: FNV-1a 64 followed by the
// Murmur3 64-bit avalanche finalizer. The finalizer is essential: raw FNV-1a
// hashes of short strings that differ only near the end (exactly the shape of
// virtual-node keys "<id>#<replica>") cluster badly, which would wreck load
// balance. The composition is fixed, branchless and standard-library free.
func HashKey(key string) uint64 {
	return fmix64(fnvSum([]byte(key)))
}

// fmix64 is the MurmurHash3 64-bit avalanche finalizer (public-domain mixing
// constants by Apple/SMHasher). It is a bijection on uint64, so distinct FNV
// outputs remain distinct after mixing.
func fmix64(h uint64) uint64 {
	h ^= h >> 33
	h *= 0xff51afd7ed558ccd
	h ^= h >> 33
	h *= 0xc4ceb9fe1a85ec53
	h ^= h >> 33
	return h
}

// HashVNode returns the fixed hash of virtual node replica r of nodeID.
// The replica index is zero-padded to 8 decimal digits so ids like
// "a#1" / "a1" style collisions are impossible and ordering of the textual
// key is irrelevant anyway: hashing is the only thing that matters.
func HashVNode(nodeID string, replica int) uint64 {
	var buf [256 + 1 + 8]byte
	id := []byte(nodeID)
	n := copy(buf[:], id)
	buf[n] = '#'
	n++
	// fixed-width decimal suffix
	digits := []byte(strconv.Itoa(replica))
	start := n + (8 - len(digits))
	for i := n; i < start; i++ {
		buf[i] = '0'
	}
	copy(buf[start:], digits)
	n += 8
	return fmix64(fnvSum(buf[:n]))
}

func toHex(h uint64) string {
	const hexd = "0123456789abcdef"
	var b [16]byte
	for i := 15; i >= 0; i-- {
		b[i] = hexd[h&0xf]
		h >>= 4
	}
	return string(b[:])
}

// quote is strconv.Quote kept local for the small marshalers.
func quote(s string) string { return strconv.Quote(s) }

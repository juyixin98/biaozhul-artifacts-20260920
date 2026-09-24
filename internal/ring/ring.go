// Package ring implements a deterministic weighted consistent-hash ring.
//
// Hashing is fixed (SHA-256, first 8 bytes big-endian) and ordering is fixed
// (position, then node id, then vnode index), so rings built anywhere from the
// same configuration are byte-for-byte identical.
package ring

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
)

// VNodesPerWeightUnit is the default number of vnodes assigned per weight unit.
const VNodesPerWeightUnit = 64

const (
	maxWeight        = 1 << 20
	maxVnodesPerUnit = 1 << 20
)

// Node is a physical node with a capacity weight (weight 0 means weight 1).
type Node struct {
	ID     string `json:"id"`
	Weight int    `json:"weight"`
}

// VNode is one virtual node placement on the ring.
type VNode struct {
	Position uint64 // hashed position on [0, 2^64)
	NodeID   string
	Index    int
}

// vnodeView is the JSON form of VNode: Position is emitted both as a decimal
// string (uint64-safe for JavaScript clients) and as 0x-prefixed hex.
type vnodeView struct {
	Position    string `json:"position"`
	PositionHex string `json:"position_hex"`
	NodeID      string `json:"node_id"`
	Index       int    `json:"index"`
}

// MarshalJSON implements json.Marshaler.
func (v VNode) MarshalJSON() ([]byte, error) {
	return json.Marshal(vnodeView{
		Position:    fmt.Sprintf("%d", v.Position),
		PositionHex: "0x" + fmt.Sprintf("%016x", v.Position),
		NodeID:      v.NodeID,
		Index:       v.Index,
	})
}

// Ring is an immutable consistent-hash ring.
type Ring struct {
	name          string
	nodes         []Node // sorted by id
	nodesByName   map[string]Node
	vnodesPerUnit int
	vnodes        []VNode // sorted by (position, node id, index)
}

// New builds a ring from the given nodes. weight 0 is treated as 1.
// vnodesPerUnit <= 0 falls back to VNodesPerWeightUnit.
func New(name string, nodes []Node, vnodesPerUnit int) (*Ring, error) {
	if len(nodes) == 0 {
		return nil, errors.New("ring requires at least one node")
	}
	if vnodesPerUnit <= 0 {
		vnodesPerUnit = VNodesPerWeightUnit
	}
	if vnodesPerUnit > maxVnodesPerUnit {
		return nil, fmt.Errorf("vnodes_per_weight_unit too large: %d", vnodesPerUnit)
	}

	byName := make(map[string]Node, len(nodes))
	sorted := make([]Node, 0, len(nodes))
	for _, n := range nodes {
		if n.ID == "" {
			return nil, errors.New("node id must not be empty")
		}
		if n.Weight < 0 {
			return nil, fmt.Errorf("node %q has negative weight %d", n.ID, n.Weight)
		}
		if n.Weight > maxWeight {
			return nil, fmt.Errorf("node %q weight %d exceeds max %d", n.ID, n.Weight, maxWeight)
		}
		if _, dup := byName[n.ID]; dup {
			return nil, fmt.Errorf("duplicate node id %q", n.ID)
		}
		w := n.Weight
		if w == 0 {
			w = 1
		}
		byName[n.ID] = Node{ID: n.ID, Weight: w}
		sorted = append(sorted, Node{ID: n.ID, Weight: w})
	}
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].ID < sorted[j].ID })

	total := 0
	for _, n := range sorted {
		total += n.Weight * vnodesPerUnit
	}
	vs := make([]VNode, 0, total)
	for _, n := range sorted {
		count := n.Weight * vnodesPerUnit
		for i := 0; i < count; i++ {
			vs = append(vs, VNode{
				Position: Hash([]byte(VNodeKey(n.ID, i))),
				NodeID:   n.ID,
				Index:    i,
			})
		}
	}
	sort.Slice(vs, func(i, j int) bool { return lessVNode(vs[i], vs[j]) })

	return &Ring{
		name:          name,
		nodes:         sorted,
		nodesByName:   byName,
		vnodesPerUnit: vnodesPerUnit,
		vnodes:        vs,
	}, nil
}

// Hash returns the fixed hash of b: first 8 bytes of SHA-256, big-endian.
func Hash(b []byte) uint64 {
	sum := sha256.Sum256(b)
	return binary.BigEndian.Uint64(sum[:8])
}

// VNodeKey is the fixed key of a node's i-th vnode: "<nodeID>#vn<i>".
func VNodeKey(nodeID string, i int) string {
	return fmt.Sprintf("%s#vn%d", nodeID, i)
}

// Name returns the ring name.
func (r *Ring) Name() string { return r.name }

// Nodes returns the nodes sorted by id.
func (r *Ring) Nodes() []Node {
	out := make([]Node, len(r.nodes))
	copy(out, r.nodes)
	return out
}

// VNodesPerUnit returns the configured number of vnodes per weight unit.
func (r *Ring) VNodesPerUnit() int { return r.vnodesPerUnit }

// VNodes returns a copy of all vnodes sorted by (position, node id, index).
func (r *Ring) VNodes() []VNode {
	out := make([]VNode, len(r.vnodes))
	copy(out, r.vnodes)
	return out
}

// Owner returns the node id owning the given key.
func (r *Ring) Owner(key string) string {
	return r.OwnerBytes([]byte(key))
}

// OwnerBytes returns the node id owning the given raw key bytes.
func (r *Ring) OwnerBytes(key []byte) string {
	return r.OwnerAt(Hash(key))
}

// OwnerAt returns the node id owning a ring position h.
//
// The owner is the first vnode with position >= h (positions compared with
// their fixed tie-break); if none exists ownership wraps to the first vnode.
// Equivalently a vnode at position p owns the segment (prev, p] ending at p.
func (r *Ring) OwnerAt(h uint64) string {
	return ownerAt(r.vnodes, h)
}

func ownerAt(vs []VNode, h uint64) string {
	// vs are fully sorted by (position, node id, index), so the first vnode
	// whose position is >= h is also the first vnode at that position — the
	// one that owns the boundary point h.
	idx := sort.Search(len(vs), func(i int) bool { return vs[i].Position >= h })
	if idx == len(vs) {
		return vs[0].NodeID
	}
	return vs[idx].NodeID
}

func lessVNode(a, b VNode) bool {
	if a.Position != b.Position {
		return a.Position < b.Position
	}
	if a.NodeID != b.NodeID {
		return a.NodeID < b.NodeID
	}
	return a.Index < b.Index
}

// NodeView is the JSON-safe description of a node.
type NodeView struct {
	ID     string `json:"id"`
	Weight int    `json:"weight"`
	VNodes int    `json:"vnodes"`
}

// Summary is the JSON-safe description of a ring.
type Summary struct {
	Name          string     `json:"name"`
	Nodes         []NodeView `json:"nodes"`
	VNodesPerUnit int        `json:"vnodes_per_weight_unit"`
	TotalVNodes   int        `json:"total_vnodes"`
	TotalWeight   int        `json:"total_weight"`
}

// Summary returns the JSON-safe description of the ring.
func (r *Ring) Summary() Summary {
	views := make([]NodeView, 0, len(r.nodes))
	totalWeight := 0
	for _, n := range r.nodes {
		views = append(views, NodeView{
			ID:     n.ID,
			Weight: n.Weight,
			VNodes: n.Weight * r.vnodesPerUnit,
		})
		totalWeight += n.Weight
	}
	return Summary{
		Name:          r.name,
		Nodes:         views,
		VNodesPerUnit: r.vnodesPerUnit,
		TotalVNodes:   len(r.vnodes),
		TotalWeight:   totalWeight,
	}
}

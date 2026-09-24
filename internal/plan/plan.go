// Package plan computes exact rebalancing (migration) plans between two
// weighted consistent-hash rings.
//
// Every vnode position from the old and new ring is a cut point. Cutting the
// key space at all cut points yields segments on which both the old and new
// owner are constant; a segment is a migration iff the owners differ.
//
// The segments are inclusive integer ranges [lo, hi] that tile [0, 2^64)
// exactly: they are pairwise disjoint, leave no gap, and their key counts sum
// to 2^64.
package plan

import (
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"sort"

	"consistenthash/internal/ring"
)

// SpaceSize is the number of addressable ring positions: 2^64.
var SpaceSize = new(big.Int).Lsh(big.NewInt(1), 64)

var one = big.NewInt(1)

// Range is an inclusive integer interval [Lo, Hi] inside [0, 2^64).
type Range struct {
	Lo       uint64
	Hi       uint64
	KeyCount big.Int
}

type rangeJSON struct {
	Lo       string `json:"lo"`
	LoHex    string `json:"lo_hex"`
	Hi       string `json:"hi"`
	HiHex    string `json:"hi_hex"`
	KeyCount string `json:"key_count"`
}

// MarshalJSON implements json.Marshaler. Bounds are emitted as decimal strings
// (so uint64 values survive JavaScript) and 0x-prefixed hex.
func (r Range) MarshalJSON() ([]byte, error) {
	return json.Marshal(rangeJSON{
		Lo:       fmt.Sprintf("%d", r.Lo),
		LoHex:    "0x" + fmt.Sprintf("%016x", r.Lo),
		Hi:       fmt.Sprintf("%d", r.Hi),
		HiHex:    "0x" + fmt.Sprintf("%016x", r.Hi),
		KeyCount: r.KeyCount.String(),
	})
}

// Segment is a maximal range on which both owners are constant.
type Segment struct {
	Range Range  `json:"range"`
	From  string `json:"from_node"`
	To    string `json:"to_node"`
}

// Moved reports whether ownership of the segment changes.
func (s Segment) Moved() bool { return s.From != s.To }

// Move bundles all ranges that migrate from one old node to one new node.
type Move struct {
	FromNode string  `json:"from_node"`
	ToNode   string  `json:"to_node"`
	Ranges   []Range `json:"ranges"`
	KeyCount big.Int `json:"key_count"`
}

type moveJSON struct {
	FromNode   string  `json:"from_node"`
	ToNode     string  `json:"to_node"`
	Ranges     []Range `json:"ranges"`
	RangeCount int     `json:"range_count"`
	KeyCount   string  `json:"key_count"`
}

// MarshalJSON implements json.Marshaler.
func (m Move) MarshalJSON() ([]byte, error) {
	return json.Marshal(moveJSON{
		FromNode:   m.FromNode,
		ToNode:     m.ToNode,
		Ranges:     m.Ranges,
		RangeCount: len(m.Ranges),
		KeyCount:   m.KeyCount.String(),
	})
}

// NodeShare records a new-ring node's resulting share of the key space.
type NodeShare struct {
	NodeID    string  `json:"node_id"`
	Weight    int     `json:"weight"`
	VNodes    int     `json:"vnodes"`
	OwnedKeys big.Int `json:"owned_keys"`
	Fraction  string  `json:"share_fraction"` // exact fraction, e.g. "1/6"
	Share     string  `json:"share"`          // decimal, 12 digits, display only
}

type nodeShareJSON struct {
	NodeID    string `json:"node_id"`
	Weight    int    `json:"weight"`
	VNodes    int    `json:"vnodes"`
	OwnedKeys string `json:"owned_keys"`
	Fraction  string `json:"share_fraction"`
	Share     string `json:"share"`
}

// MarshalJSON implements json.Marshaler.
func (s NodeShare) MarshalJSON() ([]byte, error) {
	return json.Marshal(nodeShareJSON{
		NodeID:    s.NodeID,
		Weight:    s.Weight,
		VNodes:    s.VNodes,
		OwnedKeys: s.OwnedKeys.String(),
		Fraction:  s.Fraction,
		Share:     s.Share,
	})
}

// Plan is the exact migration plan from one ring topology to another.
type Plan struct {
	Old               ring.Summary `json:"old"`
	New               ring.Summary `json:"new"`
	Segments          []Segment    `json:"segments"`
	Moves             []Move       `json:"moves"`
	NodeShares        []NodeShare  `json:"node_shares"`
	MovedKeys         big.Int      `json:"moved_keys"`
	UnchangedKeys     big.Int      `json:"unchanged_keys"`
	MovedSegments     int          `json:"moved_segments"`
	UnchangedSegments int          `json:"unchanged_segments"`
}

type planJSON struct {
	Old               ring.Summary `json:"old"`
	New               ring.Summary `json:"new"`
	KeySpace          string       `json:"key_space"`
	Segments          []Segment    `json:"segments"`
	Moves             []Move       `json:"moves"`
	NodeShares        []NodeShare  `json:"node_shares"`
	MovedKeys         string       `json:"moved_keys"`
	UnchangedKeys     string       `json:"unchanged_keys"`
	TotalSegments     int          `json:"total_segments"`
	MovedSegments     int          `json:"moved_segments"`
	UnchangedSegments int          `json:"unchanged_segments"`
}

// MarshalJSON implements json.Marshaler.
func (p Plan) MarshalJSON() ([]byte, error) {
	return json.Marshal(planJSON{
		Old:               p.Old,
		New:               p.New,
		KeySpace:          SpaceSize.String(),
		Segments:          p.Segments,
		Moves:             p.Moves,
		NodeShares:        p.NodeShares,
		MovedKeys:         p.MovedKeys.String(),
		UnchangedKeys:     p.UnchangedKeys.String(),
		TotalSegments:     len(p.Segments),
		MovedSegments:     p.MovedSegments,
		UnchangedSegments: p.UnchangedSegments,
	})
}

// Build computes the exact migration plan from old to new.
func Build(old, newr *ring.Ring) (*Plan, error) {
	if old == nil || newr == nil {
		return nil, errors.New("plan: both rings must be non-nil")
	}

	oldV := old.VNodes()
	newV := newr.VNodes()

	// Unique cut points, ascending.
	posSet := make(map[uint64]struct{}, len(oldV)+len(newV))
	for _, v := range oldV {
		posSet[v.Position] = struct{}{}
	}
	for _, v := range newV {
		posSet[v.Position] = struct{}{}
	}
	positions := make([]uint64, 0, len(posSet))
	for p := range posSet {
		positions = append(positions, p)
	}
	sort.Slice(positions, func(i, j int) bool { return positions[i] < positions[j] })

	segments := make([]Segment, 0, len(positions)+1)

	addSegment := func(lo, hi uint64) {
		if lo > hi {
			return
		}
		rng := Range{Lo: lo, Hi: hi}
		// hi - lo + 1; hi >= lo so no underflow.
		rng.KeyCount.SetUint64(hi)
		rng.KeyCount.Add(&rng.KeyCount, one)
		loBig := new(big.Int).SetUint64(lo)
		rng.KeyCount.Sub(&rng.KeyCount, loBig)

		segments = append(segments, Segment{
			Range: rng,
			From:  ownerAt(oldV, lo),
			To:    ownerAt(newV, lo),
		})
	}

	// Linear segments ending at each cut point: (prev, p], first one [0, p0].
	for i, p := range positions {
		var lo uint64
		if i > 0 {
			lo = positions[i-1] + 1
		}
		addSegment(lo, p)
	}
	// Wrap segment (lastPos, 2^64) -> first vnode. Absorb the point 0 into
	// the first linear segment instead of representing a true ring wrap.
	if last := positions[len(positions)-1]; last < ^uint64(0) {
		addSegment(last+1, ^uint64(0))
	}

	// Tally ownership in the new ring (segments are already tiling order).
	owned := make(map[string]*big.Int)
	for _, n := range newr.Nodes() {
		owned[n.ID] = new(big.Int)
	}
	// Nodes that exist only in the old ring can still appear as From; NodeShares
	// covers new-ring nodes only.

	type moveAgg struct {
		from, to string
		ranges   []Range
		count    big.Int
		order    int
	}
	moveByKey := make(map[string]*moveAgg)
	moveOrder := make([]string, 0)

	var moved, unchanged big.Int
	movedSeg, unchangedSeg := 0, 0

	for _, seg := range segments {
		c := new(big.Int).Set(&seg.Range.KeyCount)
		if c2, ok := owned[seg.To]; ok {
			c2.Add(c2, c)
		}
		if seg.Moved() {
			moved.Add(&moved, c)
			movedSeg++
			key := seg.From + "\x00" + seg.To
			m, ok := moveByKey[key]
			if !ok {
				m = &moveAgg{from: seg.From, to: seg.To, order: len(moveOrder)}
				moveByKey[key] = m
				moveOrder = append(moveOrder, key)
			}
			m.ranges = append(m.ranges, seg.Range)
			m.count.Add(&m.count, c)
		} else {
			unchanged.Add(&unchanged, c)
			unchangedSeg++
		}
	}

	moves := make([]Move, 0, len(moveOrder))
	for _, key := range moveOrder {
		m := moveByKey[key]
		ranges := make([]Range, len(m.ranges))
		copy(ranges, m.ranges)
		count := new(big.Int).Set(&m.count)
		moves = append(moves, Move{
			FromNode: m.from,
			ToNode:   m.to,
			Ranges:   ranges,
			KeyCount: *count,
		})
	}

	shares := make([]NodeShare, 0, len(newr.Nodes()))
	for _, n := range newr.Nodes() {
		cnt := owned[n.ID] // always present
		rat := new(big.Rat).SetFrac(new(big.Int).Set(cnt), SpaceSize)
		dec := new(big.Float).SetRat(rat).Text('f', 12)
		shares = append(shares, NodeShare{
			NodeID:    n.ID,
			Weight:    n.Weight,
			VNodes:    n.Weight * newr.VNodesPerUnit(),
			OwnedKeys: *new(big.Int).Set(cnt),
			Fraction:  rat.String(),
			Share:     dec,
		})
	}

	return &Plan{
		Old:               old.Summary(),
		New:               newr.Summary(),
		Segments:          segments,
		Moves:             moves,
		NodeShares:        shares,
		MovedKeys:         *new(big.Int).Set(&moved),
		UnchangedKeys:     *new(big.Int).Set(&unchanged),
		MovedSegments:     movedSeg,
		UnchangedSegments: unchangedSeg,
	}, nil
}

// ownerAt mirrors ring.Ring.OwnerAt against an arbitrary vnode slice.
func ownerAt(vs []ring.VNode, h uint64) string {
	idx := sort.Search(len(vs), func(i int) bool { return vs[i].Position >= h })
	if idx == len(vs) {
		return vs[0].NodeID
	}
	return vs[idx].NodeID
}

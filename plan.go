package main

import (
	"encoding/json"
	"math/big"
	"sort"
)

// Arc is a half-open arc along the forward circle:
//
//	{ h | StartExclusive < h <= EndInclusive }        (Wrap == false)
//	{ h | h > StartExclusive } U { h | h <= EndInclusive }  (Wrap == true)
//
// The arc "starts just after" StartExclusive and ends at EndInclusive.
// StartExclusive == EndInclusive with Wrap=true denotes the whole circle
// (used when old and new ownership is identical everywhere).
type Arc struct {
	StartExclusive uint64 `json:"start_exclusive"`
	EndInclusive   uint64 `json:"end_inclusive"`
	Wrap           bool   `json:"wrap"`
}

// Length returns the number of hash positions covered by the arc (1 .. 2^64).
func (a Arc) Length() *big.Int {
	length := new(big.Int)
	if a.Wrap {
		// (MaxUint64 - start) + (end + 1) == MaxUint64 + 1 + end - start
		length.SetUint64(a.EndInclusive)
		length.Add(length, new(big.Int).Lsh(big.NewInt(1), 64))
		length.Sub(length, new(big.Int).SetUint64(a.StartExclusive))
	} else {
		length.SetUint64(a.EndInclusive)
		length.Sub(length, new(big.Int).SetUint64(a.StartExclusive))
	}
	return length
}

// Contains reports whether hash position h falls inside the arc.
func (a Arc) Contains(h uint64) bool {
	if a.Wrap {
		return h > a.StartExclusive || h <= a.EndInclusive
	}
	return h > a.StartExclusive && h <= a.EndInclusive
}

// MarshalJSON adds hex encodings and the decimal length string to the JSON
// representation.
func (a Arc) MarshalJSON() ([]byte, error) {
	type alias Arc
	return json.Marshal(struct {
		alias
		StartHex string `json:"start_exclusive_hex"`
		EndHex   string `json:"end_inclusive_hex"`
		Length   string `json:"length"`
	}{
		alias:    alias(a),
		StartHex: "0x" + toHex(a.StartExclusive),
		EndHex:   "0x" + toHex(a.EndInclusive),
		Length:   a.Length().String(),
	})
}

// Move describes one maximal arc whose owner changes from From to To.
// From == "" means the old ring was empty (initial assignment); To == ""
// means the new ring is empty (the whole ring is drained).
type Move struct {
	From string `json:"from"`
	To   string `json:"to"`
	Arc  Arc    `json:"arc"`
}

// Stay describes one maximal arc whose owner does not change, kept so the
// response proves which ranges are NOT migrated and the arcs partition the
// entire circle.
type Stay struct {
	Node string `json:"node"`
	Arc  Arc    `json:"arc"`
}

// NodeFlow summarizes migration volume for one physical node.
type NodeFlow struct {
	Node       string `json:"node"`
	Incoming   string `json:"incoming_length"`
	Outgoing   string `json:"outgoing_length"`
	Unaffected string `json:"unaffected_length"`
}

// Plan is the precise rebalancing plan between two ring revisions.
type Plan struct {
	FromRevision int        `json:"from_revision"`
	ToRevision   int        `json:"to_revision"`
	Moved        []Move     `json:"moved"`
	Stayed       []Stay     `json:"stayed"`
	NodeFlows    []NodeFlow `json:"node_flows"`
	// TotalLength is always 2^64: sum of all moved and stayed arc lengths.
	TotalLength string `json:"total_length"`
	// MovedLength is the hash-space length that changes owner.
	MovedLength string `json:"moved_length"`
	// MovedFraction is MovedLength / 2^64 in [0,1].
	MovedFraction float64 `json:"moved_fraction"`
}

// seg is a linear segment (prev, pos] in non-wrap coordinates, tagged with
// old/new owners.
type seg struct {
	prev uint64
	pos  uint64
	from string
	to   string
}

// BuildPlan computes the exact migration plan from ring old to ring new.
// Either ring may be empty (nil is treated as empty). The plan partitions the
// full 2^64 hash circle: moved arcs U stayed arcs are seamless, non-overlapping
// and total to 2^64.
func BuildPlan(fromRev, toRev int, old, nxt *Ring) *Plan {
	// Build the sorted set of every vnode boundary from both rings.
	boundarySet := make(map[uint64]struct{})
	for _, r := range []*Ring{old, nxt} {
		if r != nil {
			for _, v := range r.vnodes {
				boundarySet[v.Hash] = struct{}{}
			}
		}
	}
	bounds := make([]uint64, 0, len(boundarySet))
	for h := range boundarySet {
		bounds = append(bounds, h)
	}
	sort.Slice(bounds, func(i, j int) bool { return bounds[i] < bounds[j] })

	// Linear segments: (b[i-1], b[i]] for i>=1, plus the wrap segment
	// (b[last], b[0]] containing position 0. Each segment has one constant
	// owner in each ring: owners only change at boundaries.
	var segs []seg
	for i := 1; i < len(bounds); i++ {
		segs = append(segs, seg{
			prev: bounds[i-1],
			pos:  bounds[i],
			from: ownerOrEmpty(old, bounds[i]),
			to:   ownerOrEmpty(nxt, bounds[i]),
		})
	}

	// Merge adjacent linear segments with equal (from,to) pairs.
	merged := mergeSegments(segs)

	var moves []Move
	var stays []Stay
	if len(bounds) > 0 {
		lastBound := bounds[len(bounds)-1]
		firstBound := bounds[0]
		wrapFrom, wrapTo := ownerAt0(old), ownerAt0(nxt)

		// Leading/trailing linear blocks contiguous with the wrap segment
		// (the wrap arc touches merged[0] at firstBound and the tail at
		// lastBound). Count how many segments at each end share the wrap
		// pair so one maximal wrapping arc can be formed.
		lead := 0
		for lead < len(merged) && merged[lead].from == wrapFrom && merged[lead].to == wrapTo {
			lead++
		}
		tail := len(merged)
		for tail > lead && merged[tail-1].from == wrapFrom && merged[tail-1].to == wrapTo {
			tail--
		}

		var arcs []Arc
		var pairs [][2]string
		if lead == len(merged) {
			// Every segment (linear and wrap) has the same pair: whole circle.
			arcs = append(arcs, Arc{StartExclusive: lastBound, EndInclusive: lastBound, Wrap: true})
			pairs = append(pairs, [2]string{wrapFrom, wrapTo})
		} else {
			// Middle (strictly linear) arcs first, in forward order.
			for _, s := range merged[lead:tail] {
				arcs = append(arcs, Arc{StartExclusive: s.prev, EndInclusive: s.pos})
				pairs = append(pairs, [2]string{s.from, s.to})
			}
			if lead > 0 || tail < len(merged) {
				// Tail block + wrap + lead block form one wrapping arc.
				start := lastBound
				if tail < len(merged) {
					start = merged[tail].prev
				}
				end := firstBound
				if lead > 0 {
					end = merged[lead-1].pos
				}
				arcs = append(arcs, Arc{StartExclusive: start, EndInclusive: end, Wrap: true})
				pairs = append(pairs, [2]string{wrapFrom, wrapTo})
			} else {
				// Wrap pair differs from both neighbours: standalone wrap arc.
				arcs = append(arcs, Arc{StartExclusive: lastBound, EndInclusive: firstBound, Wrap: true})
				pairs = append(pairs, [2]string{wrapFrom, wrapTo})
			}
		}

		for i, arc := range arcs {
			if pairs[i][0] != pairs[i][1] {
				moves = append(moves, Move{From: pairs[i][0], To: pairs[i][1], Arc: arc})
			} else {
				stays = append(stays, Stay{Node: pairs[i][0], Arc: arc})
			}
		}
	}

	plan := &Plan{
		FromRevision: fromRev,
		ToRevision:   toRev,
		Moved:        moves,
		Stayed:       stays,
		TotalLength:  new(big.Int).Lsh(big.NewInt(1), 64).String(),
	}

	// Aggregate flows per node (nodes appearing in either revision).
	nodeSet := make(map[string]struct{})
	for _, m := range moves {
		if m.From != "" {
			nodeSet[m.From] = struct{}{}
		}
		if m.To != "" {
			nodeSet[m.To] = struct{}{}
		}
	}
	for _, s := range stays {
		nodeSet[s.Node] = struct{}{}
	}
	flows := map[string]*NodeFlow{}
	zero := func(id string) *NodeFlow {
		f := &NodeFlow{Node: id, Incoming: "0", Outgoing: "0", Unaffected: "0"}
		flows[id] = f
		nodeSet[id] = struct{}{}
		return f
	}
	get := func(id string) *NodeFlow {
		if f, ok := flows[id]; ok {
			return f
		}
		return zero(id)
	}
	addLen := func(cur string, l *big.Int) string {
		c, ok := new(big.Int).SetString(cur, 10)
		if !ok {
			c = new(big.Int)
		}
		return c.Add(c, l).String()
	}
	movedLen := new(big.Int)
	for _, m := range moves {
		l := m.Arc.Length()
		movedLen.Add(movedLen, l)
		if m.From != "" {
			f := get(m.From)
			f.Outgoing = addLen(f.Outgoing, l)
		}
		if m.To != "" {
			f := get(m.To)
			f.Incoming = addLen(f.Incoming, l)
		}
	}
	for _, s := range stays {
		f := get(s.Node)
		f.Unaffected = addLen(f.Unaffected, s.Arc.Length())
	}

	ids := make([]string, 0, len(nodeSet))
	for id := range nodeSet {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		plan.NodeFlows = append(plan.NodeFlows, *flows[id])
	}
	plan.MovedLength = movedLen.String()
	total, _ := new(big.Int).SetString(plan.TotalLength, 10)
	frac, _ := new(big.Float).Quo(
		new(big.Float).SetInt(movedLen),
		new(big.Float).SetInt(total),
	).Float64()
	plan.MovedFraction = frac
	return plan
}

// mergeSegments merges adjacent linear segments with equal (from,to).
func mergeSegments(in []seg) []seg {
	if len(in) == 0 {
		return in
	}
	out := []seg{in[0]}
	for i := 1; i < len(in); i++ {
		last := &out[len(out)-1]
		if last.from == in[i].from && last.to == in[i].to {
			last.pos = in[i].pos
		} else {
			out = append(out, in[i])
		}
	}
	return out
}

func ownerOrEmpty(r *Ring, h uint64) string {
	if r == nil || r.Empty() {
		return ""
	}
	return r.ownerAt(h)
}

// ownerAt0 returns the owner of position 0, which lies in the wrap segment
// (last boundary, first boundary].
func ownerAt0(r *Ring) string {
	if r == nil || r.Empty() {
		return ""
	}
	return r.vnodes[0].NodeID
}

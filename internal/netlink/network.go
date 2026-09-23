// Package netlink models the unreliable links between simulated nodes.
// It never performs real I/O: sending a message only schedules simulated
// arrival events. Links may drop, duplicate and reorder packets, driven by
// a seeded RNG plus explicit forced rules for reproducible scenarios.
package netlink

import (
	"math/rand"
)

// Link are the per-(source,destination) link parameters. Probabilities are
// in [0,1].
type Link struct {
	Loss       float64 `json:"loss"`
	Duplicate  float64 `json:"duplicate"`
	BaseDelay  float64 `json:"baseDelay"`
	Jitter     float64 `json:"jitter"`
	ReorderJit float64 `json:"reorderJitter"`
}

// Network is the link fabric.
type Network struct {
	N           int
	DefaultLink Link
	Links       map[[2]int]Link
	ForcedDrops map[string][]int // msgID -> destination indices (empty = all)
	Holds       map[string]map[int][]float64
	// Delays suppresses the normal link packet for msgID at the listed
	// destinations and instead emits only the scripted delayed copies. This
	// models a retransmission that arrives much later (used to script
	// buffering/backpressure deterministically).
	Delays map[string]map[int][]float64
	Rand   *rand.Rand
}

// New builds a fabric from per-link overrides.
func New(n int, def Link, overrides map[[2]int]Link, seed int64) *Network {
	nw := &Network{
		N:           n,
		DefaultLink: def,
		Links:       make(map[[2]int]Link),
		ForcedDrops: make(map[string][]int),
		Holds:       make(map[string]map[int][]float64),
		Delays:      make(map[string]map[int][]float64),
		Rand:        rand.New(rand.NewSource(seed)),
	}
	for k, v := range overrides {
		nw.Links[k] = v
	}
	return nw
}

// ForceDrop marks msgID as dropped at dst (-1 = all destinations).
func (nw *Network) ForceDrop(msgID string, dst int) {
	nw.ForcedDrops[msgID] = append(nw.ForcedDrops[msgID], dst)
}

// ForceHold schedules an extra copy of msgID to dst (-1 = all) with the
// given delay. Multiple ForceHold calls produce multiple delayed copies.
func (nw *Network) ForceHold(msgID string, dst int, delay float64) {
	m, ok := nw.Holds[msgID]
	if !ok {
		m = make(map[int][]float64)
		nw.Holds[msgID] = m
	}
	if dst == -1 {
		for d := 0; d < nw.N; d++ {
			m[d] = append(m[d], delay)
		}
	} else {
		m[dst] = append(m[dst], delay)
	}
}

// ForceDelay suppresses the normal packet of msgID to dst and emits only
// scripted delayed copies (a deterministic late arrival / retransmission).
func (nw *Network) ForceDelay(msgID string, dst int, delays ...float64) {
	m, ok := nw.Delays[msgID]
	if !ok {
		m = make(map[int][]float64)
		nw.Delays[msgID] = m
	}
	m[dst] = append(m[dst], delays...)
}

func (nw *Network) linkOf(src, dst int) Link {
	if l, ok := nw.Links[[2]int{src, dst}]; ok {
		return l
	}
	return nw.DefaultLink
}

func (nw *Network) forcedDrop(id string, dst int) bool {
	for _, d := range nw.ForcedDrops[id] {
		if d == -1 || d == dst {
			return true
		}
	}
	return false
}

// Plan is the outcome of sending one message: zero or more arrival delays,
// one per surviving packet copy. An empty slice means total loss.
type Plan struct {
	Arrivals []float64
	// DropReason is "" when delivered, otherwise "forced" or "loss".
	DropReason string
	// DupExtra is the number of copies beyond the first one delivered.
	DupExtra int
}

// Send computes what the link (src -> dst) does with m at time now.
// RNG calls happen in a fixed order (loss, duplicate, delays) so a given
// seed always produces the same run.
func (nw *Network) Send(mID string, src, dst int, now float64) Plan {
	l := nw.linkOf(src, dst)

	if nw.forcedDrop(mID, dst) {
		return Plan{DropReason: "forced"}
	}

	// Scripted late arrivals replace the normal packet entirely.
	if delays, ok := nw.Delays[mID][dst]; ok && len(delays) > 0 {
		arr := make([]float64, len(delays))
		for i, d := range delays {
			arr[i] = now + d
		}
		return Plan{Arrivals: arr}
	}

	if l.Loss > 0 && nw.Rand.Float64() < l.Loss {
		return Plan{DropReason: "loss"}
	}

	copies := 1
	if l.Duplicate > 0 && nw.Rand.Float64() < l.Duplicate {
		copies++
	}
	held := nw.Holds[mID][dst]

	plan := Plan{}
	for c := 0; c < copies; c++ {
		// Per-copy uniform jitter around BaseDelay; copies can then pass
		// each other, which is how reordering appears in a queue-free link
		// model.
		delay := l.BaseDelay + (nw.Rand.Float64()*2-1)*l.Jitter + (nw.Rand.Float64()*2-1)*l.ReorderJit
		if delay < 0 {
			delay = 0
		}
		plan.Arrivals = append(plan.Arrivals, now+delay)
	}
	// Scripted extra copies keep their explicit relative timing and are
	// not counted in the jittered loop above.
	for _, d := range held {
		plan.Arrivals = append(plan.Arrivals, now+d)
	}
	plan.DupExtra = len(plan.Arrivals) - 1
	return plan
}

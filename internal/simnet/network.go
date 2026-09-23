// Package simnet models an unreliable in-process network: every message copy
// sent between two nodes may be dropped, duplicated and/or delayed. Loss,
// duplication and reordering therefore arise naturally from per-copy delays
// rather than from any real cluster or wall-clock timing.
package simnet

import (
	"math/rand"

	"causal-broadcast/internal/node"
)

// Action describes what the network did with one copy.
type Action string

const (
	ActionDelivered Action = "delivered" // one copy scheduled
	ActionDuplicate Action = "duplicate" // two copies scheduled (independent arrival paths)
	ActionDropped   Action = "dropped"   // copy discarded
)

// Policy is the seeded stochastic part of the network. Rates are in [0,1].
type Policy struct {
	LossRate      float64 `json:"loss_rate"`
	DuplicateRate float64 `json:"duplicate_rate"`
	MinDelayMs    int64   `json:"min_delay_ms"`
	MaxDelayMs    int64   `json:"max_delay_ms"`
}

// Fault is an explicit, deterministic override matching individual copies.
// Empty string / 0 in a matcher means wildcard; -1 in DelayMs means
// "no override". First matching fault wins; stochastic policy is only
// consulted when no fault matches.
type Fault struct {
	OriginNode string `json:"origin_node,omitempty"`
	OriginSeq  int    `json:"origin_seq,omitempty"`
	From       string `json:"from,omitempty"`
	To         string `json:"to,omitempty"`
	Action     string `json:"action"`             // deliver | drop | duplicate | delay
	DelayMs    int64  `json:"delay_ms,omitempty"` // for action=delay
}

// Decision is the logged outcome of one Send call.
type Decision struct {
	Time    int64  `json:"time"`
	From    string `json:"from"`
	To      string `json:"to"`
	Origin  string `json:"origin"`
	Action  Action `json:"action"`
	DelayMs int64  `json:"delay_ms,omitempty"`
	Reason  string `json:"reason"` // "fault" or "random"
}

// Sink receives scheduled arrivals. The engine supplies it so the network
// stays free of simulator/event-package imports.
type Sink func(arrivalTime int64, to string, m *node.Message)

// Network applies faults + random policy and schedules copies via Sink.
type Network struct {
	policy Policy
	faults []Fault
	rng    *rand.Rand
	sink   Sink
	log    []Decision
}

// New builds a network. rng is shared and advanced deterministically (one
// draw block per Send, nodes traversed in sorted order by the engine).
func New(policy Policy, faults []Fault, rng *rand.Rand, sink Sink) *Network {
	return &Network{policy: policy, faults: faults, rng: rng, sink: sink}
}

// Log returns all per-copy decisions in send order.
func (n *Network) Log() []Decision { return n.log }

func (n *Network) match(m *node.Message, to string) (*Fault, bool) {
	for i := range n.faults {
		f := &n.faults[i]
		if f.OriginNode != "" && f.OriginNode != m.Origin.Node {
			continue
		}
		if f.OriginSeq != 0 && f.OriginSeq != m.Origin.Seq {
			continue
		}
		// An omitted From anchors the fault to the FIRST hop: it matches
		// only copies sent by the message originator. Set From explicitly
		// to target relay (gossip) copies from another holder.
		from := f.From
		if from == "" {
			from = m.Origin.Node
		}
		if from != m.From {
			continue
		}
		if f.To != "" && f.To != to {
			continue
		}
		return f, true
	}
	return nil, false
}

func (n *Network) delay() int64 {
	lo, hi := n.policy.MinDelayMs, n.policy.MaxDelayMs
	if hi < lo {
		hi = lo
	}
	if hi <= 0 {
		return 0
	}
	return lo + n.rng.Int63n(hi-lo+1)
}

// Send handles one copy from m.From to to at virtual time now. It records a
// Decision and, unless dropped, schedules one (or two) arrivals through Sink.
func (n *Network) Send(now int64, to string, m *node.Message) {
	arrival := func(d int64) {
		n.sink(now+d, to, m)
	}
	decision := Decision{
		Time:   now,
		From:   m.From,
		To:     to,
		Origin: m.Origin.String(),
	}

	if f, ok := n.match(m, to); ok {
		decision.Reason = "fault"
		switch Action(f.Action) {
		case "drop":
			decision.Action = ActionDropped
		case "duplicate":
			decision.Action = ActionDuplicate
			d := f.DelayMs
			if d < 0 {
				d = n.delay()
			}
			decision.DelayMs = d
			arrival(d)
			arrival(d + 1) // distinct arrival times, still a true duplicate copy
		case "delay":
			decision.Action = ActionDelivered
			decision.DelayMs = f.DelayMs
			arrival(f.DelayMs)
		default: // "delivered"
			decision.Action = ActionDelivered
			d := f.DelayMs
			if d < 0 {
				d = n.delay()
			}
			decision.DelayMs = d
			arrival(d)
		}
		n.log = append(n.log, decision)
		return
	}

	// Stochastic policy: fixed draw order per copy -> reproducible.
	decision.Reason = "random"
	if r := n.rng.Float64(); r < n.policy.LossRate {
		decision.Action = ActionDropped
		n.log = append(n.log, decision)
		return
	}
	d := n.delay()
	decision.DelayMs = d
	if r := n.rng.Float64(); r < n.policy.DuplicateRate {
		decision.Action = ActionDuplicate
		arrival(d)
		arrival(d + 1)
	} else {
		decision.Action = ActionDelivered
		arrival(d)
	}
	n.log = append(n.log, decision)
}

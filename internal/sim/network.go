// Package sim provides an in-memory, tick-driven simulated network and cluster
// harness around the raft nodes. The network never touches a socket: messages
// are queued with a deterministic delivery tick, and anything that cannot be
// delivered (network partition, powered-off target) is quarantined into a
// "stale" queue that the fault model can later release, modelling an old
// message arriving after a long delay.
package sim

import (
	"sort"

	"raftlab/internal/raft"
)

type queuedMsg struct {
	dueAt int
	msg   raft.Message
}

// network holds the in-flight message queues for one cluster.
type network struct {
	tick    int
	latency int            // fixed per-hop delivery delay in ticks
	pending []queuedMsg    // messages waiting for their delivery tick
	stale   []raft.Message // undeliverable messages, held for late replay
	groups  map[int]string // node id -> partition group; equal groups are connected
}

func newNetwork(latency int, ids []int) *network {
	g := make(map[int]string, len(ids))
	for _, id := range ids {
		g[id] = "A"
	}
	return &network{tick: 0, latency: latency, groups: g}
}

// connected reports whether two live nodes can exchange messages.
func (n *network) connected(a, b int) bool {
	return n.groups[a] == n.groups[b]
}

func (n *network) setGroup(id int, group string) { n.groups[id] = group }

func (n *network) heal() {
	for id := range n.groups {
		n.groups[id] = "A"
	}
}

// send queues a fresh message produced at the current tick.
func (n *network) send(m raft.Message) {
	n.pending = append(n.pending, queuedMsg{dueAt: n.tick + n.latency, msg: m})
}

// due removes and returns messages due by now, in delivery order.
// Delivery order is by (to, from, type) so results do not depend on Go map
// iteration or enqueue accidents.
func (n *network) due(now int) []raft.Message {
	out := []raft.Message{}
	kept := make([]queuedMsg, 0, len(n.pending))
	for _, q := range n.pending {
		if q.dueAt <= now {
			out = append(out, q.msg)
		} else {
			kept = append(kept, q)
		}
	}
	n.pending = kept
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if a.To != b.To {
			return a.To < b.To
		}
		if a.From != b.From {
			return a.From < b.From
		}
		return a.Type < b.Type
	})
	return out
}

// quarantine stores an undeliverable message for possible late delivery.
func (n *network) quarantine(m raft.Message) {
	n.stale = append(n.stale, m)
}

// releaseStale re-enqueues every quarantined message as if it had just
// finished a very long network delay. Returns how many were released.
func (n *network) releaseStale() int {
	k := len(n.stale)
	for _, m := range n.stale {
		n.send(m)
	}
	n.stale = n.stale[:0]
	return k
}

func (n *network) dropStale() int {
	k := len(n.stale)
	n.stale = n.stale[:0]
	return k
}

func (n *network) staleCount() int { return len(n.stale) }

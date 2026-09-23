// Package node implements per-node causal-broadcast state: a vector clock of
// delivered messages, a hold-back buffer for messages whose dependencies are
// not yet satisfied, de-duplication and bounded-buffer backpressure.
package node

import (
	"sort"

	"causal-broadcast/internal/vec"
)

// Message is one broadcast copy carried between nodes.
type Message struct {
	Origin  vec.Key   `json:"origin"` // (originating node, per-origin seq)
	Sender  string    `json:"sender"` // node that relayed this copy
	From    string    `json:"from"`   // immediate previous hop (for logging)
	Deps    []vec.Key `json:"deps"`   // causal dependency vector (all keys <= V_origin)
	Clock   vec.Clock `json:"clock"`  // origin's clock at broadcast time (redundant with Deps, kept for inspection)
	Payload string    `json:"payload"`
}

// Delivery records one successful application-level delivery.
type Delivery struct {
	Time    int64     `json:"time"`
	Node    string    `json:"node"`
	Origin  vec.Key   `json:"origin"`
	Payload string    `json:"payload"`
	Clock   vec.Clock `json:"clock_after"`
}

// Outcome classifies what a node does with an incoming copy.
type Outcome int

const (
	// Delivered means this copy (or buffered messages it unblocked) was applied.
	Delivered Outcome = iota
	// Buffered means the message is held waiting for missing dependencies.
	Buffered
	// Duplicate means this message was already delivered; the copy is dropped.
	Duplicate
	// Backpressure means dependencies are missing AND the buffer is full;
	// the message is rejected and NOT retained (retry may arrive later).
	Backpressure
)

// BackpressureEvent captures one rejected copy for diagnostics.
type BackpressureEvent struct {
	Time      int64     `json:"time"`
	Node      string    `json:"node"`
	Origin    vec.Key   `json:"origin"`
	Capacity  int       `json:"capacity"`
	WaitingOn []vec.Key `json:"waiting_on"`
}

// Node is a single simulated process.
type Node struct {
	ID        string
	clock     vec.Clock
	buffer    map[vec.Key]*Message
	delivered map[vec.Key]bool
	cap       int

	// Counters/logs
	dupCount     int
	backpressure []BackpressureEvent
}

// New creates a node with a hold-back buffer bounded by capacity (<=0 means
// unbounded).
func New(id string, capacity int) *Node {
	return &Node{
		ID:        id,
		clock:     vec.Clock{},
		buffer:    map[vec.Key]*Message{},
		delivered: map[vec.Key]bool{},
		cap:       capacity,
	}
}

// Clock returns a copy of the node's delivered vector clock.
func (n *Node) Clock() vec.Clock { return n.clock.Clone() }

// BufferLen returns the number of messages currently held back.
func (n *Node) BufferLen() int { return len(n.buffer) }

// BufferKeys returns the origins currently buffered, deterministically sorted.
func (n *Node) BufferKeys() []vec.Key {
	out := make([]vec.Key, 0, len(n.buffer))
	for k := range n.buffer {
		out = append(out, k)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Less(out[j]) })
	return out
}

// BufferedMissing returns, for every buffered message, the dependencies still
// missing at this node, keyed by the buffered message origin. Uses the same
// readiness rule as delivery (same-origin predecessor via seq-1).
func (n *Node) BufferedMissing() map[string][]vec.Key {
	out := map[string][]vec.Key{}
	for _, k := range n.BufferKeys() {
		miss := ready(n.clock, n.buffer[k])
		if len(miss) > 0 {
			out[k.String()] = miss
		}
	}
	return out
}

// DuplicateCount returns how many duplicate copies were suppressed.
func (n *Node) DuplicateCount() int { return n.dupCount }

// Broadcast constructs a new message originating at this node. seq is the
// node's next per-origin sequence number and is supplied by the engine.
func (n *Node) Broadcast(seq int, payload string) *Message {
	k := vec.Key{Node: n.ID, Seq: seq}
	deps := n.clock.Deps()
	return &Message{
		Origin:  k,
		Sender:  n.ID,
		From:    n.ID,
		Deps:    deps,
		Clock:   n.clock.Clone(),
		Payload: payload,
	}
}

// ready reports whether all non-self dependencies are satisfied and the
// predecessor from the same origin (seq-1) has been delivered. A message is
// not required to "depend on itself": the origin entry is checked via seq-1,
// as in standard causal-broadcast delivery.
func ready(clock vec.Clock, m *Message) []vec.Key {
	var miss []vec.Key
	for _, k := range m.Deps {
		if k.Node == m.Origin.Node {
			continue
		}
		if !clock.Has(k) {
			miss = append(miss, k)
		}
	}
	pred := vec.Key{Node: m.Origin.Node, Seq: m.Origin.Seq - 1}
	if pred.Seq > 0 && !clock.Has(pred) {
		miss = append(miss, pred)
	}
	sort.Slice(miss, func(i, j int) bool { return miss[i].Less(miss[j]) })
	return miss
}

// Handle processes one incoming copy at virtual time t. It returns the
// outcome, zero or more newly delivered messages (the incoming one first,
// then any buffered messages released by it, in causal-safe order), and a
// backpressure record when the outcome is Backpressure.
func (n *Node) Handle(t int64, m *Message) (Outcome, []*Message, *BackpressureEvent) {
	// 1. De-duplication: delivered origins are accepted at most once; a copy
	// already sitting in the buffer is also a duplicate.
	if n.delivered[m.Origin] || n.buffer[m.Origin] != nil {
		n.dupCount++
		return Duplicate, nil, nil
	}

	// 2. Causal readiness check against the delivered vector clock.
	miss := ready(n.clock, m)
	if len(miss) == 0 {
		delivered := n.deliver(m)
		delivered = append(delivered, n.flush()...)
		return Delivered, delivered, nil
	}

	// 3. Not ready: buffer, or reject with backpressure if the bounded
	// buffer is full. Rejection never mutates clock or buffer, so causal
	// order cannot be violated by a later retry.
	if n.cap > 0 && len(n.buffer) >= n.cap {
		bp := &BackpressureEvent{
			Time:      t,
			Node:      n.ID,
			Origin:    m.Origin,
			Capacity:  n.cap,
			WaitingOn: miss,
		}
		n.backpressure = append(n.backpressure, *bp)
		return Backpressure, nil, bp
	}

	cp := *m
	n.buffer[m.Origin] = &cp
	return Buffered, nil, nil
}

// HandleLocal delivers a message originated at this node. A sender always
// satisfies its own new message's dependencies; delivering it may also
// release buffered dependents via flush.
func (n *Node) HandleLocal(m *Message) []*Message {
	if n.delivered[m.Origin] || n.buffer[m.Origin] != nil {
		return nil
	}
	delivered := n.deliver(m)
	delivered = append(delivered, n.flush()...)
	return delivered
}

// deliver applies one ready message: observe it and return it.
func (n *Node) deliver(m *Message) []*Message {
	n.clock.Observe(m.Origin)
	n.delivered[m.Origin] = true
	return []*Message{m}
}

// flush repeatedly scans the buffer and delivers every message whose
// dependencies are now satisfied, until a scan delivers nothing. Within a
// scan the deterministic key order makes releases reproducible.
func (n *Node) flush() []*Message {
	var out []*Message
	for {
		var readyKeys []vec.Key
		for _, k := range n.BufferKeys() {
			if len(ready(n.clock, n.buffer[k])) == 0 {
				readyKeys = append(readyKeys, k)
			}
		}
		if len(readyKeys) == 0 {
			return out
		}
		for _, k := range readyKeys {
			m := n.buffer[k]
			delete(n.buffer, k)
			out = append(out, n.deliver(m)...)
		}
	}
}

// DeliveriesCount reports how many distinct messages have been delivered.
func (n *Node) DeliveriesCount() int { return len(n.delivered) }

// Has reports whether the node already delivered or currently buffers k.
// Used to suppress a backpressure-retry whose message arrived by another path.
func (n *Node) Has(k vec.Key) bool {
	return n.delivered[k] || n.buffer[k] != nil
}

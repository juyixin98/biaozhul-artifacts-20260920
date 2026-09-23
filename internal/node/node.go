package node

// Message is a causal-broadcast application message.
//
// ID is the globally unique "sender-seq" identifier (e.g. "A1" for the
// first message broadcast by node A). Sender is the global node index and
// Seq that sender's 1-based broadcast counter. Clock is the message's
// vector-clock dependency annotation.
type Message struct {
	ID     string
	Sender int
	Seq    int
	Clock  VC
	Body   string
}

// Buffered is a message held until its dependencies are satisfied.
type Buffered struct {
	Msg Message
}

// Outcome classifies what happened to one receive attempt.
type Outcome string

const (
	// OutcomeDelivered means the attempt delivered the message and
	// possibly released a cascade of previously buffered messages.
	OutcomeDelivered Outcome = "delivered"
	// OutcomeBuffered means the message was stored for later release.
	OutcomeBuffered Outcome = "buffered"
	// OutcomeDuplicate means the message was already delivered or buffered.
	OutcomeDuplicate Outcome = "duplicate"
	// OutcomeBackpressure means the buffer was full; nothing changed.
	OutcomeBackpressure Outcome = "backpressure"
)

// DeliveredMsg pairs a delivered message with the receiver's clock as it
// stood immediately after that delivery (head delivery plus each cascade
// release has its own snapshot).
type DeliveredMsg struct {
	Msg        Message
	ClockAfter VC
}

// ReceiveResult is the result of one Receive call. Delivered is populated
// only when Newly is OutcomeDelivered; its first element is the message
// passed into Receive and the rest are cascade releases from the buffer.
type ReceiveResult struct {
	Newly     Outcome
	Delivered []DeliveredMsg
}

// Node is one simulated process.
type Node struct {
	Index int
	Names []string // shared index->name table, used for diagnostics

	clock     VC
	delivered map[string]bool
	buf       map[string]Buffered
	bufCap    int // <= 0 means unbounded
	highWater int
}

// New builds a node. bufCap <= 0 means an unbounded buffer.
func New(index int, n int, bufCap int, names []string) *Node {
	return &Node{
		Index:     index,
		Names:     names,
		clock:     make(VC, n),
		delivered: make(map[string]bool),
		buf:       make(map[string]Buffered),
		bufCap:    bufCap,
	}
}

// Clock returns a copy of the current delivered vector clock.
func (n *Node) Clock() VC { return n.clock.Clone() }

// Delivered reports whether the message has already been delivered.
func (n *Node) Delivered(id string) bool { return n.delivered[id] }

// DeliveredCount is the total number of distinct messages delivered.
func (n *Node) DeliveredCount() int { return len(n.delivered) }

// InBuffer reports whether the message is currently buffered.
func (n *Node) InBuffer(id string) bool { _, ok := n.buf[id]; return ok }

// BufferedCount is the current number of held messages.
func (n *Node) BufferedCount() int { return len(n.buf) }

// HighWater is the largest the buffer ever grew.
func (n *Node) HighWater() int { return n.highWater }

// BufferCap returns the configured capacity (0 = unbounded).
func (n *Node) BufferCap() int { return n.bufCap }

// Snapshot returns copies of the buffered messages, deterministically
// ordered by (sender index, seq).
func (n *Node) Snapshot() []Buffered {
	out := make([]Buffered, 0, len(n.buf))
	for _, b := range n.buf {
		out = append(out, b)
	}
	// Simple insertion-style ordering; buffers are small in practice.
	for i := 1; i < len(out); i++ {
		for j := i; j > 0 && lessMsg(out[j].Msg, out[j-1].Msg); j-- {
			out[j], out[j-1] = out[j-1], out[j]
		}
	}
	return out
}

func lessMsg(a, b Message) bool {
	if a.Sender != b.Sender {
		return a.Sender < b.Sender
	}
	return a.Seq < b.Seq
}

// MissingDepsFor reports the unsatisfied predecessors of a buffered message.
func (n *Node) MissingDepsFor(id string) ([]Missing, bool) {
	b, ok := n.buf[id]
	if !ok {
		return nil, false
	}
	return MissingDeps(n.clock, b.Msg.Clock, b.Msg.Sender), true
}

// Receive processes one network arrival. State never changes on
// duplicate or backpressure outcomes, so a rejected retry is safe and
// causal order is preserved.
func (n *Node) Receive(m Message) ReceiveResult {
	// Duplicate suppression is checked first so that a re-sent copy of an
	// already-held message is never mistaken for a buffer overflow.
	if n.delivered[m.ID] || n.InBuffer(m.ID) {
		return ReceiveResult{Newly: OutcomeDuplicate}
	}
	if !Deliverable(n.clock, m.Clock, m.Sender) {
		if n.bufCap > 0 && len(n.buf) >= n.bufCap {
			return ReceiveResult{Newly: OutcomeBackpressure}
		}
		n.buf[m.ID] = Buffered{Msg: m}
		if len(n.buf) > n.highWater {
			n.highWater = len(n.buf)
		}
		return ReceiveResult{Newly: OutcomeBuffered}
	}
	n.markDelivered(m)
	delivered := []DeliveredMsg{{Msg: m, ClockAfter: n.clock.Clone()}}
	delivered = append(delivered, n.drainBuffer()...)
	return ReceiveResult{Newly: OutcomeDelivered, Delivered: delivered}
}

// drainBuffer repeatedly releases every currently deliverable buffered
// message. Each delivery advances the clock, which may unlock further
// messages, so the loop repeats until a pass releases nothing.
func (n *Node) drainBuffer() []DeliveredMsg {
	var released []DeliveredMsg
	for {
		var head Message
		found := false
		// Deterministic release order: lowest (sender, seq) among the
		// currently deliverable candidates.
		for _, b := range n.buf {
			if !Deliverable(n.clock, b.Msg.Clock, b.Msg.Sender) {
				continue
			}
			if !found || lessMsg(b.Msg, head) {
				head = b.Msg
				found = true
			}
		}
		if !found {
			return released
		}
		delete(n.buf, head.ID)
		n.markDelivered(head)
		released = append(released, DeliveredMsg{Msg: head, ClockAfter: n.clock.Clone()})
	}
}

func (n *Node) markDelivered(m Message) {
	n.delivered[m.ID] = true
	n.clock.Merge(m.Clock)
}

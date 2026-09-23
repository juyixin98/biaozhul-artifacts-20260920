package raft

import (
	"bytes"
	"container/heap"
	"math/rand"
	"sync"
)

// Timing constants in virtual milliseconds. Deterministic and tuned so a full
// election (timeout + request/response propagation) completes well inside
// 250ms of virtual time.
const (
	BaseElectionMs = 150
	HeartbeatMs    = 60
	NetDelayMs     = 4
)

// TimerKind distinguishes scheduled timers.
type TimerKind int

const (
	TimerElection TimerKind = iota
	TimerHeartbeat
)

// Event is a scheduled occurrence on the virtual clock.
type Event struct {
	At    int64
	Kind  TimerKind
	Node  int
	Gen   int
	Msg   *Message
	Index int // heap bookkeeping
}

type eventHeap []*Event

func (h eventHeap) Len() int            { return len(h) }
func (h eventHeap) Less(i, j int) bool  { return h[i].At < h[j].At }
func (h eventHeap) Swap(i, j int)       { h[i], h[j] = h[j], h[i]; h[i].Index, h[j].Index = j, i }
func (h *eventHeap) Push(x interface{}) { e := x.(*Event); e.Index = len(*h); *h = append(*h, e) }
func (h *eventHeap) Pop() interface{} {
	old := *h
	n := len(old)
	e := old[n-1]
	*h = old[:n-1]
	e.Index = -1
	return e
}

// Network models the in-memory network: uniform delay, a partition matrix
// (symmetric), and per-node stalls. While a node is stalled, messages TO it
// are held in a FIFO queue and released on Resume (they retain their original
// ordering, simulating delayed old messages); messages while partitioned are
// dropped.
type Network struct {
	delayNs  int64
	down     map[[2]int]bool
	stalled  map[int]bool
	held     map[int][]*Message
	holdFrom map[int]bool
	heldFrom map[int][]*Message
}

func newNetwork(delayMs int) *Network {
	return &Network{
		delayNs:  int64(delayMs) * 1_000_000,
		down:     map[[2]int]bool{},
		stalled:  map[int]bool{},
		held:     map[int][]*Message{},
		holdFrom: map[int]bool{},
		heldFrom: map[int][]*Message{},
	}
}

// Partition splits the cluster into groups: only pairs within the same group
// can communicate.
func (nw *Network) Partition(groups [][]int) {
	nw.down = map[[2]int]bool{}
	for i := range groups {
		for j := i + 1; j < len(groups); j++ {
			for _, a := range groups[i] {
				for _, b := range groups[j] {
					nw.down[[2]int{a, b}] = true
					nw.down[[2]int{b, a}] = true
				}
			}
		}
	}
}

// Heal clears all partitions.
func (nw *Network) Heal() { nw.down = map[[2]int]bool{} }

// IsDown reports whether the pair is currently partitioned.
func (nw *Network) IsDown(a, b int) bool { return nw.down[[2]int{a, b}] }

// Pause holds all messages addressed to id until Resume.
func (nw *Network) Pause(id int) { nw.stalled[id] = true }

// PauseFrom holds every message sent by id until ResumeFrom. This is how the
// harness creates deliberately delayed "old" messages: the sender keeps
// generating current-time traffic which is frozen and released later, after
// the cluster has moved on without it.
func (nw *Network) PauseFrom(id int) { nw.holdFrom[id] = true }

// ResumeFrom releases held outbound messages in FIFO order.
func (nw *Network) ResumeFrom(id int) []*Message {
	delete(nw.holdFrom, id)
	q := nw.heldFrom[id]
	nw.heldFrom[id] = nil
	return q
}

// HoldFrom reports whether outbound messages from id are frozen.
func (nw *Network) HoldFrom(id int) bool { return nw.holdFrom[id] }

// Resume releases held messages, returning them in FIFO order.
func (nw *Network) Resume(id int) []*Message {
	delete(nw.stalled, id)
	q := nw.held[id]
	nw.held[id] = nil
	return q
}

func (nw *Network) Stalled(id int) bool { return nw.stalled[id] }

// HeldCount reports how many messages are queued behind a paused node.
func (nw *Network) HeldCount(id int) int { return len(nw.held[id]) }

// TraceEvent is one observable simulator step, for replay and debugging.
type TraceEvent struct {
	At     int64       `json:"at"`
	Type   string      `json:"type"`
	From   int         `json:"from,omitempty"`
	To     int         `json:"to,omitempty"`
	Term   int         `json:"term,omitempty"`
	Kind   string      `json:"kind,omitempty"`
	Detail string      `json:"detail,omitempty"`
	Msg    interface{} `json:"msg,omitempty"`
}

// Simulator drives a set of nodes on a virtual clock.
type Simulator struct {
	ids     []int
	variant Variant
	nodes   map[int]*Node
	alive   map[int]bool
	h       eventHeap
	now     int64
	rng     *rand.Rand
	nw      *Network

	// disks[id] is the last persisted bytes; restart rebuilds from it.
	disks map[int]*bytes.Buffer

	traceOn bool
	Trace   []TraceEvent

	mu sync.Mutex
}

// MuLock/MuUnlock serialise concurrent HTTP-driven mutations on one
// simulator. The deterministic test harness runs single-threaded and does not
// take the lock.
func (s *Simulator) MuLock()   { s.mu.Lock() }
func (s *Simulator) MuUnlock() { s.mu.Unlock() }

// TraceCopy returns a defensive copy of the recorded trace events.
func (s *Simulator) TraceCopy() []TraceEvent {
	if !s.traceOn {
		return nil
	}
	out := make([]TraceEvent, len(s.Trace))
	copy(out, s.Trace)
	return out
}

// NewSimulator builds a fresh cluster and schedules the first election timers.
func NewSimulator(ids []int, variant Variant, seed int64) *Simulator {
	cfg := Config{IDs: ids, Variant: variant}
	s := &Simulator{
		ids:     append([]int(nil), ids...),
		variant: variant,
		nodes:   map[int]*Node{},
		alive:   map[int]bool{},
		rng:     rand.New(rand.NewSource(seed)),
		nw:      newNetwork(NetDelayMs),
		disks:   map[int]*bytes.Buffer{},
	}
	heap.Init(&s.h)
	for _, id := range ids {
		n := New(id, cfg)
		s.nodes[id] = n
		s.alive[id] = true
		s.persist(id)
		s.scheduleElection(id)
	}
	return s
}

// EnableTrace starts recording observable events (off during enumeration for
// speed; on for interactive sessions and counterexample replays).
func (s *Simulator) EnableTrace() { s.traceOn = true }

func (s *Simulator) log(ev TraceEvent) {
	if s.traceOn {
		s.Trace = append(s.Trace, ev)
	}
}

// Now returns virtual time in nanoseconds.
func (s *Simulator) Now() int64 { return s.now }

// IDs returns the sorted node ids.
func (s *Simulator) IDs() []int { return append([]int(nil), s.ids...) }

// Network exposes fault controls.
func (s *Simulator) Network() *Network { return s.nw }

func (s *Simulator) persist(id int) {
	buf := &bytes.Buffer{}
	if err := s.nodes[id].Persist(buf); err != nil {
		panic(err) // in-memory writer cannot fail
	}
	s.disks[id] = buf
}

// scheduleElection arms node id's election timer with freshly jittered timeout.
func (s *Simulator) scheduleElection(id int) {
	n := s.nodes[id]
	n.electionGen++
	timeout := ElectionTimeout(s.rng, BaseElectionMs)
	heap.Push(&s.h, &Event{
		At: s.now + timeout, Kind: TimerElection, Node: id, Gen: n.electionGen,
	})
}

func (s *Simulator) scheduleHeartbeat(id int) {
	n := s.nodes[id]
	n.heartbeatGen++
	heap.Push(&s.h, &Event{
		At: s.now + HeartbeatMs*1_000_000, Kind: TimerHeartbeat, Node: id, Gen: n.heartbeatGen,
	})
}

// send enqueues a message: dropped under partition, held while the recipient
// is paused, otherwise scheduled with the uniform network delay.
func (s *Simulator) send(m Message) {
	if s.nw.IsDown(m.From, m.To) {
		s.log(TraceEvent{At: s.now, Type: "drop", From: m.From, To: m.To, Kind: m.Kind.String(), Term: m.Term})
		return
	}
	if s.nw.Stalled(m.To) {
		s.nw.held[m.To] = append(s.nw.held[m.To], &m)
		s.log(TraceEvent{At: s.now, Type: "hold", From: m.From, To: m.To, Kind: m.Kind.String(), Term: m.Term})
		return
	}
	if s.nw.HoldFrom(m.From) {
		s.nw.heldFrom[m.From] = append(s.nw.heldFrom[m.From], &m)
		s.log(TraceEvent{At: s.now, Type: "hold-from", From: m.From, To: m.To, Kind: m.Kind.String(), Term: m.Term})
		return
	}
	mm := m
	mm.Deliver = s.now + s.nw.delayNs
	heap.Push(&s.h, &Event{At: mm.Deliver, Msg: &mm})
}

func (s *Simulator) emitAll(ms []Message) {
	for i := range ms {
		s.send(ms[i])
	}
}

// Advance processes events with timestamps <= now+durationNs, then moves the
// clock. onEvent (when non-nil) is invoked after each message delivery with
// the simulator so an external checker can assert invariants per event.
func (s *Simulator) Advance(durationNs int64, onEvent func(*Simulator)) {
	deadline := s.now + durationNs
	for s.h.Len() > 0 && s.h[0].At <= deadline {
		ev := heap.Pop(&s.h).(*Event)
		s.now = ev.At
		if ev.Msg != nil {
			s.deliver(ev.Msg)
			if onEvent != nil {
				onEvent(s)
			}
		} else {
			s.handleTimer(ev)
		}
	}
	s.now = deadline
}

func (s *Simulator) handleTimer(ev *Event) {
	id := ev.Node
	if !s.alive[id] {
		return
	}
	n := s.nodes[id]
	switch ev.Kind {
	case TimerElection:
		if ev.Gen != n.electionGen || n.role == Leader {
			return
		}
		out := n.startElection(nil, s.now, s.nw.delayNs)
		s.persist(id)
		s.log(TraceEvent{At: s.now, Type: "election", From: id, Term: n.Term()})
		s.emitAll(out)
		// Candidate re-arms its election timer in case the election stalls.
		s.scheduleElection(id)
	case TimerHeartbeat:
		if ev.Gen != n.heartbeatGen || n.role != Leader {
			return
		}
		s.emitAll(n.appendToAll(s.now, s.nw.delayNs))
		s.scheduleHeartbeat(id)
	}
}

func (s *Simulator) deliver(m *Message) {
	if !s.alive[m.To] {
		s.log(TraceEvent{At: s.now, Type: "drop-dead", From: m.From, To: m.To, Kind: m.Kind.String()})
		return
	}
	// A partition installed after the message was sent still drops it.
	if s.nw.IsDown(m.From, m.To) {
		s.log(TraceEvent{At: s.now, Type: "drop", From: m.From, To: m.To, Kind: m.Kind.String(), Term: m.Term})
		return
	}
	switch m.Kind {
	case MsgVoteReq:
		s.log(TraceEvent{At: s.now, Type: "recv", From: m.From, To: m.To, Kind: m.Kind.String(), Term: m.Term, Msg: viewVote(*m.Vote)})
	case MsgVoteResp:
		s.log(TraceEvent{At: s.now, Type: "recv", From: m.From, To: m.To, Kind: m.Kind.String(), Term: m.Term, Msg: viewVote(*m.Vote)})
	case MsgAppendReq:
		s.log(TraceEvent{At: s.now, Type: "recv", From: m.From, To: m.To, Kind: m.Kind.String(), Term: m.Term, Msg: viewAppend(*m.Append)})
	case MsgAppendResp:
		s.log(TraceEvent{At: s.now, Type: "recv", From: m.From, To: m.To, Kind: m.Kind.String(), Term: m.Term})
	}
	s.dispatch(m, false)
}

// ClientCommand proposes cmd to id; messages are produced only if it is the
// leader. Returns whether it was accepted.
func (s *Simulator) ClientCommand(id int, cmd string) bool {
	if !s.alive[id] || s.nodes[id].Role() != Leader {
		return false
	}
	out := s.nodes[id].ClientCommand(cmd, s.now, s.nw.delayNs)
	s.persist(id)
	s.log(TraceEvent{At: s.now, Type: "propose", From: id, Term: s.nodes[id].Term(), Detail: cmd})
	s.emitAll(out)
	return true
}

// Leader returns the current-term leader id, or -1 if none is known (checks
// only live leaders).
func (s *Simulator) Leader() int {
	for _, id := range s.ids {
		if s.alive[id] && s.nodes[id].Role() == Leader {
			return id
		}
	}
	return -1
}

// LeadersByTerm returns every live leader keyed by its term — the enumerator
// uses it to assert at most one leader per term.
func (s *Simulator) LeadersByTerm() map[int]int {
	out := map[int]int{}
	for _, id := range s.ids {
		if s.alive[id] && s.nodes[id].Role() == Leader {
			out[s.nodes[id].Term()] = id
		}
	}
	return out
}

// Restart kills a node and immediately restores it from persisted state; the
// process loses all volatile state (role, commit index advanced beyond disk,
// timers) while retaining term/vote/log.
func (s *Simulator) Restart(id int) {
	if !s.alive[id] {
		return
	}
	// Ensure current durable state is on "disk".
	s.persist(id)
	delete(s.nodes, id)
	delete(s.alive, id)

	n, err := Restore(id, Config{IDs: s.ids, Variant: s.variant}, bytes.NewReader(s.disks[id].Bytes()))
	if err != nil {
		panic(err)
	}
	// Restored commit index must not exceed what was applied/persisted:
	// commitIndex is volatile, so a fresh follower starts at 0 and catches up.
	n.ResetVolatile()
	s.nodes[id] = n
	s.alive[id] = true
	s.log(TraceEvent{At: s.now, Type: "restart", To: id, Term: n.Term()})
	s.scheduleElection(id)
}

// ResetVolatile models loss of RAM on restart: follower, no known leader,
// commit/lastApplied reset, but the persisted log is retained and the KV
// machine is rebuilt lazily as entries re-commit.
func (n *Node) ResetVolatile() {
	n.role = Follower
	n.leaderID = -1
	n.commitIdx = 0
	n.lastApplied = 0
	n.kv = map[string]string{}
	n.votes = map[int]bool{}
	n.matchIndex = map[int]int{}
	n.nextIndex = map[int]int{}
	n.electionGen++
	n.heartbeatGen++
}

// Pause holds all messages addressed to id (fault-injection entry point).
func (s *Simulator) Pause(id int) {
	s.nw.Pause(id)
	s.log(TraceEvent{At: s.now, Type: "pause", To: id})
}

// PauseFrom freezes outbound messages from id to manufacture delayed old msgs.
func (s *Simulator) PauseFrom(id int) {
	s.nw.PauseFrom(id)
	s.log(TraceEvent{At: s.now, Type: "pause-from", From: id})
}

// ResumeFrom releases frozen outbound messages.
func (s *Simulator) ResumeFrom(id int) {
	queued := s.nw.ResumeFrom(id)
	for _, m := range queued {
		mm := *m
		mm.Deliver = s.now + s.nw.delayNs
		s.log(TraceEvent{At: s.now, Type: "release-from", From: mm.From, To: mm.To, Kind: mm.Kind.String(), Term: mm.Term})
		heap.Push(&s.h, &Event{At: mm.Deliver, Msg: &mm})
	}
}

// Resume releases messages held behind a paused node. The messages keep their
// stale contents (including old terms), which exercises delayed-old-message
// handling.
func (s *Simulator) Resume(id int) {
	queued := s.nw.Resume(id)
	for _, m := range queued {
		mm := *m
		mm.Deliver = s.now + s.nw.delayNs
		s.log(TraceEvent{At: s.now, Type: "release", From: mm.From, To: mm.To, Kind: mm.Kind.String(), Term: mm.Term})
		heap.Push(&s.h, &Event{At: mm.Deliver, Msg: &mm})
	}
}

// InjectMessage delivers m to its recipient immediately, bypassing the
// network entirely (fault-injection test hook for "an old delayed message
// finally arrives" regardless of the current partition state).
func (s *Simulator) InjectMessage(m Message) {
	s.log(TraceEvent{At: s.now, Type: "inject", From: m.From, To: m.To, Kind: m.Kind.String(), Term: m.Term})
	s.dispatch(&m, true)
}

// dispatch applies one RPC to its recipient. direct=true skips alive and
// partition checks (message injection); regular network deliveries use false.
func (s *Simulator) dispatch(m *Message, direct bool) {
	dst := s.nodes[m.To]
	if dst == nil {
		return
	}
	beforeTerm := dst.Term()
	switch m.Kind {
	case MsgVoteReq:
		reply := dst.HandleRequestVote(*m.Vote, s.now, s.nw.delayNs)
		if dst.Term() != beforeTerm || (reply != nil && reply.Vote.Grant) {
			s.persist(m.To)
		}
		if reply != nil && reply.Vote.Grant {
			s.scheduleElection(m.To)
		}
		if reply != nil {
			s.send(*reply)
		}
	case MsgVoteResp:
		out := dst.handleVoteResponse(*m, s.now, s.nw.delayNs)
		if dst.Role() == Leader {
			s.log(TraceEvent{At: s.now, Type: "become-leader", To: m.To, Term: dst.Term()})
			s.scheduleHeartbeat(m.To)
		}
		s.emitAll(out)
	case MsgAppendReq:
		reply := dst.HandleAppendEntries(*m.Append, s.now, s.nw.delayNs)
		if dst.Term() != beforeTerm {
			s.persist(m.To)
		}
		if reply != nil && reply.Append.Success {
			s.persist(m.To)
			s.scheduleElection(m.To)
		}
		if reply != nil {
			s.send(*reply)
		}
	case MsgAppendResp:
		out := dst.handleAppendResponse(*m, s.now, s.nw.delayNs)
		if dst.Term() != beforeTerm {
			s.persist(m.To)
			s.scheduleElection(m.To)
		}
		s.emitAll(out)
	}
}

// NodeView exposes a node for snapshots and the checker.
func (s *Simulator) Node(id int) *Node { return s.nodes[id] }

// Alive reports liveness.
func (s *Simulator) Alive(id int) bool { return s.alive[id] }

// CommittedPrefix returns the committed log entries of a node.
func (s *Simulator) CommittedPrefix(id int) []LogEntry {
	n := s.nodes[id]
	out := make([]LogEntry, 0, n.commitIdx)
	for i := 1; i <= n.commitIdx && i < len(n.log); i++ {
		out = append(out, n.log[i])
	}
	return out
}

// Package sim implements a deterministic, single-process discrete event
// simulator for a fenced-lease lock: a lock service that hands out leases with
// monotonically increasing fencing tokens, clients that renew by heartbeat,
// and a protected resource that rejects writes carrying stale tokens.
//
// There are no real sockets, goroutines (for simulation logic) or clocks:
// every node runs at logical times chosen by the Engine, and message delivery
// goes through an injectable Network model that can drop, duplicate, delay
// and reorder packets.
package sim

import (
	"container/heap"
	"fmt"
	"sort"
)

// Time is logical simulation time, in milliseconds. Time only moves forward.
type Time int64

// Kind selects the concrete payload of an Event.
type Kind int

const (
	KindWakeup  Kind = iota // internal: a paused node's resume boundary (runs first at t)
	KindMessage             // a network message is delivered to Dst
	KindTimer               // a named timer fires for its node
	KindAction              // a scripted client action runs
)

// Event is one item in the engine's event queue.
type Event struct {
	Time Time
	Kind Kind

	Dst   string // KindMessage / KindAction: target node
	Msg   *Envelope
	Node  string // KindTimer: owning node
	Name  string // KindTimer: timer name
	Act   *ActionSpec
	Timer int64 // KindTimer: per-(node,name) version, for lazy cancellation

	seq int64 // insertion sequence; tie-breaks equal (Time, Kind) for determinism
}

type eventHeap []*Event

func (h eventHeap) Len() int { return len(h) }
func (h eventHeap) Less(i, j int) bool {
	if h[i].Time != h[j].Time {
		return h[i].Time < h[j].Time
	}
	if h[i].Kind != h[j].Kind {
		return h[i].Kind < h[j].Kind
	}
	return h[i].seq < h[j].seq
}
func (h eventHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }
func (h *eventHeap) Push(x any)   { *h = append(*h, x.(*Event)) }
func (h *eventHeap) Pop() any {
	old := *h
	n := len(old)
	e := old[n-1]
	old[n-1] = nil
	*h = old[:n-1]
	return e
}

// Node is implemented by every simulated node (client, lock service, resource).
type Node interface {
	ID() string
	// HandleMessage processes a delivered network message.
	HandleMessage(h Host, t Time, msg *Envelope)
	// OnTimer processes a named timer fired by the engine.
	OnTimer(h Host, t Time, name string)
	// OnAction processes a scripted scenario action.
	OnAction(h Host, t Time, a *ActionSpec)
	// OnPause / OnResume notify the node that it was paused/resumed. Its
	// pending timers and inbound messages are frozen over the interval.
	OnPause(h Host, t Time)
	OnResume(h Host, t Time)
}

// Host is the engine-facing API a node may call while it is being dispatched.
type Host interface {
	Now() Time
	Send(msg *Envelope)
	SetTimer(node, name string, at Time)
	CancelTimer(node, name string)
	Log(event string, detail map[string]any)
	PauseNode(id string, until Time)
	ResumeNode(id string)
	Rng() *Rng
}

// Recorder receives trace events, in simulation order.
type Recorder interface {
	Record(t Time, node, event string, detail map[string]any)
}

type pauseState struct {
	paused bool
	until  Time
	held   []*Event // events whose scheduled time fell inside the pause
}

// Engine is the simulation kernel.
type Engine struct {
	now   Time
	queue eventHeap
	seq   int64
	event int64 // processed-event counter, included in every trace record

	nodes   map[string]Node
	pauses  map[string]*pauseState
	net     *Network
	rec     Recorder
	rng     *Rng
	maxTime Time

	// timers holds the current version of each named timer; scheduled events
	// carrying an older version are stale and dropped (lazy cancellation).
	timers map[[2]string]timerEntry
}

type timerEntry struct {
	version int64
	at      Time
}

// NewEngine builds an engine. seed fully determines probabilistic faults.
func NewEngine(net *Network, rec Recorder, seed uint64, maxTime Time) *Engine {
	e := &Engine{
		nodes:   map[string]Node{},
		pauses:  map[string]*pauseState{},
		timers:  map[[2]string]timerEntry{},
		net:     net,
		rec:     rec,
		rng:     NewRng(seed),
		maxTime: maxTime,
	}
	heap.Init(&e.queue)
	return e
}

// AddNode registers a node; must be called before Run.
func (e *Engine) AddNode(n Node) {
	if _, dup := e.nodes[n.ID()]; dup {
		panic("duplicate node " + n.ID())
	}
	e.nodes[n.ID()] = n
	e.pauses[n.ID()] = &pauseState{}
}

func (e *Engine) Now() Time { return e.now }

func (e *Engine) Rng() *Rng { return e.rng }

func (e *Engine) Log(event string, detail map[string]any) {
	e.record(e.now, "", event, detail)
}

func (e *Engine) record(t Time, node, event string, detail map[string]any) {
	if e.rec != nil {
		e.rec.Record(t, node, event, detail)
	}
}

// SetTimer schedules a named timer for node at time at, replacing any current
// timer with the same (node, name).
func (e *Engine) SetTimer(node, name string, at Time) {
	k := [2]string{node, name}
	cur := e.timers[k]
	cur.version++
	cur.at = at
	e.timers[k] = cur
	ev := &Event{
		Time:  at,
		Kind:  KindTimer,
		Node:  node,
		Name:  name,
		Timer: cur.version,
		seq:   e.nextSeq(),
	}
	heap.Push(&e.queue, ev)
}

func (e *Engine) CancelTimer(node, name string) {
	k := [2]string{node, name}
	if c, ok := e.timers[k]; ok {
		c.version++
		e.timers[k] = c
	}
}

// nextSeq hands out a monotonic id for deterministic tie-breaking.
func (e *Engine) nextSeq() int64 {
	e.seq++
	return e.seq
}

// PauseNode pauses node id: events due before until are held back, then
// delivered at the resume time. Idempotent: a pause already covering until
// is a no-op; re-pausing extends the interval.
func (e *Engine) PauseNode(id string, until Time) {
	p := e.pauses[id]
	if !p.paused {
		p.paused = true
		p.until = until
		if n, ok := e.nodes[id]; ok {
			n.OnPause(e, e.now)
		}
		// Schedule the exact resume boundary so the node wakes at `until`
		// even if no other event happens at that time.
		heap.Push(&e.queue, &Event{
			Time: until, Kind: KindWakeup, Node: id, Dst: id, seq: e.nextSeq(),
		})
		e.record(e.now, id, "node.pause", map[string]any{"until": until, "duration": until - e.now})
		return
	}
	if until > p.until {
		p.until = until
	}
}

// ResumeNode ends a pause immediately (at h.Now()). If the node is not
// paused it is a no-op, so resume actions are harmless if the scheduled
// pause boundary already elapsed.
func (e *Engine) ResumeNode(id string) {
	p := e.pauses[id]
	if p == nil || !p.paused {
		return
	}
	e.resumeNode(e.now, p, id)
}

// resumeNode ends a pause and queues held events for delivery now, preserving
// their original order.
func (e *Engine) resumeNode(t Time, p *pauseState, id string) {
	if !p.paused {
		return
	}
	p.paused = false
	p.until = 0
	held := p.held
	p.held = nil
	for _, ev := range held {
		ev.Time = t
		ev.seq = e.nextSeq()
		heap.Push(&e.queue, ev)
	}
	if n, ok := e.nodes[id]; ok {
		n.OnResume(e, t)
	}
	e.record(t, id, "node.resume", map[string]any{"held_delivered": len(held)})
}

// dispatch holds or delivers an event depending on the destination pause.
// pause/resume actions bypass the hold: they control the pause itself and
// must run even while the node is frozen.
func (e *Engine) dispatch(ev *Event, held bool) {
	var dst string
	switch ev.Kind {
	case KindMessage:
		dst = ev.Dst
	case KindAction:
		dst = ev.Dst
		if ev.Act != nil && (ev.Act.Op == OpPause || ev.Act.Op == OpResume) {
			held = false
		}
	case KindTimer:
		dst = ev.Node
	case KindWakeup:
		// Pure boundary marker; the resume loop in Run acts on it.
		return
	}
	p := e.pauses[dst]
	if p != nil && p.paused && held {
		p.held = append(p.held, ev)
		return
	}
	e.deliver(ev)
}

func (e *Engine) deliver(ev *Event) {
	n, ok := e.nodes[ev.Dst]
	if !ok {
		n, ok = e.nodes[ev.Node]
	}
	if !ok {
		return
	}
	e.event++
	switch ev.Kind {
	case KindMessage:
		n.HandleMessage(e, ev.Time, ev.Msg)
	case KindTimer:
		// Drop stale (cancelled/replaced) timer versions.
		if c, ok := e.timers[[2]string{ev.Node, ev.Name}]; !ok || c.version != ev.Timer {
			return
		}
		n.OnTimer(e, ev.Time, ev.Name)
	case KindAction:
		n.OnAction(e, ev.Time, ev.Act)
	}
}

// scheduleAction enqueues a scripted action from the scenario.
func (e *Engine) scheduleAction(a *ActionSpec) {
	ev := &Event{Time: a.At, Kind: KindAction, Dst: a.Client, Act: a, seq: e.nextSeq()}
	heap.Push(&e.queue, ev)
}

// Send hands an outbound message to the network model and schedules its
// delivery (or deliveries, for duplicates). Messages sent while the sender
// is paused are dropped: a frozen node cannot produce traffic.
func (e *Engine) Send(msg *Envelope) {
	msg.ID = e.nextSeq()
	msg.SendTime = e.now
	senderPaused := e.pauses[msg.Src] != nil && e.pauses[msg.Src].paused
	offers := e.net.Transmit(e, msg, senderPaused)
	for _, o := range offers {
		ev := &Event{
			Time: o.deliverAt,
			Kind: KindMessage,
			Dst:  msg.Dst,
			Msg:  o.msg,
			seq:  e.nextSeq(),
		}
		heap.Push(&e.queue, ev)
	}
}

// Run drains the event queue until maxTime, then returns the number of events
// processed (including held-but-never-delivered events at horizon? no: only
// dispatched events are counted).
func (e *Engine) Run(actions []*ActionSpec) (processed int64, err error) {
	for _, a := range actions {
		if e.nodes[a.Client] == nil {
			return 0, fmt.Errorf("action at t=%d: unknown client %q", a.At, a.Client)
		}
		e.scheduleAction(a)
	}

	for e.queue.Len() > 0 {
		ev := heap.Pop(&e.queue).(*Event)
		if ev.Time > e.maxTime {
			break
		}
		e.now = ev.Time

		// Complete any pause whose resume time has arrived. Resuming may
		// enqueue held events at this same time; iterate node ids in sorted
		// order so multi-node resumes are deterministic (map order is not).
		for _, id := range e.pausedNodeIDs() {
			p := e.pauses[id]
			if p.paused && p.until <= e.now {
				e.resumeNode(e.now, p, id)
			}
		}

		e.dispatch(ev, true)
		processed = e.event
	}

	// Nodes still paused at horizon are resumed for snapshot purposes only.
	for _, id := range e.pausedNodeIDs() {
		p := e.pauses[id]
		if p.paused {
			e.now = p.until
			e.resumeNode(e.now, p, id)
		}
	}
	return processed, nil
}

// pausedNodeIDs returns registered node ids in sorted order.
func (e *Engine) pausedNodeIDs() []string {
	ids := make([]string, 0, len(e.nodes))
	for id := range e.nodes {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

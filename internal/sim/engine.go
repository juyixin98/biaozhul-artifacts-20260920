// Package sim is a deterministic discrete-event simulation engine.
//
// Time is an integer tick counter owned entirely by the Engine: no node may
// read a wall clock. Nodes communicate only through Message events; timers are
// scheduled with At/After. The Network layer sits in front of delivery and may
// drop, duplicate, delay or reorder messages. Pausing a node holds every event
// destined for it (incoming network messages and its own timers) until resume,
// which models a long GC pause / process stall: the rest of the world keeps
// moving and lock leases can expire while the node is frozen.
package sim

import (
	"container/heap"
	"fmt"
)

// Message is the uniform envelope exchanged between handlers.
type Message struct {
	From    string         // sender node id
	To      string         // recipient node id
	Method  string         // application-level dispatch key
	Body    map[string]any // application payload
	Attempt int            // set by Network for duplicates (1 = original)
}

// Event is an item in the simulation queue. It is either a message delivery
// (Msg != nil) or a timer firing (Kind == "timer").
type Event struct {
	Time int64
	Seq  int64
	Kind string // "message" or "timer"
	Msg  *Message
	Dest string
	Name string // timer name / control tag
	Data map[string]any
}

// Handler is implemented by every simulated node or service.
type Handler interface {
	NodeID() string
	Handle(env *Env, ev *Event)
}

// Env is the interface a Handler uses to interact with the world.
type Env struct {
	eng    *Engine
	SelfID string
}

// Now returns the current simulated time.
func (e *Env) Now() int64 { return e.eng.Now() }

// Send hands a message to the network for eventual delivery.
func (e *Env) Send(m *Message) { e.eng.Send(m) }

// At schedules a named timer at an absolute time. A timer with the same name
// scheduled for the same node before the old one fires implicitly cancels the
// old one; use unique names for independent timers.
func (e *Env) At(t int64, name string, data map[string]any) {
	e.eng.Schedule(t, e.SelfID, name, data)
}

// After schedules a named timer relative to now.
func (e *Env) After(d int64, name string, data map[string]any) {
	e.At(e.Now()+d, name, data)
}

// CancelTimers removes all pending timers with the given name for this node.
func (e *Env) CancelTimers(name string) { e.eng.CancelTimers(e.SelfID, name) }

// Pause freezes this node: every event bound for it is held until Resume.
func (e *Env) Pause(until int64) { e.eng.Pause(e.SelfID, until) }

// Resume unpauses another node, releasing its held events at the current time.
func (e *Env) Resume(node string) { e.eng.resume(node) }

// AddRule installs a network fault rule at the current point in time.
func (e *Env) AddRule(r *Rule) { e.eng.net.AddRule(r) }

// Record appends a trace record for this node.
func (e *Env) Record(recType string, fields map[string]any) {
	e.eng.Record(e.SelfID, recType, fields)
}

// Rng returns the engine's deterministic random source.
func (e *Env) Rng() *Rng { return e.eng.rng }

// ---------------------------------------------------------------------------
// Record
// ---------------------------------------------------------------------------

// Record is one trace entry. Maps serialize directly to JSON.
type Record struct {
	Time   int64          `json:"time"`
	Node   string         `json:"node"`
	Type   string         `json:"type"`
	Fields map[string]any `json:"fields"`
}

// ---------------------------------------------------------------------------
// Event heap
// ---------------------------------------------------------------------------

type eventHeap []*Event

func (h eventHeap) Len() int { return len(h) }
func (h eventHeap) Less(i, j int) bool {
	if h[i].Time != h[j].Time {
		return h[i].Time < h[j].Time
	}
	return h[i].Seq < h[j].Seq
}
func (h eventHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }
func (h *eventHeap) Push(x any)   { *h = append(*h, x.(*Event)) }
func (h *eventHeap) Pop() any {
	old := *h
	n := len(old)
	it := old[n-1]
	*h = old[:n-1]
	return it
}

// ---------------------------------------------------------------------------
// Engine
// ---------------------------------------------------------------------------

// Engine owns the clock, the event queue, the node table and the network.
type Engine struct {
	hq     eventHeap
	seq    int64
	now    int64
	nodes  map[string]Handler
	net    *Network
	rng    *Rng
	traces []Record
	ended  bool

	paused map[string]bool
	until  map[string]int64
	held   map[string][]*Event
}

// New creates an engine. seed drives the deterministic RNG used by the network
// and by application workloads.
func New(seed int64, cfg NetConfig) *Engine {
	e := &Engine{
		nodes:  map[string]Handler{},
		paused: map[string]bool{},
		until:  map[string]int64{},
		held:   map[string][]*Event{},
	}
	e.rng = NewRng(seed)
	e.net = NewNetwork(e, cfg)
	return e
}

// Register adds a handler. Must be called before Run.
func (e *Engine) Register(h Handler) {
	if _, dup := e.nodes[h.NodeID()]; dup {
		panic(fmt.Sprintf("sim: duplicate node id %q", h.NodeID()))
	}
	e.nodes[h.NodeID()] = h
}

// Rng exposes the deterministic RNG to the runner.
func (e *Engine) Rng() *Rng { return e.rng }

// Network exposes the network so rules can be added at runtime.
func (e *Engine) Network() *Network { return e.net }

// Now returns the current simulated time.
func (e *Engine) Now() int64 { return e.now }

// Records returns a copy of all trace records collected so far.
func (e *Engine) Records() []Record {
	out := make([]Record, len(e.traces))
	copy(out, e.traces)
	return out
}

// Record appends a trace record attributed to node.
func (e *Engine) Record(node, recType string, fields map[string]any) {
	if fields == nil {
		fields = map[string]any{}
	}
	e.traces = append(e.traces, Record{Time: e.now, Node: node, Type: recType, Fields: fields})
}

// push queues an event, stamping it with the next global sequence number so
// events at the same timestamp have a total, insertion-derived order.
func (e *Engine) push(ev *Event) {
	e.seq++
	ev.Seq = e.seq
	heap.Push(&e.hq, ev)
}

// Send routes a message through the network; the network decides when (and
// whether) the delivery event is queued.
func (e *Engine) Send(m *Message) {
	if m.Attempt == 0 {
		m.Attempt = 1
	}
	e.net.Route(m)
}

// deliver is called by the Network for each copy of a message it releases.
func (e *Engine) deliver(at int64, m *Message) {
	ev := &Event{Time: at, Kind: "message", Msg: m, Dest: m.To}
	if e.paused[m.To] {
		e.held[m.To] = append(e.held[m.To], ev)
		return
	}
	e.push(ev)
}

// Schedule queues a timer for node.
func (e *Engine) Schedule(t int64, node, name string, data map[string]any) {
	if t < e.now {
		t = e.now
	}
	ev := &Event{Time: t, Kind: "timer", Dest: node, Name: name, Data: data}
	if e.paused[node] {
		e.held[node] = append(e.held[node], ev)
		return
	}
	e.push(ev)
}

// CancelTimers drops queued, not-yet-fired timers with the given name.
func (e *Engine) CancelTimers(node, name string) {
	out := e.hq[:0]
	for _, ev := range e.hq {
		if ev.Kind == "timer" && ev.Dest == node && ev.Name == name {
			continue
		}
		out = append(out, ev)
	}
	e.hq = out
	heap.Init(&e.hq)
}

// Pause freezes node and schedules its automatic resume at until. Events for a
// paused node are buffered (in arrival order) and released at resume.
func (e *Engine) Pause(node string, until int64) {
	if e.paused[node] {
		return
	}
	e.paused[node] = true
	e.until[node] = until
	if until > e.now {
		e.push(&Event{Time: until, Kind: "control", Dest: node, Name: "resume"})
	}
	e.Record(node, "node.paused", map[string]any{"until": until, "duration": until - e.now})
}

// Resume releases a paused node immediately (an explicit resume action).
func (e *Engine) Resume(node string) { e.resume(node) }

// resume releases all events held during a pause. Buffered events keep their
// original (time, arrival) order but are all re-enqueued at the resume time:
// a message whose send time predates the pause now visibly "arrives late".
func (e *Engine) resume(node string) {
	if !e.paused[node] {
		return
	}
	e.paused[node] = false
	delete(e.until, node)
	held := e.held[node]
	delete(e.held, node)
	for _, ev := range held {
		ev.Time = e.now
		ev.Seq = 0
		e.push(ev)
	}
	e.Record(node, "node.resumed", map[string]any{"held_events": len(held)})
}

// IsPaused reports whether node is currently frozen.
func (e *Engine) IsPaused(node string) bool { return e.paused[node] }

// End terminates the run at the current time even if events remain.
func (e *Engine) End() { e.ended = true }

// Run advances the simulation until no events remain, maxTime is reached
// (timers exactly at maxTime still fire), or End is called.
func (e *Engine) Run(maxTime int64) {
	for e.hq.Len() > 0 && !e.ended {
		ev := heap.Pop(&e.hq).(*Event)
		if ev.Time > maxTime {
			break
		}
		e.now = ev.Time

		// Auto-resume is an engine event: it must fire even though the
		// destination is currently paused.
		if ev.Kind == "control" && ev.Name == "resume" {
			if e.paused[ev.Dest] && e.until[ev.Dest] == ev.Time {
				e.resume(ev.Dest)
			}
			continue
		}

		// Defensive: nothing should reach here for a paused node because
		// deliver/Schedule buffer it, but keep the invariant explicit.
		if e.paused[ev.Dest] {
			e.held[ev.Dest] = append(e.held[ev.Dest], ev)
			continue
		}

		h := e.nodes[ev.Dest]
		if h == nil {
			e.Record(ev.Dest, "sim.unknown_destination", map[string]any{"kind": ev.Kind, "method": ev.Name})
			continue
		}
		h.Handle(&Env{eng: e, SelfID: ev.Dest}, ev)
	}
}

// Package engine implements a deterministic single-process discrete-event
// simulator: a virtual clock, a priority queue of events, an unreliable
// network (loss / duplication / reordering via delay jitter) and crash /
// restart primitives. There are no goroutines, no sockets and no wall clock:
// a run with the same scenario and seed always produces the same trace.
package engine

import (
	"container/heap"
	"errors"
	"fmt"
	"math/rand"
)

// CrashSentinel is the panic value used by a milestone hook to abort the
// remainder of the currently executing handler immediately after a crash has
// been performed. Any durable write above the crash point is already on disk;
// sends below the crash point never execute.
var CrashSentinel = errors.New("crash at milestone")

// NodeID identifies a simulated process (coordinator or participant).
type NodeID string

// Node is the contract a simulated process must implement.
type Node interface {
	ID() NodeID
	HandleMessage(now int64, from NodeID, payload any)
	OnRestart(now int64)
	Crash()
	IsDown() bool
}

// Record is one line of the execution trace.
type Record struct {
	Tick   int64  `json:"tick"`
	Kind   string `json:"kind"`
	Node   string `json:"node,omitempty"`
	Detail string `json:"detail,omitempty"`
}

// Recorder receives trace events while the simulation runs.
type Recorder func(Record)

// NetConfig configures the unreliable network. Delays are in virtual ticks.
type NetConfig struct {
	BaseDelay int64   `json:"baseDelay"` // minimum one-way delay
	Jitter    int64   `json:"jitter"`    // extra uniform delay in [0, Jitter]; causes reordering
	Loss      float64 `json:"loss"`      // probability a message is dropped
	Duplicate float64 `json:"duplicate"` // probability a message is delivered twice
}

// event kinds
const (
	evMessage = 0
	evFunc    = 1
)

type event struct {
	at      int64
	seq     int64
	kind    int
	from    NodeID
	to      NodeID
	payload any
	fn      func(now int64)
}

type pq []*event

func (p pq) Len() int { return len(p) }
func (p pq) Less(i, j int) bool {
	if p[i].at != p[j].at {
		return p[i].at < p[j].at
	}
	return p[i].seq < p[j].seq // FIFO at equal ticks => deterministic
}
func (p pq) Swap(i, j int) { p[i], p[j] = p[j], p[i] }
func (p *pq) Push(x any)   { *p = append(*p, x.(*event)) }
func (p *pq) Pop() any {
	old := *p
	n := len(old)
	it := old[n-1]
	*p = old[:n-1]
	return it
}

// Engine is the discrete-event kernel.
type Engine struct {
	nodes  map[NodeID]Node
	queue  *pq
	now    int64
	seqCtr int64
	rng    *rand.Rand
	net    *Network
	rec    Recorder
}

// New creates an engine. Randomness (network faults) comes from seed.
func New(seed int64, rec Recorder, cfg NetConfig) *Engine {
	e := &Engine{
		nodes: map[NodeID]Node{},
		queue: &pq{},
		rng:   rand.New(rand.NewSource(seed)),
		rec:   rec,
	}
	heap.Init(e.queue)
	e.net = &Network{cfg: cfg, rng: e.rng, eng: e}
	return e
}

// Rng exposes the deterministic RNG (used so all randomness is ordered).
func (e *Engine) Rng() *rand.Rand { return e.rng }

// Trace appends an explanatory record to the execution trace.
func (e *Engine) Trace(kind, node, detail string) {
	e.record(Record{Tick: e.now, Kind: kind, Node: node, Detail: detail})
}

// Now is the virtual tick currently being dispatched.
func (e *Engine) Now() int64 { return e.now }

// Register adds a node.
func (e *Engine) Register(n Node) { e.nodes[n.ID()] = n }

func (e *Engine) record(r Record) {
	if e.rec != nil {
		e.rec(r)
	}
}

func (e *Engine) push(ev *event) {
	ev.seq = e.seqCtr
	e.seqCtr++
	heap.Push(e.queue, ev)
}

// ScheduleFunc enqueus a bare action at tick at (timers, client input,
// restart hooks).
func (e *Engine) ScheduleFunc(at int64, fn func(now int64)) {
	e.push(&event{at: at, kind: evFunc, fn: fn})
}

func (e *Engine) scheduleMsg(at int64, from, to NodeID, payload any) {
	e.push(&event{at: at, kind: evMessage, from: from, to: to, payload: payload})
}

// Send transmits a message through the unreliable network. Must be called
// while an event is being dispatched (virtual time is "now").
func (e *Engine) Send(from, to NodeID, payload any) {
	e.net.transmit(e.now, from, to, payload)
}

// Crash immediately wipes the node's volatile state. Its WAL is untouched.
// A restart is scheduled downTicks later when downTicks > 0.
func (e *Engine) Crash(id NodeID, downTicks int64) {
	n, ok := e.nodes[id]
	if !ok || n.IsDown() {
		return
	}
	n.Crash()
	e.record(Record{Tick: e.now, Kind: "crash", Node: string(id),
		Detail: fmt.Sprintf("downTicks=%d", downTicks)})
	if downTicks > 0 {
		at := e.now + downTicks
		e.ScheduleFunc(at, func(now int64) {
			node, ok := e.nodes[id]
			if !ok || !node.IsDown() {
				return
			}
			node.OnRestart(now)
			e.record(Record{Tick: now, Kind: "restart", Node: string(id)})
		})
	}
}

// Run drains the event queue up to and including horizon.
func (e *Engine) Run(horizon int64) {
	for e.queue.Len() > 0 {
		ev := heap.Pop(e.queue).(*event)
		if ev.at > horizon {
			break
		}
		e.now = ev.at
		switch ev.kind {
		case evMessage:
			target, ok := e.nodes[ev.to]
			if !ok {
				continue
			}
			if target.IsDown() {
				// Simulated node is powered off: the message is lost
				// (retransmissions are the protocol's responsibility).
				e.record(Record{Tick: ev.at, Kind: "drop", Node: string(ev.from),
					Detail: fmt.Sprintf("to=%s type=%T reason=node-down", ev.to, ev.payload)})
				continue
			}
			e.record(Record{Tick: ev.at, Kind: "deliver", Node: string(ev.to),
				Detail: fmt.Sprintf("from=%s type=%T", ev.from, ev.payload)})
			e.dispatch(func() { target.HandleMessage(ev.at, ev.from, ev.payload) })
		case evFunc:
			e.dispatch(func() { ev.fn(ev.at) })
		}
	}
}

// dispatch runs one handler, converting a milestone crash sentinel into a
// recorded event rather than a propagating panic.
func (e *Engine) dispatch(fn func()) {
	defer func() {
		if r := recover(); r != nil {
			if err, ok := r.(error); ok && errors.Is(err, CrashSentinel) {
				return
			}
			panic(r)
		}
	}()
	fn()
}

// Network models an unreliable datagram network. Every call consumes RNG
// draws in a fixed order, which keeps identical seeds reproducible.
type Network struct {
	cfg NetConfig
	rng *rand.Rand
	eng *Engine
}

func (n *Network) transmit(now int64, from, to NodeID, payload any) {
	n.eng.record(Record{Tick: now, Kind: "send", Node: string(from),
		Detail: fmt.Sprintf("to=%s type=%T", to, payload)})
	if n.rng.Float64() < n.cfg.Loss {
		n.eng.record(Record{Tick: now, Kind: "drop", Node: string(from),
			Detail: fmt.Sprintf("to=%s type=%T reason=loss", to, payload)})
		return
	}
	delay := n.cfg.BaseDelay
	if n.cfg.Jitter > 0 {
		delay += n.rng.Int63n(n.cfg.Jitter + 1)
	}
	n.eng.scheduleMsg(now+delay, from, to, payload)
	if n.rng.Float64() < n.cfg.Duplicate {
		d2 := n.cfg.BaseDelay
		if n.cfg.Jitter > 0 {
			d2 += n.rng.Int63n(n.cfg.Jitter + 1)
		}
		n.eng.record(Record{Tick: now, Kind: "dup", Node: string(from),
			Detail: fmt.Sprintf("to=%s type=%T", to, payload)})
		n.eng.scheduleMsg(now+d2, from, to, payload)
	}
}

package sim

import (
	"container/heap"
	"math/rand"
	"sort"
)

// actor is anything that receives messages and timer firings.
type actor interface {
	id() string
	receive(m *Msg)
	timer(kind string, data any)
}

type queuedEvent struct {
	when  int64
	seq   int64 // FIFO tie-break: smaller scheduled first -> causal order
	kind  byte  // 0 = delivery, 1 = timer
	msg   *Msg
	to    actor
	tid   int64
	tdata any
}

// eventHeap is a min-heap ordered by (when, seq): the same causal ordering the
// earlier linear scan produced, at O(log n) per push/pop instead of O(n).
type eventHeap []*queuedEvent

func (h eventHeap) Len() int { return len(h) }
func (h eventHeap) Less(i, j int) bool {
	if h[i].when != h[j].when {
		return h[i].when < h[j].when
	}
	return h[i].seq < h[j].seq
}
func (h eventHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }
func (h *eventHeap) Push(x any)   { *h = append(*h, x.(*queuedEvent)) }
func (h *eventHeap) Pop() any {
	old := *h
	n := len(old)
	x := old[n-1]
	old[n-1] = nil
	*h = old[:n-1]
	return x
}

// Engine is the deterministic discrete-event core: a logical clock, an event
// queue and the simulated lossy network. Everything runs in one goroutine and
// every random decision comes from one RNG, so identical Config+Seed produces
// identical runs.
type Engine struct {
	Seed int64
	rng  *rand.Rand

	now      int64
	eventSeq int64
	timerSeq int64
	queue    eventHeap
	actors   map[string]actor
	dead     map[string]bool
	active   map[int64]bool

	// Network knobs (rates in [0,1], delays in ticks).
	loss, dup, reorder      float64
	minDelay, maxDelay      int
	blackholeMigrationUntil int64 // migration msgs at tick < this are dropped
	blackholeActive         bool

	stats *Stats

	// Hook the upper layer may use to observe delivery (ground truth).
	onDeliver func(m *Msg)
}

func newEngine(seed int64, st *Stats) *Engine {
	return &Engine{
		Seed:   seed,
		rng:    rand.New(rand.NewSource(seed)),
		actors: map[string]actor{},
		dead:   map[string]bool{},
		active: map[int64]bool{},
		stats:  st,
	}
}

func (e *Engine) register(a actor) { e.actors[a.id()] = a }

// setNetwork configures the lossy-link parameters (rates in [0,1), delays in
// ticks). Called once before the run starts.
func (e *Engine) setNetwork(loss, dup, reorder float64, minDelay, maxDelay int) {
	e.loss, e.dup, e.reorder = loss, dup, reorder
	e.minDelay, e.maxDelay = minDelay, maxDelay
}

// tick returns the current logical time.
func (e *Engine) tick() int64 { return e.now }

// setBlackhole installs/removes the migration-only network interruption.
func (e *Engine) setBlackhole(on bool, until int64) {
	e.blackholeActive = on
	if on {
		e.blackholeMigrationUntil = until
	}
}

// send puts one logical message through the network. Retransmissions call
// send again with the same Msg.Seq.
func (e *Engine) send(m *Msg) {
	e.stats.MessagesSent++
	// Simulated interruption: node-to-node migration traffic is blackholed;
	// client and control traffic still flows, so the protocol must recover via
	// retransmission once the window ends.
	if e.blackholeActive && m.Migration && isNode(m.From) && isNode(m.To) {
		e.stats.MessagesDropped++
		return
	}
	if e.rng.Float64() < e.loss {
		e.stats.MessagesDropped++
		return
	}
	copies := 1
	if e.rng.Float64() < e.dup {
		copies = 2
		e.stats.MessagesDuplicated++
	}
	for c := 0; c < copies; c++ {
		delay := e.drawDelay(c)
		e.scheduleDelivery(delay, m)
	}
}

func (e *Engine) drawDelay(copyIdx int) int64 {
	d := e.minDelay
	if e.maxDelay > e.minDelay {
		d += e.rng.Intn(e.maxDelay - e.minDelay + 1)
	}
	// Reordering: an extra delay beyond the normal window. Duplicate copies
	// also take the detour so they can arrive after the original.
	if e.rng.Float64() < e.reorder || copyIdx == 1 {
		e.stats.MessagesReordered++
		d += e.maxDelay + 1 + e.rng.Intn(e.maxDelay+1)
	}
	return int64(d)
}

func (e *Engine) scheduleDelivery(delay int64, m *Msg) {
	a := e.actors[m.To]
	e.push(&queuedEvent{
		when: e.now + delay,
		kind: 0,
		msg:  m,
		to:   a, // nil if target never existed; handled at pop
	})
}

// after schedules a one-shot timer; returns its id for cancellation.
func (e *Engine) after(delay int64, a actor, kind string, data any) int64 {
	e.timerSeq++
	tid := e.timerSeq
	e.active[tid] = true
	e.push(&queuedEvent{when: e.now + delay, kind: 1, to: a, tid: tid, tdata: timerPayload{kind, data}})
	return tid
}

type timerPayload struct {
	kind string
	data any
}

func (e *Engine) push(ev *queuedEvent) {
	e.eventSeq++
	ev.seq = e.eventSeq
	heap.Push(&e.queue, ev)
}

// cancelTimer marks a timer id stale.
func (e *Engine) cancelTimer(tid int64) { delete(e.active, tid) }

// run processes events in (when, seq) order until the next event exceeds
// stopTick.
func (e *Engine) run(stopTick int64) {
	for e.queue.Len() > 0 {
		ev := heap.Pop(&e.queue).(*queuedEvent)
		if ev.when > stopTick {
			break
		}
		e.now = ev.when
		switch ev.kind {
		case 0:
			if ev.to == nil || e.dead[ev.msg.To] {
				e.stats.MessagesDropped++
				continue
			}
			e.stats.MessagesDelivered++
			e.stats.BytesDelivered += ev.msg.Size()
			if e.onDeliver != nil {
				e.onDeliver(ev.msg)
			}
			ev.to.receive(ev.msg)
		case 1:
			if ev.to == nil || e.dead[ev.to.id()] || !e.active[ev.tid] {
				continue
			}
			delete(e.active, ev.tid)
			tp := ev.tdata.(timerPayload)
			ev.to.timer(tp.kind, tp.data)
		}
	}
}

func isNode(id string) bool {
	// Node ids are registered with a "node:" prefix internally; client ids use
	// "client:" and the controller is "ctrl".
	return len(id) > 5 && id[:5] == "node:"
}

// sortedKeys helper for deterministic report iteration.
func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

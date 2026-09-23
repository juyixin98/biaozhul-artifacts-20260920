// Package sim is a deterministic single-process discrete-event simulator for
// OR-Set replicas. There are no sockets, goroutines-as-nodes or clocks: a
// priority queue of timestamped events drives the whole run. The emulated
// network can independently drop, duplicate and reorder full-state sync
// messages. Every random choice comes from one math/rand source seeded by the
// run config, so identical input always produces an identical trace.
package sim

import (
	"container/heap"
	"fmt"
	"math/rand"
	"sort"

	orsetsim "orsetsim/internal/orset"
)

// NetworkConfig configures the emulated network, all durations in abstract
// integer ticks.
type NetworkConfig struct {
	LossProb      float64 `json:"loss_prob"`      // probability a message is dropped
	DuplicateProb float64 `json:"duplicate_prob"` // probability a delivered message is duplicated
	ReorderProb   float64 `json:"reorder_prob"`   // probability a message is deliberately held back
	MinDelay      uint64  `json:"min_delay"`      // minimum delivery delay
	MaxDelay      uint64  `json:"max_delay"`      // maximum base delivery delay
}

// Op is a scheduled client operation.
type Op struct {
	Time    uint64 `json:"time"`
	Node    string `json:"node"`
	Op      string `json:"op"`      // add | remove | sync | gc
	Element string `json:"element"` // add/remove target
	Target  string `json:"target"`  // sync target (empty = broadcast); gc ignores it
}

// Config is the JSON run description.
type Config struct {
	Seed     int64         `json:"seed"`
	Nodes    []string      `json:"nodes"`
	Network  NetworkConfig `json:"network"`
	Events   []Op          `json:"events"`
	MaxTicks uint64        `json:"max_ticks"` // optional hard stop; 0 = run to exhaustion
}

// Entry is one trace record.
type Entry struct {
	Time    uint64   `json:"time"`
	Node    string   `json:"node,omitempty"`
	Kind    string   `json:"kind"` // add | remove | send | drop | dup | deliver | gc | warn
	Element string   `json:"element,omitempty"`
	Tags    []string `json:"tags,omitempty"`
	From    string   `json:"from,omitempty"`
	Target  string   `json:"target,omitempty"`
	MsgID   uint64   `json:"msg_id,omitempty"`
	Count   int      `json:"count,omitempty"`
	Values  []string `json:"values,omitempty"`
	Detail  string   `json:"detail,omitempty"`
}

// NodeFinal is a replica's final state.
type NodeFinal struct {
	Node   string         `json:"node"`
	Values []string       `json:"values"`
	State  orsetsim.State `json:"state"`
}

// Report is the complete run result.
type Report struct {
	Seed           int64          `json:"seed"`
	Converged      bool           `json:"converged"`
	AllStatesEqual bool           `json:"all_states_equal"`
	FinalValues    []string       `json:"final_values"`
	Ticks          uint64         `json:"ticks"`
	Counts         map[string]int `json:"counts"`
	Nodes          []NodeFinal    `json:"nodes"`
	Trace          []Entry        `json:"trace"`
}

// pending network message
type message struct {
	id      uint64
	from    string
	to      string
	payload *orsetsim.State
	bornAt  uint64
}

type timedEvent struct {
	time uint64
	seq  uint64 // tie-breaker: stable FIFO among equal timestamps
	msg  *message
}

type eventHeap []timedEvent

func (h eventHeap) Len() int { return len(h) }
func (h eventHeap) Less(i, j int) bool {
	if h[i].time != h[j].time {
		return h[i].time < h[j].time
	}
	return h[i].seq < h[j].seq
}
func (h eventHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }
func (h *eventHeap) Push(x any)   { *h = append(*h, x.(timedEvent)) }
func (h *eventHeap) Pop() any {
	old := *h
	n := len(old)
	it := old[n-1]
	*h = old[:n-1]
	return it
}

// Engine runs one simulation.
type Engine struct {
	cfg    Config
	rng    *rand.Rand
	states map[string]*orsetsim.State
	seqs   map[string]uint64 // per-node minted-tag counter
	pq     *eventHeap
	hseq   uint64 // heap tie-breaker counter
	msgSeq uint64
	now    uint64
	trace  []Entry
	counts map[string]int
}

// NewEngine validates the config and constructs an engine.
func NewEngine(cfg Config) (*Engine, error) {
	if len(cfg.Nodes) < 1 {
		return nil, fmt.Errorf("config needs at least one node")
	}
	seen := map[string]bool{}
	for _, n := range cfg.Nodes {
		if n == "" {
			return nil, fmt.Errorf("node name must not be empty")
		}
		if seen[n] {
			return nil, fmt.Errorf("duplicate node %q", n)
		}
		seen[n] = true
	}
	if cfg.Network.MinDelay > cfg.Network.MaxDelay {
		return nil, fmt.Errorf("min_delay (%d) > max_delay (%d)", cfg.Network.MinDelay, cfg.Network.MaxDelay)
	}
	for _, p := range []struct {
		name string
		v    float64
	}{
		{"loss_prob", cfg.Network.LossProb},
		{"duplicate_prob", cfg.Network.DuplicateProb},
		{"reorder_prob", cfg.Network.ReorderProb},
	} {
		if p.v < 0 || p.v >= 1 {
			return nil, fmt.Errorf("%s must be in [0,1), got %v", p.name, p.v)
		}
	}
	for i, e := range cfg.Events {
		if !seen[e.Node] {
			return nil, fmt.Errorf("events[%d]: unknown node %q", i, e.Node)
		}
		switch e.Op {
		case "add", "remove":
			if e.Element == "" {
				return nil, fmt.Errorf("events[%d]: %s needs an element", i, e.Op)
			}
		case "sync":
			if e.Target != "" && !seen[e.Target] {
				return nil, fmt.Errorf("events[%d]: unknown sync target %q", i, e.Target)
			}
			if e.Target == e.Node {
				return nil, fmt.Errorf("events[%d]: node %q cannot sync to itself", i, e.Node)
			}
		case "gc":
		default:
			return nil, fmt.Errorf("events[%d]: unknown op %q", i, e.Op)
		}
	}
	e := &Engine{
		cfg:    cfg,
		rng:    rand.New(rand.NewSource(cfg.Seed)),
		states: map[string]*orsetsim.State{},
		seqs:   map[string]uint64{},
		pq:     &eventHeap{},
		counts: map[string]int{},
	}
	heap.Init(e.pq)
	for _, n := range cfg.Nodes {
		e.states[n] = orsetsim.New()
	}
	// Deterministic event order even if the JSON lists them shuffled.
	sort.SliceStable(cfg.Events, func(i, j int) bool {
		if cfg.Events[i].Time != cfg.Events[j].Time {
			return cfg.Events[i].Time < cfg.Events[j].Time
		}
		return i < j
	})
	return e, nil
}

// Run executes all events and pending network messages, then returns the report.
func (e *Engine) Run() *Report {
	ei := 0
	for {
		// Next client event time.
		var nextClient uint64
		haveClient := ei < len(e.cfg.Events)
		if haveClient {
			nextClient = e.cfg.Events[ei].Time
		}
		// Next network event time.
		var nextNet uint64
		haveNet := e.pq.Len() > 0
		if haveNet {
			nextNet = (*e.pq)[0].time
		}
		switch {
		case !haveClient && !haveNet:
			return e.finish()
		case haveClient && (!haveNet || nextClient <= nextNet):
			e.now = nextClient
			// Drain every client event scheduled at this tick (config order).
			for ei < len(e.cfg.Events) && e.cfg.Events[ei].Time == e.now {
				e.applyClient(e.cfg.Events[ei])
				ei++
			}
		default:
			ev := heap.Pop(e.pq).(timedEvent)
			if e.cfg.MaxTicks > 0 && ev.time > e.cfg.MaxTicks {
				return e.finish()
			}
			e.now = ev.time
			e.deliver(ev.msg)
		}
	}
}

func (e *Engine) applyClient(op Op) {
	switch op.Op {
	case "add":
		e.seqs[op.Node]++
		t := e.states[op.Node].Add(op.Node, e.seqs[op.Node], op.Element)
		e.trace = append(e.trace, Entry{
			Time: e.now, Node: op.Node, Kind: "add", Element: op.Element,
			Tags: []string{t.String()}, Values: e.states[op.Node].Values(),
		})
		e.counts["add"]++
	case "remove":
		tags := e.states[op.Node].Remove(op.Element)
		tagStrs := make([]string, len(tags))
		for i, t := range tags {
			tagStrs[i] = t.String()
		}
		e.trace = append(e.trace, Entry{
			Time: e.now, Node: op.Node, Kind: "remove", Element: op.Element,
			Tags: tagStrs, Values: e.states[op.Node].Values(),
		})
		e.counts["remove"]++
	case "sync":
		targets := []string{op.Target}
		if op.Target == "" {
			targets = e.cfg.Nodes
		}
		for _, to := range targets {
			if to == op.Node {
				continue
			}
			e.send(op.Node, to)
		}
	case "gc":
		e.gc()
	}
}

// send snapshots the sender's full state and runs it through the emulated
// network. Random draws happen in one fixed order — loss, duplicate, reorder,
// delay — to keep runs reproducible.
func (e *Engine) send(from, to string) {
	e.msgSeq++
	m := &message{
		id:      e.msgSeq,
		from:    from,
		to:      to,
		payload: e.states[from].Clone(),
		bornAt:  e.now,
	}
	e.counts["send"]++
	e.trace = append(e.trace, Entry{
		Time: e.now, Node: from, Kind: "send", From: from, Target: to, MsgID: m.id,
	})

	if e.rng.Float64() < e.cfg.Network.LossProb {
		e.counts["drop"]++
		e.trace = append(e.trace, Entry{
			Time: e.now, Kind: "drop", From: from, Target: to, MsgID: m.id,
			Detail: "network dropped message",
		})
		return
	}

	dup := e.rng.Float64() < e.cfg.Network.DuplicateProb
	reorder := e.rng.Float64() < e.cfg.Network.ReorderProb
	delay := e.drawDelay()
	if reorder {
		// Hold this message long enough that traffic sent shortly after it
		// tends to land first — explicit, observable reordering rather than
		// relying on random-delay collisions.
		delay += e.cfg.Network.MaxDelay
		e.counts["reorder"]++
		e.trace = append(e.trace, Entry{
			Time: e.now, Kind: "reorder", From: from, Target: to, MsgID: m.id,
			Detail: fmt.Sprintf("message held back, delivery at t=%d", e.now+delay),
		})
	}
	heap.Push(e.pq, timedEvent{time: e.now + delay, seq: e.nextHSeq(), msg: m})
	if dup {
		e.counts["duplicate"]++
		dupDelay := e.drawDelay() + e.cfg.Network.MaxDelay
		e.trace = append(e.trace, Entry{
			Time: e.now, Kind: "dup", From: from, Target: to, MsgID: m.id,
			Detail: fmt.Sprintf("message duplicated, second delivery at t=%d", e.now+dupDelay),
		})
		heap.Push(e.pq, timedEvent{time: e.now + dupDelay, seq: e.nextHSeq(), msg: m})
	}
}

func (e *Engine) drawDelay() uint64 {
	span := e.cfg.Network.MaxDelay - e.cfg.Network.MinDelay + 1
	return e.cfg.Network.MinDelay + uint64(e.rng.Int63n(int64(span)))
}

func (e *Engine) deliver(m *message) {
	st := e.states[m.to]
	beforeVals := append([]string(nil), st.Values()...)
	st.Merge(m.payload)
	afterVals := st.Values()
	e.counts["deliver"]++
	detail := "no local change"
	if !equalStringSlices(beforeVals, afterVals) {
		detail = fmt.Sprintf("values %v -> %v", beforeVals, afterVals)
	}
	e.trace = append(e.trace, Entry{
		Time: e.now, Node: m.to, Kind: "deliver", From: m.from, Target: m.to,
		MsgID: m.id, Values: afterVals, Detail: detail,
	})
}

// gc performs a *coordinated* reclamation across all replicas: only tags that
// are tombstoned on every single node are discarded, and every node is a
// witness for every other node. This models the stability barrier described
// in the README.
func (e *Engine) gc() {
	nodes := append([]*orsetsim.State(nil), e.statesOrdered()...)

	// Collect all tombstoned tags from the first node, then intersect.
	var candidates map[orsetsim.Tag]struct{}
	for i, st := range nodes {
		if i == 0 {
			candidates = map[orsetsim.Tag]struct{}{}
			for t := range st.Tombstones() {
				candidates[t] = struct{}{}
			}
			continue
		}
		for t := range candidates {
			if _, ok := st.Tombstones()[t]; !ok {
				delete(candidates, t)
			}
		}
	}
	tags := make([]orsetsim.Tag, 0, len(candidates))
	for t := range candidates {
		tags = append(tags, t)
	}
	// Witness against the pre-reclaim snapshots: the stability decision must
	// be a single atomic barrier, not be influenced by earlier nodes in this
	// loop having already compacted themselves.
	snap := make([]*orsetsim.State, len(nodes))
	for i, st := range nodes {
		snap[i] = st.Clone()
	}
	total := 0
	for _, name := range e.cfg.Nodes {
		total += e.states[name].Reclaim(tags, snap...)
	}
	e.counts["gc"]++
	e.trace = append(e.trace, Entry{
		Time: e.now, Kind: "gc", Count: total,
		Detail: fmt.Sprintf("coordinated GC across %d nodes reclaimed %d tag entries", len(nodes), total),
	})
}

func (e *Engine) statesOrdered() []*orsetsim.State {
	out := make([]*orsetsim.State, 0, len(e.cfg.Nodes))
	for _, n := range e.cfg.Nodes {
		out = append(out, e.states[n])
	}
	return out
}

func (e *Engine) nextHSeq() uint64 {
	e.hseq++
	return e.hseq
}

func (e *Engine) finish() *Report {
	r := &Report{
		Seed:   e.cfg.Seed,
		Ticks:  e.now,
		Counts: e.counts,
		Trace:  e.trace,
	}
	names := append([]string(nil), e.cfg.Nodes...)
	sort.Strings(names)
	var ref *orsetsim.State
	allEqual := true
	for _, n := range e.cfg.Nodes {
		st := e.states[n]
		r.Nodes = append(r.Nodes, NodeFinal{Node: n, Values: st.Values(), State: *st.Clone()})
		if ref == nil {
			ref = st
		} else if !st.Equal(ref) {
			allEqual = false
		}
	}
	r.AllStatesEqual = allEqual
	if allEqual {
		r.FinalValues = ref.Values()
		r.Converged = true
	}
	return r
}

func equalStringSlices(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

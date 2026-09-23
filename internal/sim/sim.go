// Package sim is a deterministic, single-process discrete event
// simulator for versioned replicas exchanging vector-clock state.
//
// Time is a logical integer tick. External events (client writes/reads,
// gossip sends) are scheduled in the scenario; the network turns every
// send into zero or more internal delivery events. A seeded RNG decides
// probabilistic drops, duplicates and delays, so a scenario with a fixed
// seed always produces the identical trace (deterministic). No goroutine,
// wall clock or real network is involved.
package sim

import (
	"container/heap"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand"
	"sort"

	"vcconflict/internal/register"
)

// NetworkConfig configures the fault injection applied to every send
// unless an event overrides it.
type NetworkConfig struct {
	// DropProb is the probability a message copy is discarded.
	DropProb float64 `json:"drop_prob,omitempty"`
	// DuplicateProb is the probability a message copy is delivered twice.
	DuplicateProb float64 `json:"duplicate_prob,omitempty"`
	// MinDelay/MaxDelay give the inclusive uniform range of delivery
	// delay in logical ticks. Both default to 1.
	MinDelay int64 `json:"min_delay,omitempty"`
	MaxDelay int64 `json:"max_delay,omitempty"`
}

// EventSpec is one externally scheduled event.
type EventSpec struct {
	// At is the logical tick at which the event fires.
	At int64 `json:"at"`
	// Type is one of: write, read, send, broadcast.
	Type string `json:"type"`
	// NodeName is the acting node (all types).
	NodeName string `json:"node"`

	// Write fields.
	Key     string          `json:"key,omitempty"`
	Value   json.RawMessage `json:"value,omitempty"`
	Context []string        `json:"context,omitempty"`

	// Send/broadcast fields.
	To string `json:"to,omitempty"`

	// Per-event fault overrides (send/broadcast only).
	DropProb      *float64 `json:"drop_prob,omitempty"`
	DuplicateProb *float64 `json:"duplicate_prob,omitempty"`
	Delay         *int64   `json:"delay,omitempty"`
}

// Scenario is the full JSON run request.
type Scenario struct {
	Name        string         `json:"name"`
	Description string         `json:"description,omitempty"`
	Nodes       []string       `json:"nodes"`
	Seed        int64          `json:"seed,omitempty"`
	Network     *NetworkConfig `json:"network,omitempty"`
	Events      []EventSpec    `json:"events"`
}

// Stats summarizes the run.
type Stats struct {
	WritesAccepted int `json:"writes_accepted"`
	WritesRejected int `json:"writes_rejected"`
	Reads          int `json:"reads"`
	Sends          int `json:"sends"`
	Drops          int `json:"drops"`
	Duplicates     int `json:"duplicates"`
	Deliveries     int `json:"deliveries"`
	InvalidEvents  int `json:"invalid_events"`
}

// MergeDigest is the JSON view of one key's merge outcome on delivery.
type MergeDigest struct {
	Added          []string `json:"added"`
	Duplicate      []string `json:"duplicate"`
	Pruned         []string `json:"pruned"`
	PrunedIncoming []string `json:"pruned_incoming"`
}

// TraceEntry is one ordered record of everything that happened.
type TraceEntry struct {
	Seq  int64  `json:"seq"`
	At   int64  `json:"at"`
	Kind string `json:"kind"`

	Node    string          `json:"node,omitempty"`
	To      string          `json:"to,omitempty"`
	Key     string          `json:"key,omitempty"`
	Via     string          `json:"via,omitempty"` // "broadcast"
	Value   json.RawMessage `json:"value,omitempty"`
	Context []string        `json:"context,omitempty"`

	NewVersion *register.Version  `json:"new_version,omitempty"`
	Covered    []string           `json:"covered,omitempty"`
	Siblings   []register.Version `json:"siblings,omitempty"`

	Reason  string   `json:"reason,omitempty"`
	Missing []string `json:"missing,omitempty"`

	MessageID string                 `json:"message_id,omitempty"`
	Delay     int64                  `json:"delay,omitempty"`
	Merge     map[string]MergeDigest `json:"merge,omitempty"`
}

// NodeState is a node's surviving registers at the end of the run.
type NodeState struct {
	Node string                        `json:"node"`
	Keys map[string][]register.Version `json:"keys"`
}

// Result is the JSON run response.
type Result struct {
	Name       string       `json:"name"`
	Ticks      int64        `json:"ticks"`
	Stats      Stats        `json:"stats"`
	Trace      []TraceEntry `json:"trace"`
	FinalState []NodeState  `json:"final_state"`
}

// Validate checks scenario shape and fills defaults.
func (sc *Scenario) Validate() error {
	if len(sc.Nodes) == 0 {
		return errors.New("scenario must declare at least one node")
	}
	seen := map[string]bool{}
	for _, n := range sc.Nodes {
		if n == "" {
			return errors.New("node IDs must be non-empty")
		}
		if seen[n] {
			return fmt.Errorf("duplicate node ID %q", n)
		}
		seen[n] = true
	}
	if sc.Network == nil {
		sc.Network = &NetworkConfig{}
	}
	net := sc.Network
	if err := checkProb("network.drop_prob", net.DropProb); err != nil {
		return err
	}
	if err := checkProb("network.duplicate_prob", net.DuplicateProb); err != nil {
		return err
	}
	if net.MinDelay == 0 && net.MaxDelay == 0 {
		net.MinDelay, net.MaxDelay = 1, 1
	}
	if net.MinDelay < 0 || net.MaxDelay < 0 || net.MinDelay > net.MaxDelay {
		return fmt.Errorf("network delay range invalid: need 0 <= min_delay(%d) <= max_delay(%d)", net.MinDelay, net.MaxDelay)
	}
	for i := range sc.Events {
		e := &sc.Events[i]
		if e.At < 0 {
			return fmt.Errorf("event %d: at must be >= 0", i)
		}
		if !seen[e.NodeName] {
			return fmt.Errorf("event %d (at=%d): unknown node %q", i, e.At, e.NodeName)
		}
		switch e.Type {
		case "write":
			if e.Key == "" {
				return fmt.Errorf("event %d: write requires key", i)
			}
			if len(e.Value) > 0 && !json.Valid(e.Value) {
				return fmt.Errorf("event %d: value is not valid JSON", i)
			}
		case "read":
			if e.Key == "" {
				return fmt.Errorf("event %d: read requires key", i)
			}
		case "send":
			if !seen[e.To] {
				return fmt.Errorf("event %d: send target %q is not a node", i, e.To)
			}
		case "broadcast":
		default:
			return fmt.Errorf("event %d: unknown type %q", i, e.Type)
		}
		if e.DropProb != nil {
			if err := checkProb(fmt.Sprintf("event %d drop_prob", i), *e.DropProb); err != nil {
				return err
			}
		}
		if e.DuplicateProb != nil {
			if err := checkProb(fmt.Sprintf("event %d duplicate_prob", i), *e.DuplicateProb); err != nil {
				return err
			}
		}
		if e.Delay != nil && *e.Delay < 0 {
			return fmt.Errorf("event %d: delay must be >= 0", i)
		}
	}
	return nil
}

func checkProb(name string, p float64) error {
	if p < 0 || p > 1 {
		return fmt.Errorf("%s must be within [0,1], got %v", name, p)
	}
	return nil
}

// ---- event heap ----

type queuedEvent struct {
	at   int64
	seq  int64
	prio int // 0: network delivery; 1: external client event
	kind string
	spec *EventSpec
	msg  *message
}

type evtHeap []*queuedEvent

func (h evtHeap) Len() int { return len(h) }
func (h evtHeap) Less(i, j int) bool {
	if h[i].at != h[j].at {
		return h[i].at < h[j].at
	}
	if h[i].prio != h[j].prio {
		return h[i].prio < h[j].prio // deliveries settle before same-tick writes
	}
	return h[i].seq < h[j].seq
}
func (h evtHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }
func (h *evtHeap) Push(x any)   { *h = append(*h, x.(*queuedEvent)) }
func (h *evtHeap) Pop() any {
	old := *h
	n := len(old)
	it := old[n-1]
	*h = old[:n-1]
	return it
}

type message struct {
	id     string
	from   string
	toNode string
	snap   register.Snapshot
}

// Run validates and executes the scenario, returning the full trace.
func Run(sc *Scenario) (*Result, error) {
	if err := sc.Validate(); err != nil {
		return nil, err
	}
	e := &engine{
		sc:     sc,
		stores: make(map[string]*register.Store, len(sc.Nodes)),
		rng:    rand.New(rand.NewSource(sc.Seed)),
		trace:  []TraceEntry{},
	}
	for _, n := range sc.Nodes {
		e.stores[n] = register.NewStore(n)
	}
	// External events keep declaration order as tie-break (stable sort).
	indexed := make([]*EventSpec, len(sc.Events))
	for i := range sc.Events {
		indexed[i] = &sc.Events[i]
	}
	sort.SliceStable(indexed, func(i, j int) bool { return indexed[i].At < indexed[j].At })
	for _, ev := range indexed {
		e.seq++
		heap.Push(&e.heap, &queuedEvent{at: ev.At, seq: e.seq, prio: 1, kind: "external", spec: ev})
	}

	for e.heap.Len() > 0 {
		qe := heap.Pop(&e.heap).(*queuedEvent)
		e.now = qe.at
		switch qe.kind {
		case "external":
			e.runExternal(qe.spec)
		case "deliver":
			e.deliver(qe.msg)
		}
	}

	res := &Result{Name: sc.Name, Ticks: e.now, Stats: e.stats, Trace: e.trace}
	for _, n := range sc.Nodes {
		st := e.stores[n]
		ns := NodeState{Node: n, Keys: map[string][]register.Version{}}
		for _, k := range st.Keys() {
			ns.Keys[k] = st.Read(k)
		}
		res.FinalState = append(res.FinalState, ns)
	}
	return res, nil
}

type engine struct {
	sc     *Scenario
	stores map[string]*register.Store
	rng    *rand.Rand
	heap   evtHeap
	seq    int64
	msgSeq int64
	now    int64
	trace  []TraceEntry
	stats  Stats
}

func (e *engine) nextSeq() int64 { e.seq++; return e.seq }

func (e *engine) log(t TraceEntry) {
	t.Seq = int64(len(e.trace) + 1)
	t.At = e.now
	e.trace = append(e.trace, t)
}

func (e *engine) runExternal(ev *EventSpec) {
	switch ev.Type {
	case "write":
		e.doWrite(ev)
	case "read":
		e.doRead(ev)
	case "send":
		e.doSend(ev, ev.To, "")
	case "broadcast":
		for _, n := range e.sc.Nodes {
			if n == ev.NodeName {
				continue
			}
			e.doSend(ev, n, "broadcast")
		}
	}
}

func (e *engine) doWrite(ev *EventSpec) {
	store := e.stores[ev.NodeName]
	ctx := ev.Context
	if ctx == nil {
		ctx = []string{}
	}
	rep, err := store.Write(ev.Key, ev.Value, ctx)
	t := TraceEntry{
		At: e.now, Kind: "write", Node: ev.NodeName, Key: ev.Key,
		Value: cloneRaw(ev.Value), Context: append([]string{}, ctx...),
	}
	if err != nil {
		e.stats.WritesRejected++
		t.Kind = "write_rejected"
		var rej *register.RejectError
		if errors.As(err, &rej) {
			t.Reason = rej.Reason
			t.Missing = rej.Missing
			if rej.StaleUnseen != nil {
				v := rej.StaleUnseen
				t.NewVersion = &register.Version{
					ID: v.ID, Value: cloneRaw(v.Value), Clock: cloneClock(v.Clock), Author: v.Author,
				}
			}
		} else {
			t.Reason = "invalid_value"
		}
		e.log(t)
		return
	}
	e.stats.WritesAccepted++
	nv := rep.NewVersion
	t.NewVersion = &register.Version{
		ID: nv.ID, Value: cloneRaw(nv.Value), Clock: cloneClock(nv.Clock), Author: nv.Author,
	}
	t.Covered = rep.Covered
	e.log(t)
}

func (e *engine) doRead(ev *EventSpec) {
	e.stats.Reads++
	sibs := e.stores[ev.NodeName].Read(ev.Key)
	out := make([]register.Version, len(sibs))
	for i, v := range sibs {
		out[i] = register.Version{ID: v.ID, Value: cloneRaw(v.Value), Clock: cloneClock(v.Clock), Author: v.Author}
	}
	e.log(TraceEntry{
		At: e.now, Kind: "read", Node: ev.NodeName, Key: ev.Key, Siblings: out,
	})
}

// doSend applies fault injection to one (sender, recipient) pair and
// schedules the resulting deliveries. RNG rolls happen in a fixed order
// (drop, duplicate, delay) to keep seeded runs reproducible.
func (e *engine) doSend(ev *EventSpec, to, via string) {
	e.stats.Sends++
	net := e.sc.Network
	dropP, dupP := net.DropProb, net.DuplicateProb
	if ev.DropProb != nil {
		dropP = *ev.DropProb
	}
	if ev.DuplicateProb != nil {
		dupP = *ev.DuplicateProb
	}
	store := e.stores[ev.NodeName]
	snap := store.Snapshot()
	e.msgSeq++
	msg := &message{id: fmt.Sprintf("m%d", e.msgSeq), from: ev.NodeName, toNode: to, snap: snap}

	base := TraceEntry{At: e.now, Kind: "send", Node: ev.NodeName, To: to, Via: via, MessageID: msg.id}

	if e.rng.Float64() < dropP {
		e.stats.Drops++
		base.Kind = "drop"
		base.Reason = "simulated packet loss"
		e.log(base)
		return
	}
	duplicated := e.rng.Float64() < dupP
	if duplicated {
		e.stats.Duplicates++
	}
	e.log(base)

	copies := 1
	if duplicated {
		copies = 2
	}
	for c := 0; c < copies; c++ {
		delay := e.sampleDelay(ev)
		// Each copy carries an independent snapshot so later processing
		// can never alias state.
		m2 := &message{id: msg.id, from: msg.from, toNode: msg.toNode, snap: cloneSnapshot(msg.snap)}
		e.seq++
		heap.Push(&e.heap, &queuedEvent{at: e.now + delay, seq: e.seq, prio: 0, kind: "deliver", msg: m2})
		if c == 1 {
			e.log(TraceEntry{
				At: e.now, Kind: "duplicate", Node: ev.NodeName, To: to, Via: via,
				MessageID: msg.id, Delay: delay,
			})
		}
	}
}

func (e *engine) sampleDelay(ev *EventSpec) int64 {
	if ev.Delay != nil {
		return *ev.Delay
	}
	net := e.sc.Network
	if net.MinDelay == net.MaxDelay {
		return net.MinDelay
	}
	return net.MinDelay + e.rng.Int63n(net.MaxDelay-net.MinDelay+1)
}

func (e *engine) deliver(msg *message) {
	e.stats.Deliveries++
	target := e.stores[msg.toNode]
	perKey := target.MergeSnapshot(msg.snap)
	digest := make(map[string]MergeDigest, len(perKey))
	keys := make([]string, 0, len(perKey))
	for k := range perKey {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		r := perKey[k]
		digest[k] = MergeDigest{
			Added:          orEmpty(r.Added),
			Duplicate:      orEmpty(r.Duplicate),
			Pruned:         orEmpty(r.Pruned),
			PrunedIncoming: orEmpty(r.PrunedIncoming),
		}
	}
	e.log(TraceEntry{
		At: e.now, Kind: "deliver", Node: msg.toNode, MessageID: msg.id, Merge: digest,
	})
}

func orEmpty(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

func cloneRaw(b json.RawMessage) json.RawMessage {
	if b == nil {
		return nil
	}
	return append(json.RawMessage(nil), b...)
}

func cloneClock(c map[string]int64) map[string]int64 {
	if c == nil {
		return nil
	}
	out := make(map[string]int64, len(c))
	for k, v := range c {
		out[k] = v
	}
	return out
}

func cloneSnapshot(s register.Snapshot) register.Snapshot {
	out := register.Snapshot{Versions: make(map[string][]register.Version, len(s.Versions))}
	for k, vs := range s.Versions {
		cp := make([]register.Version, len(vs))
		for i, v := range vs {
			cp[i] = register.Version{ID: v.ID, Value: cloneRaw(v.Value), Clock: cloneClock(v.Clock), Author: v.Author}
		}
		out.Versions[k] = cp
	}
	return out
}

// RunJSON parses a scenario, runs it and re-serializes the result.
func RunJSON(raw []byte) ([]byte, error) {
	var sc Scenario
	if err := json.Unmarshal(raw, &sc); err != nil {
		return nil, fmt.Errorf("invalid scenario JSON: %w", err)
	}
	res, err := Run(&sc)
	if err != nil {
		return nil, err
	}
	return json.MarshalIndent(res, "", "  ")
}

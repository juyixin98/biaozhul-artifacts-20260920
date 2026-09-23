// Package sim is a deterministic discrete-event simulator for a set of
// in-process "nodes" running versioned registers.
//
// Time is a virtual logical clock; there are no goroutines, sockets or a real
// cluster. Events are pulled from a min-heap ordered by (time, seq), so the
// same scenario with the same seed always produces the identical trace.
//
// The simulated network can drop, duplicate and reorder messages. Faults can
// be driven statistically from a seed (drop/dup/reorder probabilities) or
// forced per send for deterministic acceptance scenarios.
package sim

import (
	"container/heap"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand"

	"vccsim/internal/clock"
	"vccsim/internal/register"
)

// NetworkPolicy configures the probabilistic network behaviour for one send.
type NetworkPolicy struct {
	DropProb     float64 `json:"drop_prob,omitempty"`
	DupProb      float64 `json:"dup_prob,omitempty"`
	ReorderProb  float64 `json:"reorder_prob,omitempty"`
	BaseDelay    float64 `json:"base_delay,omitempty"`
	Jitter       float64 `json:"jitter,omitempty"`
	ReorderDelay float64 `json:"reorder_delay,omitempty"`
}

// EventSpec is one event in the scenario script.
type EventSpec struct {
	Type  string          `json:"type"`
	Time  float64         `json:"time"`
	Node  string          `json:"node,omitempty"`
	From  string          `json:"from,omitempty"`
	To    string          `json:"to,omitempty"`
	Key   string          `json:"key,omitempty"`
	Value json.RawMessage `json:"value,omitempty"`

	// client_merge
	Context []ContextRef `json:"context,omitempty"`

	// network controls
	Connect bool `json:"connect,omitempty"` // partition: connect(true)/disconnect(false)

	// per-event deterministic overrides
	ForceDrop    bool `json:"force_drop,omitempty"`
	ForceDup     bool `json:"force_dup,omitempty"`
	ForceReorder bool `json:"force_reorder,omitempty"`

	// inspect assertions (optional; failure fails Run)
	ExpectSiblings *int     `json:"expect_siblings,omitempty"`
	ExpectIDs      []string `json:"expect_ids,omitempty"`
}

// ContextRef references a sibling the client merge intends to cover.
type ContextRef struct {
	ID string            `json:"id"`
	VC clock.VectorClock `json:"vc,omitempty"`
}

// Scenario is the full JSON run request.
type Scenario struct {
	Name    string        `json:"name"`
	Seed    int64         `json:"seed"`
	Nodes   []string      `json:"nodes"`
	Network NetworkPolicy `json:"network"`
	Events  []EventSpec   `json:"events"`
	MaxTime float64       `json:"max_time,omitempty"`
}

// VersionView is a version as it appears in an inspect snapshot.
type VersionView struct {
	ID    string            `json:"id"`
	Value json.RawMessage   `json:"value"`
	VC    clock.VectorClock `json:"vc"`
}

// TraceEntry is one thing that happened in the simulation.
type TraceEntry struct {
	Time       float64         `json:"time"`
	Kind       string          `json:"kind"`
	Node       string          `json:"node,omitempty"`
	From       string          `json:"from,omitempty"`
	To         string          `json:"to,omitempty"`
	Key        string          `json:"key,omitempty"`
	VersionID  string          `json:"version_id,omitempty"`
	Dropped    bool            `json:"dropped,omitempty"`
	Duplicated bool            `json:"duplicated,omitempty"`
	Reordered  bool            `json:"reordered,omitempty"`
	Delay      float64         `json:"delay,omitempty"`
	Offline    bool            `json:"offline,omitempty"`
	Accepted   bool            `json:"accepted,omitempty"`
	Error      string          `json:"error,omitempty"`
	Value      json.RawMessage `json:"value,omitempty"`
	Siblings   []VersionView   `json:"siblings,omitempty"`
	Detail     string          `json:"detail,omitempty"`
}

// NodeState is the final state of one node in the report.
type NodeState struct {
	Node   string                   `json:"node"`
	Online bool                     `json:"online"`
	Clock  clock.VectorClock        `json:"clock"`
	Keys   map[string][]VersionView `json:"keys"`
}

// Report is the JSON run result.
type Report struct {
	Name          string       `json:"name"`
	Seed          int64        `json:"seed"`
	Trace         []TraceEntry `json:"trace"`
	Final         []NodeState  `json:"final"`
	AssertionsOK  bool         `json:"assertions_ok"`
	AssertionErrs []string     `json:"assertion_errors,omitempty"`
}

// ---- event heap ----

type item struct {
	time float64
	seq  int64
	run  func()
}

type eventHeap []*item

func (h eventHeap) Len() int { return len(h) }
func (h eventHeap) Less(i, j int) bool {
	if h[i].time != h[j].time {
		return h[i].time < h[j].time
	}
	return h[i].seq < h[j].seq
}
func (h eventHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }
func (h *eventHeap) Push(x any)   { *h = append(*h, x.(*item)) }
func (h *eventHeap) Pop() any {
	old := *h
	n := len(old)
	it := old[n-1]
	*h = old[:n-1]
	return it
}

// ---- simulator ----

type simulator struct {
	scenario Scenario
	rng      *rand.Rand
	h        eventHeap
	nextSeq  int64
	traces   []TraceEntry

	nodes   map[string]*register.Register
	online  map[string]bool
	blocked map[string]map[string]bool // undirected partition edges
	// history resolves context ids supplied without an explicit clock.
	history map[string]clock.VectorClock

	failures []string
}

// Run validates and executes a scenario, returning the full deterministic report.
func Run(sc Scenario) (*Report, error) {
	if err := validate(sc); err != nil {
		return nil, err
	}
	s := &simulator{
		scenario: sc,
		rng:      rand.New(rand.NewSource(sc.Seed)),
		nodes:    map[string]*register.Register{},
		online:   map[string]bool{},
		blocked:  map[string]map[string]bool{},
		history:  map[string]clock.VectorClock{},
	}
	for _, n := range sc.Nodes {
		s.nodes[n] = register.New(n)
		s.online[n] = true
	}
	// Schedule scripted events in order; same-time events keep script order.
	for _, ev := range sc.Events {
		ev := ev
		s.scheduleAt(ev.Time, func() { s.dispatch(ev) })
	}

	maxT := sc.MaxTime
	if maxT <= 0 {
		maxT = 1e9
	}
	for s.h.Len() > 0 {
		it := heap.Pop(&s.h).(*item)
		if it.time > maxT {
			break
		}
		it.run()
	}

	rep := &Report{Name: sc.Name, Seed: sc.Seed, Trace: s.traces, AssertionsOK: true}
	for _, n := range sc.Nodes {
		st := NodeState{Node: n, Online: s.online[n], Clock: s.nodes[n].Clock(), Keys: map[string][]VersionView{}}
		for _, k := range s.nodes[n].Keys() {
			st.Keys[k] = s.views(s.nodes[n].Snapshot(k))
		}
		rep.Final = append(rep.Final, st)
	}
	if len(s.failures) > 0 {
		rep.AssertionsOK = false
		rep.AssertionErrs = append(rep.AssertionErrs, s.failures...)
	}
	return rep, nil
}

func validate(sc Scenario) error {
	if len(sc.Nodes) == 0 {
		return errors.New("scenario must define at least one node")
	}
	known := map[string]bool{}
	for _, n := range sc.Nodes {
		if n == "" {
			return errors.New("node id must not be empty")
		}
		if known[n] {
			return fmt.Errorf("duplicate node id %q", n)
		}
		known[n] = true
	}
	validTypes := map[string]bool{
		"write": true, "send": true, "offline": true, "online": true,
		"partition": true, "client_merge": true, "inspect": true,
	}
	for i, ev := range sc.Events {
		if !validTypes[ev.Type] {
			return fmt.Errorf("events[%d]: unknown type %q", i, ev.Type)
		}
		nodeFor := ev.Node
		if ev.Type == "send" || ev.Type == "partition" {
			if !known[ev.From] || !known[ev.To] {
				return fmt.Errorf("events[%d]: from/to must name known nodes", i)
			}
			if ev.From == ev.To {
				return fmt.Errorf("events[%d]: from and to must differ", i)
			}
			continue
		}
		if !known[nodeFor] {
			return fmt.Errorf("events[%d]: unknown node %q", i, nodeFor)
		}
		if ev.Type == "client_merge" && ev.Key == "" {
			return fmt.Errorf("events[%d]: client_merge requires key", i)
		}
	}
	return nil
}

func (s *simulator) scheduleAt(t float64, fn func()) {
	s.nextSeq++
	heap.Push(&s.h, &item{time: t, seq: s.nextSeq, run: fn})
}

func (s *simulator) views(vs []register.Version) []VersionView {
	out := make([]VersionView, 0, len(vs))
	for _, v := range vs {
		out = append(out, VersionView{ID: v.ID, Value: json.RawMessage(v.Value), VC: v.VC})
	}
	return out
}

func (s *simulator) trace(e TraceEntry) { s.traces = append(s.traces, e) }

func (s *simulator) dispatch(ev EventSpec) {
	switch ev.Type {
	case "write":
		s.doWrite(ev)
	case "send":
		s.doSend(ev)
	case "offline":
		s.online[ev.Node] = false
		s.trace(TraceEntry{Time: ev.Time, Kind: "offline", Node: ev.Node, Offline: true})
	case "online":
		s.online[ev.Node] = true
		s.trace(TraceEntry{Time: ev.Time, Kind: "online", Node: ev.Node})
	case "partition":
		s.setBlocked(ev.From, ev.To, !ev.Connect)
		s.trace(TraceEntry{Time: ev.Time, Kind: "partition", From: ev.From, To: ev.To,
			Detail: map[bool]string{true: "disconnected", false: "connected"}[!ev.Connect]})
	case "client_merge":
		s.doClientMerge(ev)
	case "inspect":
		s.doInspect(ev)
	}
}

func (s *simulator) doWrite(ev EventSpec) {
	v := s.nodes[ev.Node].LocalWrite(ev.Key, cloneJSON(ev.Value))
	s.history[v.ID] = clock.Copy(v.VC)
	s.trace(TraceEntry{Time: ev.Time, Kind: "write", Node: ev.Node, Key: ev.Key,
		VersionID: v.ID, Value: json.RawMessage(cloneJSON(ev.Value)),
		Siblings: s.views(s.nodes[ev.Node].Snapshot(ev.Key))})
}

// netDecision resolves the drop/dup/reorder decision for one send. Explicit
// force_* flags are deterministic and consume no random draws; otherwise the
// policy probabilities draw from the seeded RNG in a fixed order.
func (s *simulator) netDecision(p NetworkPolicy, ev EventSpec) (drop, dup, reorder bool) {
	draw := func(p float64) bool { return p > 0 && s.rng.Float64() < p }
	if ev.ForceDrop {
		return true, false, false
	}
	drop = draw(p.DropProb)
	if ev.ForceDup {
		dup = true
	} else {
		dup = draw(p.DupProb)
	}
	if ev.ForceReorder {
		reorder = true
	} else {
		reorder = draw(p.ReorderProb)
	}
	return drop, dup, reorder
}

func (s *simulator) doSend(ev EventSpec) {
	p := s.scenario.Network
	drop, dup, reorder := s.netDecision(p, ev)

	// Snapshot every current sibling: one message per version, sharing fate.
	versions := s.nodes[ev.From].Snapshot(ev.Key)
	if len(versions) == 0 {
		s.trace(TraceEntry{Time: ev.Time, Kind: "send", From: ev.From, To: ev.To, Key: ev.Key,
			Dropped: drop, Detail: "no versions at source"})
		return
	}

	base := p.BaseDelay
	if base <= 0 {
		base = 1
	}
	delay := base
	if p.Jitter > 0 {
		delay += s.rng.Float64() * p.Jitter
	}
	reorderDelay := p.ReorderDelay
	if reorderDelay <= 0 {
		reorderDelay = base * 3
	}

	s.trace(TraceEntry{Time: ev.Time, Kind: "send", From: ev.From, To: ev.To, Key: ev.Key,
		Dropped: drop, Duplicated: dup, Reordered: reorder, Delay: delay})

	if drop {
		return // message is lost; no receive event is scheduled
	}
	copies := 1
	if dup {
		copies = 2
	}
	for _, v := range versions {
		v := v
		for c := 0; c < copies; c++ {
			deliverAt := ev.Time + delay
			markedReorder := reorder
			if markedReorder {
				deliverAt = ev.Time + reorderDelay
			}
			s.scheduleAt(deliverAt, func() {
				s.deliver(ev, v, c == 1, markedReorder, deliverAt)
			})
		}
	}
}

func (s *simulator) deliver(ev EventSpec, v register.Version, isDuplicate, reordered bool, at float64) {
	if !s.online[ev.To] {
		s.trace(TraceEntry{Time: at, Kind: "receive", From: ev.From, To: ev.To, Key: ev.Key,
			VersionID: v.ID, Offline: true, Dropped: true, Detail: "destination offline"})
		return
	}
	if s.isBlocked(ev.From, ev.To) {
		s.trace(TraceEntry{Time: at, Kind: "receive", From: ev.From, To: ev.To, Key: ev.Key,
			VersionID: v.ID, Dropped: true, Detail: "network partitioned"})
		return
	}
	added := s.nodes[ev.To].Ingest(ev.Key, v)
	s.history[v.ID] = clock.Copy(v.VC)
	s.trace(TraceEntry{
		Time: at, Kind: "receive", From: ev.From, To: ev.To, Key: ev.Key,
		VersionID: v.ID, Duplicated: isDuplicate, Reordered: reordered,
		Accepted: added,
		Detail:   map[bool]string{true: "ingested", false: "duplicate or obsolete"}[added],
		Siblings: s.views(s.nodes[ev.To].Snapshot(ev.Key)),
	})
}

func (s *simulator) doClientMerge(ev EventSpec) {
	req := register.MergeRequest{
		Node:  ev.Node,
		Key:   ev.Key,
		Value: cloneJSON(ev.Value),
	}
	for _, c := range ev.Context {
		entry := register.ContextEntry{ID: c.ID, VC: c.VC}
		if entry.VC == nil {
			vc, ok := s.history[c.ID]
			if !ok {
				s.trace(TraceEntry{Time: ev.Time, Kind: "client_merge", Node: ev.Node, Key: ev.Key,
					Accepted: false, Error: register.ErrUnknownContext.Error(),
					Detail: fmt.Sprintf("context id %s not resolvable", c.ID)})
				return
			}
			entry.VC = vc
		}
		req.Context = append(req.Context, entry)
	}
	v, err := s.nodes[ev.Node].MergeWrite(req)
	if err != nil {
		s.trace(TraceEntry{Time: ev.Time, Kind: "client_merge", Node: ev.Node, Key: ev.Key,
			Value: json.RawMessage(cloneJSON(ev.Value)), Accepted: false, Error: err.Error(),
			Siblings: s.views(s.nodes[ev.Node].Snapshot(ev.Key))})
		return
	}
	s.history[v.ID] = clock.Copy(v.VC)
	s.trace(TraceEntry{Time: ev.Time, Kind: "client_merge", Node: ev.Node, Key: ev.Key,
		Value: json.RawMessage(cloneJSON(ev.Value)), Accepted: true, VersionID: v.ID,
		Siblings: s.views(s.nodes[ev.Node].Snapshot(ev.Key))})
}

// failures collects inspect assertion failures without stopping the run.
func (s *simulator) fail(ev EventSpec, msg string) {
	s.failures = append(s.failures, fmt.Sprintf("inspect at t=%g node=%s key=%s: %s", ev.Time, ev.Node, ev.Key, msg))
}

func (s *simulator) doInspect(ev EventSpec) {
	sibs := s.nodes[ev.Node].Snapshot(ev.Key)
	views := s.views(sibs)
	t := TraceEntry{Time: ev.Time, Kind: "inspect", Node: ev.Node, Key: ev.Key, Siblings: views}
	if ev.ExpectSiblings != nil && len(sibs) != *ev.ExpectSiblings {
		s.fail(ev, fmt.Sprintf("expected %d siblings, observed %d", *ev.ExpectSiblings, len(sibs)))
	}
	if ev.ExpectIDs != nil {
		got := map[string]bool{}
		for _, v := range sibs {
			got[v.ID] = true
		}
		var missing []string
		for _, id := range ev.ExpectIDs {
			if !got[id] {
				missing = append(missing, id)
			}
		}
		if missing != nil {
			s.fail(ev, fmt.Sprintf("missing expected sibling ids: %v", missing))
		}
	}
	s.trace(t)
}

func cloneJSON(b json.RawMessage) []byte {
	if len(b) == 0 {
		return []byte("null")
	}
	out := make([]byte, len(b))
	copy(out, b)
	return out
}

// ---- partition edges (undirected) ----

func (s *simulator) setBlocked(a, b string, block bool) {
	set := func(x, y string) {
		if s.blocked[x] == nil {
			s.blocked[x] = map[string]bool{}
		}
		s.blocked[x][y] = block
	}
	set(a, b)
	set(b, a)
}

func (s *simulator) isBlocked(a, b string) bool { return s.blocked[a][b] }

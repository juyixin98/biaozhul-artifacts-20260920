// Package sim is a deterministic discrete-event simulator for a weighted
// quorum read/write register. All nodes run in a single process; the
// simulated network can drop, duplicate and reorder messages. Given the
// same run spec (including seed) the simulation is fully reproducible.
package sim

import (
	"container/heap"
	"fmt"
	"math/rand"
	"sort"

	"quorumcheck/internal/quorum"
)

// NetParams controls the simulated network. Rates are probabilities in [0,1].
type NetParams struct {
	DropRate float64 `json:"drop_rate"` // probability a message is lost
	DupRate  float64 `json:"dup_rate"`  // probability a message is delivered twice
	MaxDelay int     `json:"max_delay"` // extra random delay 0..MaxDelay ticks (reordering)
}

// OpSpec is one client operation scheduled by the run.
type OpSpec struct {
	ID      string   `json:"id"`
	Type    string   `json:"type"` // "read" or "write"
	At      int      `json:"at"`   // tick at which the client issues the op
	Version int      `json:"version,omitempty"`
	Value   string   `json:"value,omitempty"`
	Targets []string `json:"targets,omitempty"` // nodes to contact; default: all live nodes
	Timeout int      `json:"timeout,omitempty"` // ticks to wait for a quorum; default 50
}

// RunSpec is the JSON run interface consumed by Run.
type RunSpec struct {
	Config  *quorum.Config `json:"config"`
	Seed    int64          `json:"seed"`
	Network NetParams      `json:"network"`
	Down    []string       `json:"down,omitempty"` // nodes unavailable for the whole run
	Ops     []OpSpec       `json:"ops"`
	Trace   bool           `json:"trace,omitempty"` // include the full event log in the result
}

// OpResult reports the outcome of one client operation.
type OpResult struct {
	ID         string   `json:"id"`
	Type       string   `json:"type"`
	Status     string   `json:"status"` // "ok" or "timeout"
	Value      string   `json:"value,omitempty"`
	Version    int      `json:"version,omitempty"`
	AckedBy    []string `json:"acked_by,omitempty"`
	StartedAt  int      `json:"started_at"`
	FinishedAt int      `json:"finished_at"`
}

// Violation reports a read that failed to observe a write which had already
// completed when the read started (atomic-register violation).
type Violation struct {
	ReadID         string `json:"read_id"`
	ReadVersion    int    `json:"read_version"`
	ReadValue      string `json:"read_value"`
	MissingWriteID string `json:"missing_write_id"`
	MissingVersion int    `json:"missing_version"`
	MissingValue   string `json:"missing_value"`
	Detail         string `json:"detail"`
}

// NodeState is the final state of one replica.
type NodeState struct {
	Version int    `json:"version"`
	Value   string `json:"value"`
}

// Result is the JSON output of a run.
type Result struct {
	Seed       int64                `json:"seed"`
	Ops        []OpResult           `json:"ops"`
	Violations []Violation          `json:"violations"`
	Final      map[string]NodeState `json:"final"`
	Events     []string             `json:"events,omitempty"`
}

// message kinds
const (
	mReadReq = iota
	mWriteReq
	mResp
)

type message struct {
	opID    string
	kind    int
	from    string
	to      string
	version int
	value   string
}

// event kinds
const (
	evOpStart = iota
	evDeliver
	evTimeout
)

type event struct {
	time int
	seq  int
	kind int
	op   OpSpec
	msg  message
	opID string
}

// eventHeap is a priority queue ordered by (time, seq) — total order, so the
// simulation is deterministic.
type eventHeap []event

func (h eventHeap) Len() int { return len(h) }
func (h eventHeap) Less(i, j int) bool {
	if h[i].time != h[j].time {
		return h[i].time < h[j].time
	}
	return h[i].seq < h[j].seq
}
func (h eventHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }
func (h *eventHeap) Push(x any)   { *h = append(*h, x.(event)) }
func (h *eventHeap) Pop() any {
	old := *h
	n := len(old)
	e := old[n-1]
	*h = old[:n-1]
	return e
}

type clientState struct {
	op       OpSpec
	acked    map[string]bool
	weight   int
	bestVer  int
	bestVal  string
	done     bool
	finished int
}

type server struct {
	version int
	value   string
}

type simulator struct {
	spec    *RunSpec
	rng     *rand.Rand
	pq      eventHeap
	seq     int
	weights map[string]int
	down    map[string]bool
	servers map[string]*server
	clients map[string]*clientState
	results []OpResult
	events  []string
}

// Validate checks the run spec for structural errors.
func (s *RunSpec) Validate() error {
	if s.Config == nil {
		return fmt.Errorf("run spec must include a config")
	}
	if _, err := s.Config.Validate(); err != nil {
		return err
	}
	if s.Network.DropRate < 0 || s.Network.DropRate > 1 ||
		s.Network.DupRate < 0 || s.Network.DupRate > 1 {
		return fmt.Errorf("drop_rate and dup_rate must be in [0,1]")
	}
	if s.Network.MaxDelay < 0 {
		return fmt.Errorf("max_delay must be >= 0")
	}
	known := map[string]bool{}
	for _, n := range s.Config.Nodes {
		known[n.ID] = true
	}
	for _, d := range s.Down {
		if !known[d] {
			return fmt.Errorf("down node %q not in config", d)
		}
	}
	seen := map[string]bool{}
	for _, op := range s.Ops {
		if op.ID == "" {
			return fmt.Errorf("op with empty id")
		}
		if seen[op.ID] {
			return fmt.Errorf("duplicate op id %q", op.ID)
		}
		seen[op.ID] = true
		if op.Type != "read" && op.Type != "write" {
			return fmt.Errorf("op %q: type must be \"read\" or \"write\"", op.ID)
		}
		if op.Type == "write" && op.Version <= 0 {
			return fmt.Errorf("write op %q: version must be positive", op.ID)
		}
		for _, t := range op.Targets {
			if !known[t] {
				return fmt.Errorf("op %q: target %q not in config", op.ID, t)
			}
		}
	}
	return nil
}

// Run executes the simulation and returns its deterministic result.
func Run(spec *RunSpec) (*Result, error) {
	if err := spec.Validate(); err != nil {
		return nil, err
	}
	s := &simulator{
		spec:    spec,
		rng:     rand.New(rand.NewSource(spec.Seed)),
		weights: map[string]int{},
		down:    map[string]bool{},
		servers: map[string]*server{},
		clients: map[string]*clientState{},
	}
	for _, n := range spec.Config.Nodes {
		s.weights[n.ID] = n.Weight
		s.servers[n.ID] = &server{}
	}
	for _, d := range spec.Down {
		s.down[d] = true
	}
	// Schedule op starts in a deterministic order.
	ops := make([]OpSpec, len(spec.Ops))
	copy(ops, spec.Ops)
	sort.Slice(ops, func(i, j int) bool {
		if ops[i].At != ops[j].At {
			return ops[i].At < ops[j].At
		}
		return ops[i].ID < ops[j].ID
	})
	for _, op := range ops {
		s.push(event{time: op.At, kind: evOpStart, op: op})
	}
	for len(s.pq) > 0 {
		ev := heap.Pop(&s.pq).(event)
		switch ev.kind {
		case evOpStart:
			s.opStart(ev.time, ev.op)
		case evDeliver:
			s.deliver(ev.time, ev.msg)
		case evTimeout:
			s.timeout(ev.time, ev.opID)
		}
	}
	return s.finish(), nil
}

func (s *simulator) push(e event) {
	e.seq = s.seq
	s.seq++
	heap.Push(&s.pq, e)
}

func (s *simulator) trace(format string, args ...any) {
	if s.spec.Trace {
		s.events = append(s.events, fmt.Sprintf(format, args...))
	}
}

// send routes a message through the simulated network.
func (s *simulator) send(now int, m message) {
	if s.rng.Float64() < s.spec.Network.DropRate {
		s.trace("t=%d DROP %s -> %s (op %s)", now, m.from, m.to, m.opID)
		return
	}
	delay := 1
	if s.spec.Network.MaxDelay > 0 {
		delay += s.rng.Intn(s.spec.Network.MaxDelay + 1)
	}
	s.push(event{time: now + delay, kind: evDeliver, msg: m})
	if s.rng.Float64() < s.spec.Network.DupRate {
		d := delay + 1
		if s.spec.Network.MaxDelay > 0 {
			d = delay + s.rng.Intn(s.spec.Network.MaxDelay+1)
		}
		s.push(event{time: now + d, kind: evDeliver, msg: m})
		s.trace("t=%d DUP %s -> %s (op %s)", now, m.from, m.to, m.opID)
	}
}

func (s *simulator) opStart(now int, op OpSpec) {
	cs := &clientState{op: op, acked: map[string]bool{}}
	s.clients[op.ID] = cs
	targets := op.Targets
	if len(targets) == 0 {
		for _, n := range s.spec.Config.SortedNodes() {
			if !s.down[n.ID] {
				targets = append(targets, n.ID)
			}
		}
	}
	kind := mReadReq
	if op.Type == "write" {
		kind = mWriteReq
	}
	sent := 0
	for _, t := range targets {
		if s.down[t] {
			s.trace("t=%d op %s: target %s is down, skipped", now, op.ID, t)
			continue
		}
		sent++
		s.send(now, message{opID: op.ID, kind: kind, from: "client", to: t, version: op.Version, value: op.Value})
	}
	timeout := op.Timeout
	if timeout <= 0 {
		timeout = 50
	}
	s.push(event{time: now + timeout, kind: evTimeout, opID: op.ID})
	s.trace("t=%d op %s (%s) started, sent to %d nodes", now, op.ID, op.Type, sent)
}

func (s *simulator) deliver(now int, m message) {
	if m.from == "client" {
		srv, ok := s.servers[m.to]
		if !ok || s.down[m.to] {
			return
		}
		switch m.kind {
		case mReadReq:
			s.send(now, message{opID: m.opID, kind: mResp, from: m.to, to: "client", version: srv.version, value: srv.value})
		case mWriteReq:
			if m.version > srv.version {
				srv.version = m.version
				srv.value = m.value
			}
			s.send(now, message{opID: m.opID, kind: mResp, from: m.to, to: "client", version: srv.version, value: srv.value})
		}
		return
	}
	// response at the client
	cs, ok := s.clients[m.opID]
	if !ok || cs.done || cs.acked[m.from] {
		return // duplicate response or finished op
	}
	cs.acked[m.from] = true
	cs.weight += s.weights[m.from]
	if m.version > cs.bestVer {
		cs.bestVer = m.version
		cs.bestVal = m.value
	}
	need := s.spec.Config.ReadThreshold
	if cs.op.Type == "write" {
		need = s.spec.Config.WriteThreshold
	}
	s.trace("t=%d op %s: ack from %s (weight %d/%d)", now, m.opID, m.from, cs.weight, need)
	if cs.weight >= need {
		cs.done = true
		cs.finished = now
	}
}

func (s *simulator) timeout(now int, opID string) {
	cs, ok := s.clients[opID]
	if !ok || cs.done {
		return
	}
	cs.done = true
	cs.finished = now
	s.trace("t=%d op %s: timeout", now, opID)
}

func (s *simulator) finish() *Result {
	res := &Result{
		Seed:  s.spec.Seed,
		Final: map[string]NodeState{},
	}
	for _, op := range s.spec.Ops {
		cs := s.clients[op.ID]
		or := OpResult{
			ID:         op.ID,
			Type:       op.Type,
			StartedAt:  op.At,
			FinishedAt: cs.finished,
		}
		if cs.weight >= thresholdFor(s.spec.Config, op.Type) {
			or.Status = "ok"
		} else {
			or.Status = "timeout"
		}
		if op.Type == "read" {
			or.Version = cs.bestVer
			or.Value = cs.bestVal
		} else {
			or.Version = op.Version
			or.Value = op.Value
		}
		for id := range cs.acked {
			or.AckedBy = append(or.AckedBy, id)
		}
		sort.Strings(or.AckedBy)
		res.Ops = append(res.Ops, or)
	}
	res.Violations = checkAtomicity(res.Ops)
	for _, n := range s.spec.Config.SortedNodes() {
		srv := s.servers[n.ID]
		res.Final[n.ID] = NodeState{Version: srv.version, Value: srv.value}
	}
	res.Events = s.events
	return res
}

func thresholdFor(c *quorum.Config, opType string) int {
	if opType == "write" {
		return c.WriteThreshold
	}
	return c.ReadThreshold
}

// checkAtomicity verifies that every completed read observes the
// highest-version write that completed before the read started.
func checkAtomicity(ops []OpResult) []Violation {
	var writes []OpResult
	for _, o := range ops {
		if o.Type == "write" && o.Status == "ok" {
			writes = append(writes, o)
		}
	}
	var out []Violation
	for _, r := range ops {
		if r.Type != "read" || r.Status != "ok" {
			continue
		}
		var latest *OpResult
		for i := range writes {
			w := &writes[i]
			if w.FinishedAt <= r.StartedAt && (latest == nil || w.Version > latest.Version) {
				latest = w
			}
		}
		if latest == nil {
			continue
		}
		if r.Version < latest.Version || (r.Version == latest.Version && r.Value != latest.Value) {
			out = append(out, Violation{
				ReadID:         r.ID,
				ReadVersion:    r.Version,
				ReadValue:      r.Value,
				MissingWriteID: latest.ID,
				MissingVersion: latest.Version,
				MissingValue:   latest.Value,
				Detail: fmt.Sprintf("read %s returned (v%d,%q) but write %s completed at t=%d with (v%d,%q) before the read started at t=%d",
					r.ID, r.Version, r.Value, latest.ID, latest.FinishedAt, latest.Version, latest.Value, r.StartedAt),
			})
		}
	}
	return out
}

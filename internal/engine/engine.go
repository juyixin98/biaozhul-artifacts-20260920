// Package engine wires nodes, the unreliable network and the discrete-event
// scheduler into one deterministic simulation driven by a JSON Request.
package engine

import (
	"fmt"
	"math/rand"
	"sort"
	"strings"

	"causal-broadcast/internal/node"
	"causal-broadcast/internal/sim"
	"causal-broadcast/internal/simnet"
	"causal-broadcast/internal/vec"
)

// ---- Request model ------------------------------------------------------

// BroadcastSpec schedules one broadcast at an absolute virtual time.
type BroadcastSpec struct {
	AtMs    int64  `json:"at_ms"`
	Node    string `json:"node"`
	Payload string `json:"payload"`
}

// TriggerSpec fires a broadcast when a node delivers AfterOrigin's message
// (exact (node, seq) match). Used to generate causal broadcast chains.
type TriggerSpec struct {
	WhenNode    string  `json:"when_node"`
	AfterOrigin vec.Key `json:"after_origin"`
	Payload     string  `json:"payload"`
}

// RetransmitSpec asks node From (which retained FromOrigin's message) to emit
// a fresh copy directly to each target in To, at AtMs. Direct injection is
// required to repair an explicitly-faulted copy: explicit faults match by
// (origin, from, to) and would otherwise drop the repair too.
type RetransmitSpec struct {
	AtMs       int64    `json:"at_ms"`
	From       string   `json:"from"`
	FromOrigin vec.Key  `json:"from_origin"`
	To         []string `json:"to"`
	DelayMs    int64    `json:"delay_ms"`
}

// Request is the full JSON run interface.
type Request struct {
	Seed  int64    `json:"seed"`
	Nodes []string `json:"nodes"`
	// Topology maps a node to its directed outgoing neighbors. A broadcast
	// (and every relay) crosses adjacency edges only; omitted topology means
	// a complete graph. Restricted topologies are what let a per-link fault
	// cause genuine, non-self-healing loss.
	Topology          map[string][]string  `json:"topology,omitempty"`
	EndTimeMs         int64                `json:"end_time_ms"`
	BufferCapacity    int                  `json:"buffer_capacity"` // 0=default 16, -1=unbounded
	Network           simnet.Policy        `json:"network"`
	Faults            []simnet.Fault       `json:"faults"`
	Broadcasts        []BroadcastSpec      `json:"broadcasts"`
	Triggers          []TriggerSpec        `json:"triggers"`
	Retransmits       []RetransmitSpec     `json:"retransmits"`
	BackpressureRetry BackpressureRetryCfg `json:"backpressure_retry"`
}

// BackpressureRetryCfg controls automatic retry of rejected copies. With
// MaxAttempts 0 (default) a rejected copy is permanently given up; -1 retries
// without limit until it is accepted or simulation ends.
type BackpressureRetryCfg struct {
	DelayMs     int64 `json:"delay_ms"`
	MaxAttempts int   `json:"max_attempts"`
}

// ---- Response model -----------------------------------------------------

// RetryEvent records the lifecycle of one backpressure retry.
type RetryEvent struct {
	Time    int64   `json:"time"`
	Node    string  `json:"node"`
	Origin  vec.Key `json:"origin"`
	Attempt int     `json:"attempt"`
	Result  string  `json:"result"` // canceled | delivered | buffered | backpressure | given_up
}

// RetransmitRecord logs one direct-injected repair copy.
type RetransmitRecord struct {
	Time   int64   `json:"time"`
	From   string  `json:"from"`
	To     string  `json:"to"`
	Origin vec.Key `json:"origin"`
	AtMs   int64   `json:"arrival_at_ms"`
}

// FinalReport summarizes the end-of-run state and permanent-loss diagnostics.
type FinalReport struct {
	EndTimeMs           int64                           `json:"end_time_ms"`
	BufferedPerNode     map[string][]vec.Key            `json:"buffered_per_node"`
	MissingForBuffered  map[string]map[string][]vec.Key `json:"missing_for_buffered"`
	RootMissingBlockers []vec.Key                       `json:"root_missing_blockers"`
	UndeliveredOrigins  []vec.Key                       `json:"undelivered_origins"`
	Diagnostics         []string                        `json:"diagnostics"`
}

// Response is the JSON run result.
type Response struct {
	Status           string                   `json:"status"` // ok | error
	Error            string                   `json:"error,omitempty"`
	Seed             int64                    `json:"seed"`
	Nodes            []string                 `json:"nodes"`
	Deliveries       []node.Delivery          `json:"deliveries"`
	Duplicates       map[string]int           `json:"duplicates_suppressed_per_node"`
	Backpressure     []node.BackpressureEvent `json:"backpressure_events"`
	Retries          []RetryEvent             `json:"retry_events"`
	NetworkDecisions []simnet.Decision        `json:"network_decisions"`
	Retransmits      []RetransmitRecord       `json:"retransmits"`
	Final            *FinalReport             `json:"final_report"`
}

// ---- Engine -------------------------------------------------------------

type engine struct {
	req    *Request
	sched  *sim.Scheduler
	rng    *rand.Rand
	net    *simnet.Network
	runErr string

	neighbors map[string][]string // deterministic sorted, outgoing links

	nodes map[string]*node.Node

	// retained[node][origin] = message; populated for every node that
	// delivered a message, so retransmits can source it from any holder.
	retained map[string]map[vec.Key]*node.Message

	// per-origin broadcast sequence counters
	seq map[string]int

	// every origin that ever exists (for final diagnostics)
	allOrigins map[vec.Key]bool

	// response accumulators
	deliveries   []node.Delivery
	backpressure []node.BackpressureEvent
	retries      []RetryEvent
	retransmits  []RetransmitRecord

	// trigger bookkeeping: index -> fired
	triggerFired map[int]bool
	// active backpressure retries: node|origin -> attempts so far
	activeRetry map[string]int
}

// validate checks the request and applies defaults.
func validate(req *Request) error {
	if len(req.Nodes) < 2 {
		return fmt.Errorf("at least 2 nodes required")
	}
	seen := map[string]bool{}
	for _, n := range req.Nodes {
		if n == "" {
			return fmt.Errorf("node id must not be empty")
		}
		if seen[n] {
			return fmt.Errorf("duplicate node id %q", n)
		}
		seen[n] = true
	}
	if req.EndTimeMs <= 0 {
		return fmt.Errorf("end_time_ms must be > 0")
	}
	if req.BufferCapacity == 0 {
		req.BufferCapacity = 16
	}
	if req.BufferCapacity < -1 {
		return fmt.Errorf("buffer_capacity must be -1 (unbounded), 0 (default) or positive")
	}
	p := req.Network
	for _, r := range []struct {
		name string
		v    float64
	}{
		{"loss_rate", p.LossRate}, {"duplicate_rate", p.DuplicateRate},
	} {
		if r.v < 0 || r.v > 1 {
			return fmt.Errorf("network.%s must be in [0,1]", r.name)
		}
	}
	if p.MinDelayMs < 0 || p.MaxDelayMs < 0 {
		return fmt.Errorf("network delays must be >= 0")
	}
	if p.MaxDelayMs > 0 && p.MaxDelayMs < p.MinDelayMs {
		return fmt.Errorf("network.max_delay_ms < min_delay_ms")
	}
	knownNode := func(id, where string) error {
		if !seen[id] {
			return fmt.Errorf("%s references unknown node %q", where, id)
		}
		return nil
	}
	if len(req.Topology) > 0 {
		for _, n := range req.Nodes {
			if _, ok := req.Topology[n]; !ok {
				return fmt.Errorf("topology must list every node (missing %q)", n)
			}
		}
		for from, tos := range req.Topology {
			if err := knownNode(from, "topology"); err != nil {
				return err
			}
			seenEdge := map[string]bool{}
			for _, to := range tos {
				if err := knownNode(to, "topology["+from+"]"); err != nil {
					return err
				}
				if to == from {
					return fmt.Errorf("topology[%s] contains a self edge", from)
				}
				if seenEdge[to] {
					return fmt.Errorf("topology[%s] lists duplicate neighbor %q", from, to)
				}
				seenEdge[to] = true
			}
		}
	}
	for i, b := range req.Broadcasts {
		if err := knownNode(b.Node, fmt.Sprintf("broadcasts[%d]", i)); err != nil {
			return err
		}
		if b.AtMs < 0 || b.AtMs >= req.EndTimeMs {
			return fmt.Errorf("broadcasts[%d].at_ms outside [0, end_time_ms)", i)
		}
	}
	for i, t := range req.Triggers {
		if err := knownNode(t.WhenNode, fmt.Sprintf("triggers[%d]", i)); err != nil {
			return err
		}
		if err := knownNode(t.AfterOrigin.Node, fmt.Sprintf("triggers[%d].after_origin", i)); err != nil {
			return err
		}
		if t.AfterOrigin.Seq <= 0 {
			return fmt.Errorf("triggers[%d].after_origin.seq must be > 0", i)
		}
	}
	for i, r := range req.Retransmits {
		if err := knownNode(r.From, fmt.Sprintf("retransmits[%d]", i)); err != nil {
			return err
		}
		if err := knownNode(r.FromOrigin.Node, fmt.Sprintf("retransmits[%d].from_origin", i)); err != nil {
			return err
		}
		if r.FromOrigin.Seq <= 0 {
			return fmt.Errorf("retransmits[%d].from_origin.seq must be > 0", i)
		}
		if r.AtMs < 0 || r.AtMs >= req.EndTimeMs {
			return fmt.Errorf("retransmits[%d].at_ms outside [0, end_time_ms)", i)
		}
		for j, to := range r.To {
			if err := knownNode(to, fmt.Sprintf("retransmits[%d].to[%d]", i, j)); err != nil {
				return err
			}
			if to == r.From {
				return fmt.Errorf("retransmits[%d].to contains source node", i)
			}
		}
		if r.DelayMs < 0 {
			return fmt.Errorf("retransmits[%d].delay_ms must be >= 0", i)
		}
	}
	validAction := map[string]bool{"deliver": true, "drop": true, "duplicate": true, "delay": true, "": false}
	for i, f := range req.Faults {
		if !validAction[f.Action] {
			return fmt.Errorf("faults[%d] has invalid action %q", i, f.Action)
		}
		if f.Action == "delay" && f.DelayMs < 0 {
			return fmt.Errorf("faults[%d].delay_ms must be >= 0", i)
		}
	}
	if req.BackpressureRetry.DelayMs < 0 {
		return fmt.Errorf("backpressure_retry.delay_ms must be >= 0")
	}
	return nil
}

// Run validates and executes one simulation.
func Run(req *Request) *Response {
	resp := &Response{
		Seed:             req.Seed,
		Nodes:            append([]string(nil), req.Nodes...),
		Deliveries:       []node.Delivery{},
		Duplicates:       map[string]int{},
		Backpressure:     []node.BackpressureEvent{},
		Retries:          []RetryEvent{},
		NetworkDecisions: []simnet.Decision{},
		Retransmits:      []RetransmitRecord{},
	}
	if err := validate(req); err != nil {
		resp.Status = "error"
		resp.Error = err.Error()
		return resp
	}

	e := &engine{
		req:          req,
		sched:        sim.NewScheduler(),
		rng:          rand.New(rand.NewSource(req.Seed)),
		nodes:        map[string]*node.Node{},
		retained:     map[string]map[vec.Key]*node.Message{},
		seq:          map[string]int{},
		allOrigins:   map[vec.Key]bool{},
		triggerFired: map[int]bool{},
		activeRetry:  map[string]int{},
		deliveries:   []node.Delivery{},
		backpressure: []node.BackpressureEvent{},
		retries:      []RetryEvent{},
		retransmits:  []RetransmitRecord{},
	}
	bufCap := req.BufferCapacity
	for _, id := range req.Nodes {
		e.nodes[id] = node.New(id, bufCap)
		e.retained[id] = map[vec.Key]*node.Message{}
	}

	// Outgoing neighbors: explicit topology, or complete graph by default.
	e.neighbors = map[string][]string{}
	for _, id := range req.Nodes {
		tos, ok := req.Topology[id]
		if !ok {
			tos = req.Nodes
		}
		ns := append([]string(nil), tos...)
		sort.Strings(ns)
		filtered := ns[:0]
		for _, n := range ns {
			if n != id {
				filtered = append(filtered, n)
			}
		}
		e.neighbors[id] = filtered
	}

	// Network sink: schedule an arrival event for the copy.
	sink := func(at int64, to string, m *node.Message) {
		cp := *m
		e.sched.At(at, "arrive", &arrivePayload{to: to, msg: &cp})
	}
	e.net = simnet.New(req.Network, req.Faults, e.rng, sink)

	for i, b := range req.Broadcasts {
		e.sched.At(b.AtMs, "broadcast", req.Broadcasts[i])
	}
	for i, r := range req.Retransmits {
		e.sched.At(r.AtMs, "retransmit", req.Retransmits[i])
	}
	// End sentinel past the window: every event at t <= endTime precedes it,
	// same-time events inserted before it precede it (FIFO tie-break).
	e.sched.At(req.EndTimeMs+1, "end", nil)

	e.sched.Run(req.EndTimeMs, e.handle)

	if e.runErr != "" {
		resp.Status = "error"
		resp.Error = e.runErr
		return resp
	}

	resp.Status = "ok"
	e.fillFinal(resp)
	resp.Deliveries = e.deliveries
	if resp.Deliveries == nil {
		resp.Deliveries = []node.Delivery{}
	}
	resp.Backpressure = e.backpressure
	if resp.Backpressure == nil {
		resp.Backpressure = []node.BackpressureEvent{}
	}
	resp.Retries = e.retries
	if resp.Retries == nil {
		resp.Retries = []RetryEvent{}
	}
	resp.Retransmits = e.retransmits
	if resp.Retransmits == nil {
		resp.Retransmits = []RetransmitRecord{}
	}
	resp.NetworkDecisions = e.net.Log()
	if resp.NetworkDecisions == nil {
		resp.NetworkDecisions = []simnet.Decision{}
	}
	resp.Duplicates = map[string]int{}
	for _, id := range req.Nodes {
		if d := e.nodes[id].DuplicateCount(); d > 0 {
			resp.Duplicates[id] = d
		}
	}
	return resp
}

type arrivePayload struct {
	to        string
	msg       *node.Message
	retryNode string // non-empty when this is a backpressure retry
	retryKey  vec.Key
	attempt   int
}

func retryID(n string, k vec.Key) string { return n + "|" + k.String() }

func (e *engine) handle(ev *sim.Event) {
	if e.runErr != "" {
		return
	}
	switch ev.Kind {
	case "broadcast":
		b := ev.Data.(BroadcastSpec)
		e.doBroadcast(ev.Time, b.Node, b.Payload)
	case "arrive":
		p := ev.Data.(*arrivePayload)
		e.onArrive(ev.Time, p)
	case "retransmit":
		r := ev.Data.(RetransmitSpec)
		e.onRetransmit(ev.Time, r)
	case "end":
		// never fires (Run stops at t < endTime) but kept for clarity
	}
}

// doBroadcast creates a new message at fromNode, delivers it locally, stores
// it for retransmits and floods one copy per other node via the network.
func (e *engine) doBroadcast(now int64, fromNode, payload string) {
	nd := e.nodes[fromNode]
	e.seq[fromNode]++
	seq := e.seq[fromNode]
	m := nd.Broadcast(seq, payload)
	e.allOrigins[m.Origin] = true
	e.retained[fromNode][m.Origin] = m

	delivered := nd.HandleLocal(m)
	e.recordDeliveries(now, fromNode, delivered)
	e.maybeTrigger(now, fromNode, delivered)

	for _, to := range e.neighbors[fromNode] {
		cp := *m
		cp.From = fromNode
		e.net.Send(now, to, &cp)
	}
}

func (e *engine) onArrive(now int64, p *arrivePayload) {
	// Backpressure-retry bookkeeping: if the message made it in by another
	// path while waiting, the scheduled retry is a no-op.
	if p.retryNode != "" {
		id := retryID(p.retryNode, p.retryKey)
		if e.nodes[p.retryNode].Has(p.retryKey) {
			e.retries = append(e.retries, RetryEvent{
				Time: now, Node: p.retryNode, Origin: p.retryKey,
				Attempt: p.attempt, Result: "canceled",
			})
			delete(e.activeRetry, id)
			return
		}
	}

	nd := e.nodes[p.to]
	outcome, delivered, bp := nd.Handle(now, p.msg)

	switch outcome {
	case node.Duplicate:
		// nothing to log beyond per-node counter
		if p.retryNode != "" {
			delete(e.activeRetry, retryID(p.retryNode, p.retryKey))
		}
	case node.Buffered:
		if p.retryNode != "" {
			e.retries = append(e.retries, RetryEvent{
				Time: now, Node: p.to, Origin: p.msg.Origin,
				Attempt: p.attempt, Result: "buffered",
			})
			delete(e.activeRetry, retryID(p.to, p.msg.Origin))
		}
	case node.Delivered:
		if p.retryNode != "" {
			e.retries = append(e.retries, RetryEvent{
				Time: now, Node: p.to, Origin: p.msg.Origin,
				Attempt: p.attempt, Result: "delivered",
			})
			delete(e.activeRetry, retryID(p.to, p.msg.Origin))
		}
		// retain + flood so a recovered predecessor continues propagating
		for _, dm := range delivered {
			e.retained[p.to][dm.Origin] = dm
		}
		e.recordDeliveries(now, p.to, delivered)
		e.maybeTrigger(now, p.to, delivered)
		for _, dm := range delivered {
			e.relay(now, p.to, dm)
		}
	case node.Backpressure:
		e.backpressure = append(e.backpressure, *bp)
		e.scheduleRetry(now, p.to, p.msg, p.attempt)
	}
}

// relay floods a newly delivered message onward from node id (one network
// copy to every other node; receivers de-duplicate, so this terminates).
func (e *engine) relay(now int64, id string, dm *node.Message) {
	for _, to := range e.neighbors[id] {
		cp := *dm
		cp.From = id
		cp.Sender = id
		e.net.Send(now, to, &cp)
	}
}

func (e *engine) scheduleRetry(now int64, to string, m *node.Message, priorAttempts int) {
	cfg := e.req.BackpressureRetry
	id := retryID(to, m.Origin)
	attempt := priorAttempts
	if _, active := e.activeRetry[id]; active {
		attempt = e.activeRetry[id]
	}
	attempt++
	if cfg.MaxAttempts == 0 {
		// Retries disabled: the rejection is terminal for this copy.
		e.retries = append(e.retries, RetryEvent{
			Time: now, Node: to, Origin: m.Origin,
			Attempt: 0, Result: "given_up",
		})
		delete(e.activeRetry, id)
		return
	}
	if cfg.MaxAttempts != -1 && attempt > cfg.MaxAttempts {
		e.retries = append(e.retries, RetryEvent{
			Time: now, Node: to, Origin: m.Origin,
			Attempt: attempt - 1, Result: "given_up",
		})
		delete(e.activeRetry, id)
		return
	}
	e.activeRetry[id] = attempt
	cp := *m
	p := &arrivePayload{
		to: to, msg: &cp, retryNode: to, retryKey: m.Origin, attempt: attempt,
	}
	e.sched.At(now+cfg.DelayMs, "arrive", p)
}

func (e *engine) onRetransmit(now int64, r RetransmitSpec) {
	holders, ok := e.retained[r.From]
	if !ok {
		e.runErr = fmt.Sprintf("retransmit source node %q missing", r.From)
		return
	}
	m, ok := holders[r.FromOrigin]
	if !ok {
		e.runErr = fmt.Sprintf("node %q never held message %s by retransmit time %d",
			r.From, r.FromOrigin.String(), r.AtMs)
		return
	}
	for _, to := range r.To {
		cp := *m
		cp.From = r.From
		cp.Sender = r.From
		e.retransmits = append(e.retransmits, RetransmitRecord{
			Time: now, From: r.From, To: to, Origin: m.Origin, AtMs: now + r.DelayMs,
		})
		// Direct injection: faults/stochastic policy do not apply.
		e.sched.At(now+r.DelayMs, "arrive", &arrivePayload{to: to, msg: &cp})
	}
}

// recordDeliveries appends application-level delivery records in the exact
// causal-safe order the node handed them back.
func (e *engine) recordDeliveries(now int64, nodeID string, msgs []*node.Message) {
	for _, m := range msgs {
		e.deliveries = append(e.deliveries, node.Delivery{
			Time: now, Node: nodeID, Origin: m.Origin,
			Payload: m.Payload, Clock: e.nodes[nodeID].Clock(),
		})
	}
}

// maybeTrigger fires each not-yet-fired trigger matching this delivery batch.
// Triggers are evaluated in request order for determinism.
func (e *engine) maybeTrigger(now int64, nodeID string, msgs []*node.Message) {
	got := map[vec.Key]bool{}
	for _, m := range msgs {
		got[m.Origin] = true
	}
	for i, t := range e.req.Triggers {
		if e.triggerFired[i] {
			continue
		}
		if t.WhenNode == nodeID && got[t.AfterOrigin] {
			e.triggerFired[i] = true
			// Same virtual time: FIFO ordering places this broadcast after
			// the delivery that caused it but before later arrivals.
			e.doBroadcast(now, nodeID, t.Payload)
		}
	}
}

func (e *engine) fillFinal(resp *Response) {
	fin := &FinalReport{
		EndTimeMs:           e.req.EndTimeMs,
		BufferedPerNode:     map[string][]vec.Key{},
		MissingForBuffered:  map[string]map[string][]vec.Key{},
		RootMissingBlockers: []vec.Key{},
		UndeliveredOrigins:  []vec.Key{},
		Diagnostics:         []string{},
	}

	// Expected vector: maximum per-origin seq produced by any broadcast.
	expected := vec.Clock{}
	for k := range e.allOrigins {
		if expected[k.Node] < k.Seq {
			expected[k.Node] = k.Seq
		}
	}

	// Per-node buffered state and its missing dependencies.
	for _, id := range e.req.Nodes {
		nd := e.nodes[id]
		if keys := nd.BufferKeys(); len(keys) > 0 {
			fin.BufferedPerNode[id] = keys
			fin.MissingForBuffered[id] = nd.BufferedMissing()
		}
	}

	// undelivered: expected origins missing at at least one node.
	// root blockers: origins delivered at exactly their originating node (or
	// nowhere) — no other node holds a copy, so neither flooding between
	// receivers nor a buffered release can ever repair them within the run.
	deliveredNodes := map[vec.Key]map[string]bool{}
	for _, d := range e.deliveries {
		set := deliveredNodes[d.Origin]
		if set == nil {
			set = map[string]bool{}
			deliveredNodes[d.Origin] = set
		}
		set[d.Node] = true
	}
	var roots []vec.Key
	var undelivered []vec.Key
	for n, max := range expected {
		for s := 1; s <= max; s++ {
			k := vec.Key{Node: n, Seq: s}
			holders := deliveredNodes[k]
			if len(holders) < len(e.req.Nodes) {
				undelivered = append(undelivered, k)
			}
			if len(holders) == 0 || (len(holders) == 1 && holders[k.Node]) {
				roots = append(roots, k)
			}
		}
	}
	sort.Slice(roots, func(i, j int) bool { return roots[i].Less(roots[j]) })
	sort.Slice(undelivered, func(i, j int) bool { return undelivered[i].Less(undelivered[j]) })
	if roots == nil {
		roots = []vec.Key{}
	}
	if undelivered == nil {
		undelivered = []vec.Key{}
	}
	fin.RootMissingBlockers = roots
	fin.UndeliveredOrigins = undelivered

	// Human-readable diagnostics (deterministic).
	var diags []string
	if len(roots) > 0 {
		strs := make([]string, len(roots))
		for i, k := range roots {
			strs[i] = k.String()
		}
		diags = append(diags, "PERMANENT LOSS: only the originating node holds these messages; "+
			"no receiver can repair them by retransmission within end_time_ms: "+
			strings.Join(strs, ", "))
	}
	for _, id := range e.req.Nodes {
		if miss, ok := fin.MissingForBuffered[id]; ok && len(miss) > 0 {
			var parts []string
			keys := make([]string, 0, len(miss))
			for k := range miss {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			for _, bk := range keys {
				deps := miss[bk]
				ds := make([]string, len(deps))
				for i, d := range deps {
					ds[i] = d.String()
				}
				parts = append(parts, fmt.Sprintf("%s waits on [%s]", bk, strings.Join(ds, ",")))
			}
			diags = append(diags, fmt.Sprintf("NODE %s: %s", id, strings.Join(parts, "; ")))
		}
	}
	if len(diags) == 0 {
		diags = append(diags, "all broadcasts delivered to all nodes in causal order")
	}
	fin.Diagnostics = diags
	resp.Final = fin
}

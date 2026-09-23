package sim

import (
	"container/heap"
	"fmt"
	"sort"
)

// msgType is request (client->node) or response (node->client).
type message struct {
	from, to string
	role     string // "A" or "B": operation role in its pair (dead nodes get "dead")
	msgType  string // "request" | "response"
	opKind   string // "read" | "write"
	opID     string
	attempt  int
}

type event struct {
	t   int
	seq int
	msg message
}

type eventHeap []event

func (h eventHeap) Len() int { return len(h) }
func (h eventHeap) Less(i, j int) bool {
	return h[i].t < h[j].t || (h[i].t == h[j].t && h[i].seq < h[j].seq)
}
func (h eventHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }
func (h *eventHeap) Push(x any)   { *h = append(*h, x.(event)) }
func (h *eventHeap) Pop() any {
	old := *h
	n := len(old)
	x := old[n-1]
	*h = old[:n-1]
	return x
}

type opState struct {
	id        string
	kind      string
	role      string
	attempt   int
	responded map[string]int // node -> accumulated response weight (dedup per attempt set)
	weight    int
	done      bool
	doneAt    int
}

func (o *opState) quorumThreshold(rq, wq int) int {
	if o.kind == "read" {
		return rq
	}
	return wq
}

type engine struct {
	in         RunInput
	weightOf   map[string]int
	alive      map[string]bool
	pol        policy
	rng        *rng
	horizon    int
	timeout    int
	maxAttempt int
	traceCap   int

	h       *eventHeap
	seq     int
	now     int
	ops     map[string]*opState
	order   []string
	stats   RunResult
	trace   []TraceEvent
	stopped bool
}

const (
	defaultHorizon    = 200
	defaultTimeout    = 20
	defaultMaxAttempt = 8
	defaultRuns       = 20
	traceCap          = 400
)

// Run executes the simulator request and aggregates all runs.
func Run(in RunInput) *Report {
	if errs := validateInput(in); len(errs) > 0 {
		return &Report{Errors: errs, Safe: true}
	}

	cfg := in.Sim
	runs := cfg.Runs
	if runs <= 0 {
		runs = defaultRuns
	}
	scripted := len(cfg.Rules) > 0
	if scripted {
		runs = 1 // rules are deterministic; multiple runs would be identical
	}

	rep := &Report{Runs: runs, Scripted: scripted, Safe: true, AllComplete: true}
	for i := 0; i < runs; i++ {
		seed := cfg.Seed
		if !scripted {
			// splitmix stream advance per run keeps runs independent yet
			// reproducible from one top-level seed.
			seed = mix(seed, uint64(i)+1)
		}
		rr := runOnce(in, seed, scripted, i < 8)
		rep.CompletedTotal += rr.Completed
		rep.TimedOutTotal += rr.TimedOut
		if len(rr.Violations) > 0 {
			rep.ViolationRuns++
			rep.Safe = false
			if len(rep.Violations) < 20 {
				rep.Violations = append(rep.Violations, rr.Violations...)
			}
		}
		if rr.TimedOut > 0 {
			rep.AllComplete = false
		}
		rep.RunResults = append(rep.RunResults, rr)
	}
	return rep
}

func mix(seed, x uint64) uint64 {
	z := seed + x*0x9E3779B97F4A7C15
	z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9
	z = (z ^ (z >> 27)) * 0x94D049BB133111EB
	return z ^ (z >> 31)
}

func runOnce(in RunInput, seed uint64, scripted, keepDetail bool) RunResult {
	cfg := in.Sim
	r := newRNG(seed)
	e := &engine{
		in:         in,
		weightOf:   map[string]int{},
		alive:      map[string]bool{},
		horizon:    orDefault(cfg.Horizon, defaultHorizon),
		timeout:    orDefault(cfg.ClientTimeout, defaultTimeout),
		maxAttempt: orDefault(cfg.MaxAttempts, defaultMaxAttempt),
		traceCap:   traceCap,
		h:          &eventHeap{},
		ops:        map[string]*opState{},
		rng:        r,
	}

	down := map[string]bool{}
	for _, d := range cfg.FailedDomains {
		for _, n := range in.Nodes {
			nd := n.Domain
			if nd == "" {
				nd = "default"
			}
			if nd == d {
				down[n.ID] = true
			}
		}
	}
	for _, n := range in.Nodes {
		e.weightOf[n.ID] = n.Weight
		e.alive[n.ID] = !down[n.ID]
	}
	if scripted {
		e.pol = &scriptedPolicy{rules: cfg.Rules}
	} else {
		nw := cfg.Network
		p := &stochasticPolicy{minD: 1, maxD: 2}
		if nw != nil {
			p.loss = nw.LossRate
			p.dup = nw.DuplicateRate
			if nw.MinDelay > 0 {
				p.minD = nw.MinDelay
			}
			if nw.MaxDelay > 0 {
				p.maxD = nw.MaxDelay
			}
			if p.maxD < p.minD {
				p.maxD = p.minD
			}
			p.reorderWindow = nw.ReorderWindow
		}
		e.pol = p
	}
	heap.Init(e.h)

	nOps := cfg.Operations
	if nOps <= 0 {
		nOps = 2
	}
	kind := cfg.Kind
	if kind == "" {
		kind = "mixed"
	}

	// Launch operations. Pairs (A,B) start simultaneously so their quorum
	// gathering overlaps: a completed disjoint responder set is exactly the
	// intersection-safety hazard.
	for i := 0; i < nOps; i++ {
		role := "A"
		if i%2 == 1 {
			role = "B"
		}
		k := "write"
		switch kind {
		case "ww":
			k = "write"
		case "rw":
			if role == "A" {
				k = "read"
			} else {
				k = "write"
			}
		default: // mixed
			if i%2 == 0 {
				k = "write"
			} else {
				k = "read"
			}
		}
		id := fmt.Sprintf("op%d-%s", i+1, k)
		o := &opState{id: id, kind: k, role: role, responded: map[string]int{}}
		e.ops[id] = o
		e.order = append(e.order, id)
		e.attempt(o)
	}

	for e.h.Len() > 0 && e.now <= e.horizon && !e.stopped {
		ev := heap.Pop(e.h).(event)
		e.now = ev.t
		if e.now > e.horizon {
			break
		}
		e.deliver(ev.msg)
	}
	if e.now > e.stats.MaxTime {
		e.stats.MaxTime = e.now
	}

	e.stats.Seed = seed
	e.stats.Completed, e.stats.TimedOut = e.collect()
	e.findViolations()
	if !keepDetail {
		e.stats.Trace = nil
		e.stats.CompletedOps = nil
	} else {
		e.stats.CompletedOps = e.summaries()
		e.stats.Trace = e.trace
	}
	return e.stats
}

func (e *engine) attempt(o *opState) {
	if o.done || o.attempt >= e.maxAttempt {
		if !o.done {
			e.log(e.now, "give_up", fmt.Sprintf("%s after %d attempts", o.id, o.attempt))
		}
		return
	}
	o.attempt++
	// Retransmission must NOT discard responses already received: the client
	// collects unique node responses across attempts, and weight is the sum
	// of that set. Resetting it here would double-count a node that answers
	// on two different attempts (a node deduped out of the responder set but
	// still added to the weight).
	e.log(e.now, "attempt", fmt.Sprintf("%s attempt=%d role=%s", o.id, o.attempt, o.role))
	for _, n := range e.in.Nodes {
		m := message{
			from: "client", to: n.ID, role: o.role,
			msgType: "request", opKind: o.kind, opID: o.id, attempt: o.attempt,
		}
		if !e.alive[n.ID] {
			m.role = "dead"
		}
		e.stats.RequestsSent++
		e.transmit(m)
	}
	// Retransmission/give-up timer.
	e.schedule(e.now+e.timeout, message{
		from: "timer", to: "client", role: o.role,
		msgType: "timeout", opKind: o.kind, opID: o.id, attempt: o.attempt,
	})
}

func (e *engine) deliver(m message) {
	switch m.msgType {
	case "timeout":
		o := e.ops[m.opID]
		if o == nil || o.done || o.attempt != m.attempt {
			return
		}
		e.attempt(o)
	case "request":
		if !e.alive[m.to] {
			// Dead domain: messages vanish (policy already drops dead sends,
			// but a scripted rule may deliver one).
			return
		}
		// Node answers every request; the client dedupes by node.
		resp := message{
			from: m.to, to: "client", role: m.role,
			msgType: "response", opKind: m.opKind, opID: m.opID, attempt: m.attempt,
		}
		e.transmit(resp)
	case "response":
		o := e.ops[m.opID]
		if o == nil || o.done {
			return
		}
		if _, seen := o.responded[m.from]; seen {
			e.log(e.now, "duplicate_response", fmt.Sprintf("%s <- %s (ignored)", o.id, m.from))
			return
		}
		wt := e.weightOf[m.from]
		o.responded[m.from] = wt
		o.weight += wt
		e.stats.ResponsesDelivered++
		if o.weight >= o.quorumThreshold(e.in.ReadQuorum, e.in.WriteQuorum) {
			o.done = true
			o.doneAt = e.now
			e.log(e.now, "quorum", fmt.Sprintf("%s weight=%d", o.id, o.weight))
		}
	}
}

func (e *engine) transmit(m message) {
	if m.msgType == "request" && m.role == "dead" {
		e.stats.Dropped++
		e.log(e.now, "drop", fmt.Sprintf("%s -> %s (domain down)", m.from, m.to))
		return
	}
	copies, delays := e.pol.decide(m, e.rng)
	for i := 0; i < copies; i++ {
		e.schedule(e.now+delays[i], m)
		if i > 0 {
			e.stats.Duplicated++
			e.log(e.now, "duplicate", fmt.Sprintf("%s -> %s (%s)", m.from, m.to, m.opID))
		}
	}
	if copies == 0 {
		e.stats.Dropped++
		e.log(e.now, "drop", fmt.Sprintf("%s -> %s (%s)", m.from, m.to, m.opID))
	}
}

func (e *engine) schedule(t int, m message) {
	e.seq++
	heap.Push(e.h, event{t: t, seq: e.seq, msg: m})
}

func (e *engine) log(t int, typ, detail string) {
	if len(e.trace) < e.traceCap {
		e.trace = append(e.trace, TraceEvent{T: t, Type: typ, Detail: detail})
	}
}

func (e *engine) collect() (completed, timedOut int) {
	for _, id := range e.order {
		if e.ops[id].done {
			completed++
		} else {
			timedOut++
		}
	}
	return
}

func (e *engine) summaries() []OpSummary {
	var out []OpSummary
	for _, id := range e.order {
		o := e.ops[id]
		if !o.done {
			continue
		}
		nodes := make([]string, 0, len(o.responded))
		for n := range o.responded {
			nodes = append(nodes, n)
		}
		sort.Strings(nodes)
		out = append(out, OpSummary{
			Op: o.id, Kind: o.kind, Responders: nodes,
			Weight: o.weight, CompletedAt: o.doneAt,
		})
	}
	return out
}

// findViolations compares every pair of completed operations: disjoint
// responder sets between two writes, or between a read and a write, are
// reported.
func (e *engine) findViolations() {
	done := make([]*opState, 0, len(e.order))
	for _, id := range e.order {
		if o := e.ops[id]; o.done {
			done = append(done, o)
		}
	}
	for i := 0; i < len(done); i++ {
		for j := i + 1; j < len(done); j++ {
			a, b := done[i], done[j]
			if !disjoint(a.responded, b.responded) {
				continue
			}
			if a.kind == "write" && b.kind == "write" {
				e.addViolation(a, b, "ww")
			} else if (a.kind == "read" && b.kind == "write") || (a.kind == "write" && b.kind == "read") {
				e.addViolation(a, b, "rw")
			}
		}
	}
}

func (e *engine) addViolation(a, b *opState, kind string) {
	e.stats.Violations = append(e.stats.Violations, Violation{
		OpA: a.id, OpB: b.id, Kind: kind,
		RespondersA: sortedKeys(a.responded),
		RespondersB: sortedKeys(b.responded),
	})
}

func disjoint(a, b map[string]int) bool {
	small, large := a, b
	if len(b) < len(a) {
		small, large = b, a
	}
	for k := range small {
		if _, ok := large[k]; ok {
			return false
		}
	}
	return true
}

func sortedKeys(m map[string]int) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func orDefault(v, d int) int {
	if v <= 0 {
		return d
	}
	return v
}

func validateInput(in RunInput) []string {
	var errs []string
	if len(in.Nodes) == 0 {
		errs = append(errs, "nodes: at least one node is required")
	}
	if in.ReadQuorum <= 0 || in.WriteQuorum <= 0 {
		errs = append(errs, "read_quorum and write_quorum must be positive")
	}
	seen := map[string]bool{}
	domains := map[string]bool{}
	for i, n := range in.Nodes {
		if n.ID == "" {
			errs = append(errs, fmt.Sprintf("nodes[%d]: empty id", i))
		} else if seen[n.ID] {
			errs = append(errs, fmt.Sprintf("nodes[%d]: duplicate node id %q", i, n.ID))
		}
		seen[n.ID] = true
		if n.Weight < 0 {
			errs = append(errs, fmt.Sprintf("nodes[%d] (%s): negative weight", i, n.ID))
		}
		d := n.Domain
		if d == "" {
			d = "default"
		}
		domains[d] = true
	}
	if nw := in.Sim.Network; nw != nil {
		if nw.LossRate < 0 || nw.LossRate > 1 {
			errs = append(errs, "sim.network.loss_rate must be in [0,1]")
		}
		if nw.DuplicateRate < 0 || nw.DuplicateRate > 1 {
			errs = append(errs, "sim.network.duplicate_rate must be in [0,1]")
		}
		if nw.MaxDelay > 0 && nw.MinDelay > nw.MaxDelay {
			errs = append(errs, "sim.network.min_delay must be <= max_delay")
		}
	}
	validAction := map[string]bool{"deliver": true, "drop": true, "duplicate": true}
	for i, ru := range in.Sim.Rules {
		if !validAction[ru.Action] {
			errs = append(errs, fmt.Sprintf("sim.rules[%d]: invalid action %q", i, ru.Action))
		}
	}
	for _, d := range in.Sim.FailedDomains {
		if !domains[d] {
			errs = append(errs, fmt.Sprintf("sim.failed_domains: unknown domain %q", d))
		}
	}
	switch in.Sim.Kind {
	case "", "ww", "rw", "mixed":
	default:
		errs = append(errs, fmt.Sprintf("sim.kind: must be one of ww, rw, mixed, got %q", in.Sim.Kind))
	}
	return errs
}

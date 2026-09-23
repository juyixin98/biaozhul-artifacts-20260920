package sim

import (
	"fmt"
	"sort"

	"chsim/internal/ring"
)

type taskState struct {
	t        ring.Task
	id       string
	cursor   int
	done     bool
	active   bool // a fetch/put round is in flight
	phase    string
	gen      int
	timerSeq int // each (re)armed timeout supersedes the previous one
	attempts int
	batch    []kv
	allDone  bool // the fetch side reported the range exhausted
}

type readResp struct {
	nodeID string
	rec    Record
	found  bool
}

type clientOp struct {
	id       uint64
	kind     string // "write" | "read"
	clientID string
	key      string
	value    string
	version  uint64
	epoch    uint64
	gen      int
	attempts int
	remain   map[string]bool
	reads    []readResp
	minConf  uint64 // confirmed version the key had when a read was issued
}

// Router is the coordinator: it owns the ring state machine, routes client
// requests (dual-read/dual-write while a migration is in flight), drives range
// transfers, cuts over at the barrier and decommissions drained nodes.
type Router struct {
	s *Sim

	members map[string]int
	ring    *ring.Ring
	oldRing *ring.Ring // non-nil while migrating
	epoch   uint64

	migrating bool
	paused    bool

	clock   uint64 // stamps a globally monotone version on each write
	nextReq uint64
	pending map[uint64]*clientOp

	// migration bookkeeping
	tasks     map[string]*taskState
	taskOrder []string
	inflight  int
	migKeys   int
	migBytes  int
	queued    []Op
	leaving   map[string]bool
	decomm    map[string]bool

	opsByEpoch map[uint64]int
}

func newRouter(s *Sim) *Router {
	return &Router{
		s:          s,
		members:    make(map[string]int),
		pending:    make(map[uint64]*clientOp),
		tasks:      make(map[string]*taskState),
		leaving:    make(map[string]bool),
		decomm:     make(map[string]bool),
		opsByEpoch: make(map[uint64]int),
		epoch:      1,
	}
}

func (r *Router) newReqID() uint64 { r.nextReq++; return r.nextReq }

// targetsFor returns the node(s) a request must touch. During a migration the
// old and new owner both participate; after the barrier only the new ring is
// used.
func (r *Router) targetsFor(key string) []string {
	if !r.migrating {
		return []string{r.ring.Owner(key)}
	}
	o := r.oldRing.Owner(key)
	n := r.ring.Owner(key)
	if o == n {
		return []string{n}
	}
	if o < n {
		return []string{o, n}
	}
	return []string{n, o}
}

func (r *Router) taskRange(id string) ring.Task {
	if ts := r.tasks[id]; ts != nil {
		return ts.t
	}
	return ring.Task{}
}

// ---------------------------------------------------------------------------
// Client write path
// ---------------------------------------------------------------------------

func (r *Router) clientWrite(clientID, key, value string) {
	r.clock++
	r.s.result.Stats.WritesIssued++

	op := &clientOp{
		id:       r.newReqID(),
		kind:     "write",
		clientID: clientID,
		key:      key,
		value:    value,
		version:  r.clock,
		epoch:    r.epoch,
		attempts: 1,
	}
	r.pending[op.id] = op
	r.opsByEpoch[op.epoch]++
	r.dispatchWrite(op)
	r.armOpTimeout(op)
}

func (r *Router) dispatchWrite(op *clientOp) {
	targets := r.targetsFor(op.key)
	op.remain = make(map[string]bool, len(targets))
	for _, t := range targets {
		op.remain[t] = true
	}
	for _, t := range targets {
		r.s.send(routerAddr, t, message{msgClientPut{
			ReqID: op.id, Gen: op.gen, Key: op.key, Value: op.value, Version: op.version,
		}})
	}
}

// ---------------------------------------------------------------------------
// Client read path: dual-read with a single version winner
// ---------------------------------------------------------------------------

func (r *Router) clientGet(clientID, key string) {
	r.s.result.Stats.ReadsIssued++
	op := &clientOp{
		id:       r.newReqID(),
		kind:     "read",
		clientID: clientID,
		key:      key,
		epoch:    r.epoch,
		attempts: 1,
		minConf:  r.s.maxConfirmedVersion(key),
	}
	r.pending[op.id] = op
	r.opsByEpoch[op.epoch]++
	r.dispatchRead(op)
	r.armOpTimeout(op)
}

func (r *Router) dispatchRead(op *clientOp) {
	targets := r.targetsFor(op.key)
	op.remain = make(map[string]bool, len(targets))
	op.reads = op.reads[:0]
	for _, t := range targets {
		op.remain[t] = true
	}
	for _, t := range targets {
		r.s.send(routerAddr, t, message{msgClientGet{ReqID: op.id, Gen: op.gen, Key: op.key}})
	}
}

// ---------------------------------------------------------------------------
// Retries
// ---------------------------------------------------------------------------

func (r *Router) armOpTimeout(op *clientOp) {
	r.s.after(r.s.cfg.OpTimeoutMs, func() {
		cur := r.pending[op.id]
		if cur == nil || cur.gen != op.gen {
			return // already completed or superseded by another timeout
		}
		if op.attempts >= r.s.cfg.MaxAttempts {
			r.failOp(op, "exhausted attempts")
			return
		}
		op.gen++
		op.attempts++
		r.s.result.Stats.Retries++
		if op.kind == "write" {
			r.dispatchWrite(op)
		} else {
			r.dispatchRead(op)
		}
		r.armOpTimeout(op)
	})
}

func (r *Router) failOp(op *clientOp, _ string) {
	delete(r.pending, op.id)
	r.opsByEpoch[op.epoch]--
	if op.kind == "write" {
		r.s.result.Stats.WritesFailed++
	} else {
		r.s.result.Stats.ReadsFailed++
	}
	r.maybeDecommission()
}

func (r *Router) completeWrite(op *clientOp) {
	delete(r.pending, op.id)
	r.opsByEpoch[op.epoch]--
	r.s.result.Stats.WritesConfirmed++
	r.s.onWriteConfirmed(op.key, Record{Value: op.value, Version: op.version})
	r.maybeDecommission()
}

func (r *Router) completeRead(op *clientOp) {
	delete(r.pending, op.id)
	r.opsByEpoch[op.epoch]--
	r.s.result.Stats.ReadsCompleted++

	// Single-version decision: the highest version returned by any replica
	// wins. Ties are the same idempotent write, so values agree.
	best, found := Record{}, false
	for _, rr := range op.reads {
		if rr.found && (!found || rr.rec.Version > best.Version) {
			best, found = rr.rec, true
		}
	}
	r.s.onReadResult(op.key, best, found, op.minConf)
	r.maybeDecommission()
}

// ---------------------------------------------------------------------------
// Node -> router
// ---------------------------------------------------------------------------

func (r *Router) handle(_ string, v any) {
	switch m := v.(type) {
	case msgPutAck:
		op := r.pending[m.ReqID]
		if op == nil || op.gen != m.Gen || op.kind != "write" {
			return
		}
		if op.remain[m.NodeID] {
			delete(op.remain, m.NodeID)
		}
		if len(op.remain) == 0 {
			r.completeWrite(op)
		}

	case msgGetResp:
		op := r.pending[m.ReqID]
		if op == nil || op.gen != m.Gen || op.kind != "read" {
			return
		}
		if !op.remain[m.NodeID] {
			return
		}
		delete(op.remain, m.NodeID)
		op.reads = append(op.reads, readResp{nodeID: m.NodeID, rec: m.Rec, found: m.Found})
		if len(op.remain) == 0 {
			r.completeRead(op)
		}

	case msgBatch:
		ts := r.tasks[m.TaskID]
		if ts == nil || ts.done || !ts.active || ts.gen != m.Gen || ts.phase != "fetch" {
			return
		}
		ts.cursor = m.NextCursor
		ts.batch = m.Records
		ts.allDone = m.Done
		ts.phase = "put"
		if len(m.Records) == 0 {
			if m.Done {
				r.finishTask(ts)
			} else {
				r.beginFetch(ts)
			}
			return
		}
		r.s.send(routerAddr, ts.t.Dst, message{msgTransferPut{
			TaskID: ts.id, Gen: ts.gen, Records: m.Records,
		}})
		r.armTransferTimeout(ts)

	case msgTransferAck:
		ts := r.tasks[m.TaskID]
		if ts == nil || ts.done || !ts.active || ts.gen != m.Gen || ts.phase != "put" {
			return
		}
		for _, p := range ts.batch {
			r.migKeys++
			r.migBytes += len(p.Key) + len(p.Rec.Value)
			r.s.migratedKeys[p.Key] = true
		}
		ts.batch = nil
		if ts.allDone {
			r.finishTask(ts)
			return
		}
		r.beginFetch(ts)

	case msgDecommissionAck:
		if !r.decomm[m.NodeID] {
			return
		}
		delete(r.decomm, m.NodeID)
		delete(r.leaving, m.NodeID)
		n := r.s.nodes[m.NodeID]
		if n != nil {
			r.s.removed[m.NodeID] = n
			delete(r.s.nodes, m.NodeID)
		}
		r.s.result.Stats.NodesDecommissioned++
	}
}

// ---------------------------------------------------------------------------
// Migration control
// ---------------------------------------------------------------------------

// control applies a scenario control op. Ring changes requested while a
// migration is running are queued and start at the next barrier in FIFO order.
func (r *Router) control(op Op) {
	switch op.Op {
	case "pause_migration":
		if r.migrating {
			r.paused = true
		}
	case "resume_migration":
		if r.migrating {
			r.paused = false
			r.scheduleTransfers()
		}
	case "add_node", "remove_node":
		if r.migrating {
			r.queued = append(r.queued, op)
			return
		}
		r.startChange(op)
	}
}

func (r *Router) startChange(op Op) {
	if op.Op == "add_node" {
		if r.members[op.NodeID] > 0 || r.leaving[op.NodeID] {
			r.s.addError(fmt.Sprintf("add_node: node %q already exists", op.NodeID))
			return
		}
		w := op.Weight
		if w <= 0 {
			w = 1
		}
		r.members[op.NodeID] = w
		r.s.nodes[op.NodeID] = newNode(op.NodeID)
	} else {
		if r.leaving[op.NodeID] || r.members[op.NodeID] == 0 {
			r.s.addError(fmt.Sprintf("remove_node: node %q not found", op.NodeID))
			return
		}
		if len(r.members) <= 1 {
			r.s.addError(fmt.Sprintf("remove_node: cannot remove the last node %q", op.NodeID))
			return
		}
		delete(r.members, op.NodeID)
		r.leaving[op.NodeID] = true
	}

	newRing := ring.Build(r.nodesSpec(), r.s.cfg.Vnodes)
	plan := ring.Plan(r.ring, newRing)

	r.oldRing = r.ring
	r.ring = newRing
	r.migrating = true
	r.paused = false
	r.migKeys, r.migBytes = 0, 0
	r.tasks = make(map[string]*taskState)
	r.taskOrder = r.taskOrder[:0]
	r.inflight = 0
	for _, t := range plan {
		ts := &taskState{t: t, id: t.ID(), phase: "fetch"}
		r.tasks[ts.id] = ts
		r.taskOrder = append(r.taskOrder, ts.id)
	}
	if len(r.tasks) == 0 {
		r.barrier()
		return
	}
	r.scheduleTransfers()
}

func (r *Router) nodesSpec() []ring.Node {
	spec := make([]ring.Node, 0, len(r.members))
	for id, w := range r.members {
		spec = append(spec, ring.Node{ID: id, Weight: w})
	}
	sort.Slice(spec, func(i, j int) bool { return spec[i].ID < spec[j].ID })
	return spec
}

// scheduleTransfers activates pending ranges up to the configured
// concurrency. A task occupies exactly one slot for its whole lifetime,
// no matter how many pages it transfers.
func (r *Router) scheduleTransfers() {
	if !r.migrating || r.paused {
		return
	}
	for _, id := range r.taskOrder {
		ts := r.tasks[id]
		if ts.done || ts.active {
			continue
		}
		if r.inflight >= r.s.cfg.Transfer.Concurrency {
			return
		}
		ts.active = true
		r.inflight++
		r.beginFetch(ts)
	}
}

// beginFetch sends one fetch round for the task's current cursor. It must not
// touch the inflight count: the slot was taken when the task activated and is
// returned only by finishTask, so paging through many batches cannot leak or
// steal concurrency slots.
func (r *Router) beginFetch(ts *taskState) {
	ts.phase = "fetch"
	ts.gen++
	ts.attempts++
	r.s.send(routerAddr, ts.t.Src, message{msgFetch{
		TaskID: ts.id, Gen: ts.gen, Cursor: ts.cursor, Limit: r.s.cfg.Transfer.BatchSize,
	}})
	r.armTransferTimeout(ts)
}

// armTransferTimeout retransmits the UNACKED message of the current phase
// without advancing cursors or bumping the generation. This is essential: a
// lost transferPut (or its ack) must not make the range pointer jump over
// records, and a retransmitted batch is harmless because stores apply
// PutIfNewer. Each arm supersedes the previous pending timer via timerSeq, so
// a timer carried over from the fetch phase can't fire a second retransmit
// chain during the put phase.
func (r *Router) armTransferTimeout(ts *taskState) {
	gen := ts.gen
	ts.timerSeq++
	timerSeq := ts.timerSeq
	r.s.after(r.s.cfg.Transfer.FetchTimeoutMs, func() {
		cur := r.tasks[ts.id]
		if cur == nil || cur.done || !cur.active || cur.gen != gen || cur.timerSeq != timerSeq {
			return
		}
		cur.attempts++
		r.s.result.Stats.Retries++
		switch cur.phase {
		case "put":
			r.s.send(routerAddr, cur.t.Dst, message{msgTransferPut{
				TaskID: cur.id, Gen: gen, Records: cur.batch,
			}})
		default: // fetch
			r.s.send(routerAddr, cur.t.Src, message{msgFetch{
				TaskID: cur.id, Gen: gen, Cursor: cur.cursor, Limit: r.s.cfg.Transfer.BatchSize,
			}})
		}
		if cur.attempts > 200 {
			r.s.addError(fmt.Sprintf("transfer task %s exceeded retransmit budget", cur.id))
			return
		}
		r.armTransferTimeout(cur)
	})
}

func (r *Router) finishTask(ts *taskState) {
	ts.done = true
	ts.active = false
	r.inflight--
	for _, id := range r.taskOrder {
		if !r.tasks[id].done {
			r.scheduleTransfers()
			return
		}
	}
	r.barrier()
}

// barrier is the single cut-over point: once every range transfer has been
// acknowledged, the old ring is discarded and the epoch advances. Requests
// in flight before the barrier continue to drain; only after they finish may
// leaving nodes be decommissioned.
func (r *Router) barrier() {
	r.migrating = false
	r.epoch++
	r.oldRing = nil
	r.s.result.Stats.Barriers = append(r.s.result.Stats.Barriers, BarrierInfo{
		Epoch:         r.epoch,
		TimeMs:        r.s.now,
		Tasks:         len(r.taskOrder),
		KeysMigrated:  r.migKeys,
		BytesMigrated: r.migBytes,
	})
	r.s.result.Stats.KeysMigrated += r.migKeys
	r.s.result.Stats.BytesMigrated += r.migBytes
	r.tasks = make(map[string]*taskState)
	r.taskOrder = r.taskOrder[:0]
	r.inflight = 0

	r.maybeDecommission()

	if len(r.queued) > 0 {
		next := r.queued[0]
		r.queued = r.queued[1:]
		r.startChange(next)
	}
}

// maybeDecommission removes leaving nodes once no client request started
// before the latest barrier is still in flight. Before each removal it checks
// the safety invariant: no version present on the leaving node may be missing
// (or older) on its current-ring owner.
func (r *Router) maybeDecommission() {
	if len(r.leaving) == 0 || r.migrating {
		return
	}
	for e := uint64(1); e < r.epoch; e++ {
		if r.opsByEpoch[e] > 0 {
			return // pre-barrier traffic still draining
		}
	}
	ids := make([]string, 0, len(r.leaving))
	for id := range r.leaving {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		if r.decomm[id] {
			continue
		}
		n := r.s.nodes[id]
		if n != nil {
			keys := make([]string, 0, len(n.store))
			for k := range n.store {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			for _, k := range keys {
				rec := n.store[k]
				owner := r.ring.Owner(k)
				holder := r.s.nodes[owner]
				hv := uint64(0)
				if holder != nil {
					hv = holder.store[k].Version
				}
				if hv < rec.Version {
					r.s.removalIssues = append(r.s.removalIssues, RemovalIssue{
						Node: id, Key: k, Version: rec.Version, Holder: owner, HolderVer: hv,
					})
				}
			}
		}
		r.decomm[id] = true
		r.s.send(routerAddr, id, message{msgDecommission{}})
		r.armDecommissionTimeout(id)
	}
}

func (r *Router) armDecommissionTimeout(id string) {
	r.s.after(r.s.cfg.Transfer.FetchTimeoutMs, func() {
		if !r.decomm[id] {
			return
		}
		r.s.result.Stats.Retries++
		r.s.send(routerAddr, id, message{msgDecommission{}})
		r.armDecommissionTimeout(id)
	})
}

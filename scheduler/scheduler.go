package scheduler

import (
	"fmt"
	"sort"
	"strings"
)

// EventSink receives every event as it is emitted (in addition to the event
// slice returned in the Report), enabling streaming front-ends such as SSE.
type EventSink func(Event)

// Option customizes a Scheduler.
type Option func(*Scheduler)

// WithClock replaces the deterministic SimClock.
func WithClock(c Clock) Option { return func(s *Scheduler) { s.clock = c } }

// WithExecutor replaces the default program executor.
func WithExecutor(e Executor) Option { return func(s *Scheduler) { s.exec = e } }

// WithEventSink attaches a streaming sink.
func WithEventSink(sink EventSink) Option { return func(s *Scheduler) { s.sink = sink } }

type lockState struct {
	id     string
	holder *TaskRuntime
	waitQ  []*TaskRuntime // blocked waiters, enqueue order
}

// Scheduler is a single-processor preemptive scheduler with mutexes and
// priority inheritance. Construct with New, run with Run.
type Scheduler struct {
	cfg   Config
	mode  InheritMode
	qp    QueuePolicy
	clock Clock
	exec  Executor
	sink  EventSink

	tasks     map[string]*TaskRuntime
	order     []string
	locks     map[string]*lockState
	lockOrder []string

	running *TaskRuntime
	events  []Event
	seq     int

	// donorEdges[owner][donor] = lock id; tracked to emit join/leave deltas.
	donorEdges map[string]map[string]string
	deadSeen   map[string]bool
	maxTicks   int64

	deadlocked bool
	fatal      string
}

// New validates the configuration and constructs a Scheduler.
func New(cfg Config, opts ...Option) (*Scheduler, error) {
	if len(cfg.Tasks) == 0 {
		return nil, fmt.Errorf("config: at least one task required")
	}
	mode := cfg.Inheritance
	if mode == "" {
		mode = InheritancePIP
	}
	if mode != InheritancePIP && mode != InheritanceNone {
		return nil, fmt.Errorf("config: invalid inheritance mode %q", mode)
	}
	qp := cfg.QueuePolicy
	if qp == "" {
		qp = QueueFIFO
	}
	if qp != QueueFIFO && qp != QueuePriority {
		return nil, fmt.Errorf("config: invalid queue policy %q", qp)
	}

	lockSet := map[string]struct{}{}
	for _, l := range cfg.Locks {
		if l.ID == "" {
			return nil, fmt.Errorf("config: lock id must not be empty")
		}
		if _, dup := lockSet[l.ID]; dup {
			return nil, fmt.Errorf("config: duplicate lock %q", l.ID)
		}
		lockSet[l.ID] = struct{}{}
	}
	if len(lockSet) == 0 {
		return nil, fmt.Errorf("config: at least one lock required")
	}

	s := &Scheduler{
		cfg:        cfg,
		mode:       mode,
		qp:         qp,
		clock:      NewSimClock(),
		exec:       NewProgramExecutor(),
		tasks:      map[string]*TaskRuntime{},
		locks:      map[string]*lockState{},
		donorEdges: map[string]map[string]string{},
		deadSeen:   map[string]bool{},
		maxTicks:   cfg.MaxTicks,
	}
	for _, o := range opts {
		o(s)
	}
	if s.clock == nil {
		s.clock = NewSimClock()
	}
	if s.exec == nil {
		s.exec = NewProgramExecutor()
	}
	if s.maxTicks <= 0 {
		s.maxTicks = 10000
	}

	for _, l := range cfg.Locks {
		s.lockOrder = append(s.lockOrder, l.ID)
		s.locks[l.ID] = &lockState{id: l.ID}
	}
	seen := map[string]bool{}
	for _, ts := range cfg.Tasks {
		if ts.ID == "" {
			return nil, fmt.Errorf("config: task id must not be empty")
		}
		if seen[ts.ID] {
			return nil, fmt.Errorf("config: duplicate task %q", ts.ID)
		}
		seen[ts.ID] = true
		if ts.Arrival < 0 {
			return nil, fmt.Errorf("config: task %q arrival must be >= 0", ts.ID)
		}
		if err := Validate(ts.Program, lockSet); err != nil {
			return nil, fmt.Errorf("config: task %q: %v", ts.ID, err)
		}
		tr := &TaskRuntime{
			ID: ts.ID, Base: ts.Base, Effective: ts.Base,
			State: StatePending, Arrival: ts.Arrival,
		}
		s.exec.Init(tr, ts.Program)
		s.tasks[ts.ID] = tr
		s.order = append(s.order, ts.ID)
	}
	return s, nil
}

func (s *Scheduler) emit(t Event) {
	s.seq++
	t.Seq = s.seq
	if t.Tick == 0 {
		t.Tick = s.clock.Now()
	}
	s.events = append(s.events, t)
	if s.sink != nil {
		s.sink(t)
	}
}

func (s *Scheduler) waiterSnapshot(lk *lockState) []string {
	if len(lk.waitQ) == 0 {
		return nil
	}
	out := make([]string, len(lk.waitQ))
	for i, w := range lk.waitQ {
		out[i] = w.ID
	}
	return out
}

// pick chooses the highest effective-priority runnable task (ready or the
// currently running task); ties break on the lexicographically smaller id so
// the simulation is deterministic.
func (s *Scheduler) pick() *TaskRuntime {
	var best *TaskRuntime
	for _, id := range s.order {
		t := s.tasks[id]
		if t.State != StateReady && t.State != StateRunning {
			continue
		}
		if best == nil || t.Effective > best.Effective ||
			(t.Effective == best.Effective && t.ID < best.ID) {
			best = t
		}
	}
	return best
}

func (s *Scheduler) addArrivals(now int64) {
	var ids []string
	for _, id := range s.order {
		t := s.tasks[id]
		if t.State == StatePending && t.Arrival <= now {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids) // deterministic simultaneous arrivals
	for _, id := range ids {
		t := s.tasks[id]
		t.State = StateReady
		s.emit(Event{Type: EvArrive, Task: t.ID, Base: t.Base, Effective: t.Effective})
	}
}

func (s *Scheduler) nextArrival(after int64) (int64, bool) {
	var n int64
	found := false
	for _, id := range s.order {
		t := s.tasks[id]
		if t.State == StatePending && t.Arrival > after && (!found || t.Arrival < n) {
			n, found = t.Arrival, true
		}
	}
	return n, found
}

func (s *Scheduler) activeCount() int {
	n := 0
	for _, id := range s.order {
		if s.tasks[id].State != StateFinished {
			n++
		}
	}
	return n
}

// Run executes the simulation until all tasks finish, a deadlock is detected,
// maxTicks is exceeded, or an invalid program is encountered.
func (s *Scheduler) Run() *Report {
	for {
		now := s.clock.Now()
		if now > s.maxTicks {
			s.fatal = fmt.Sprintf("maxTicks %d exceeded (possible livelock)", s.maxTicks)
			s.emit(Event{Type: EvFatal, Reason: s.fatal})
			break
		}

		s.addArrivals(now)

		cand := s.pick()
		if cand != s.running {
			if s.running != nil && s.running.State == StateRunning {
				old := s.running
				old.State = StateReady
				by := ""
				if cand != nil {
					by = cand.ID
				}
				s.emit(Event{Type: EvPreempt, Task: old.ID, From: old.ID,
					By: by, Effective: old.Effective})
			}
			s.running = cand
			if cand != nil {
				cand.State = StateRunning
				s.emit(Event{Type: EvDispatch, Task: cand.ID, To: cand.ID,
					Base: cand.Base, Effective: cand.Effective})
			}
		}

		if s.running == nil {
			// Nothing runnable. A blocked cycle means deadlock; otherwise jump
			// idle time to the next arrival.
			if s.detectDeadlock() {
				s.deadlocked = true
				break
			}
			next, ok := s.nextArrival(now)
			if !ok {
				// No future work. Anything still active is stuck; report it as
				// a deadlock (cycle detection above normally catches this).
				if s.activeCount() > 0 {
					s.deadlocked = true
				}
				break
			}
			delta := next - now
			for _, id := range s.order {
				if t := s.tasks[id]; t.State == StateBlocked {
					t.blockedTicks += delta
				}
			}
			s.emit(Event{Type: EvIdle, Delta: delta})
			s.clock.Advance(delta)
			continue
		}

		resched := s.step(s.running)
		if s.fatal != "" {
			s.emit(Event{Type: EvFatal, Task: s.running.ID, Reason: s.fatal})
			break
		}
		if resched {
			// A zero-duration synchronization primitive (lock acquire/release)
			// is a scheduling point: re-evaluate who should run without moving
			// the clock. This lets an equal-priority task waiting its turn make
			// progress and acquire its lock (required for AB-BA to arise).
			continue
		}
	}
	return s.buildReport()
}

func (s *Scheduler) step(t *TaskRuntime) (resched bool) {
	in, ok := s.exec.Current(t)
	if !ok {
		s.finish(t)
		return true
	}
	switch in.Op {
	case OpLock:
		// doLock advances the executor exactly once when the lock is acquired
		// (immediately, or later via a release-time handoff); on contention the
		// task blocks and the same instruction is retried after wakeup, so no
		// advancement happens here.
		s.doLock(t, in.Lock)
		return true
	case OpUnlock:
		if !t.Holds(in.Lock) {
			s.fatal = fmt.Sprintf("task %s unlocks lock %q it does not hold", t.ID, in.Lock)
			return false
		}
		s.releaseLock(t, in.Lock)
		s.exec.Advance(t)
		return true
	case OpCPU:
		if t.Remaining == 0 {
			t.Remaining = in.Ticks
		}
		t.Remaining--
		t.cpuTicks++
		now := s.clock.Now()
		s.emit(Event{Type: EvCPUTick, Task: t.ID, Base: t.Base, Effective: t.Effective,
			Remaining: t.Remaining})
		for _, id := range s.order {
			o := s.tasks[id]
			if o == t || o.State == StatePending || o.State == StateFinished {
				continue
			}
			if o.State == StateBlocked {
				o.blockedTicks++
			} else {
				o.readyTicks++
			}
		}
		s.clock.Advance(1)
		s.addArrivals(now + 1)
		if t.Remaining == 0 {
			s.exec.Advance(t)
		}
		return false // time moved; the loop re-dispatches at the next tick
	}
	return false
}

func (s *Scheduler) doLock(t *TaskRuntime, id string) {
	if t.Holds(id) {
		// Non-recursive mutex: re-locking a held lock is a self-deadlock.
		s.fatal = fmt.Sprintf("task %s attempts to re-acquire lock %q it already holds (nested self-deadlock)", t.ID, id)
		return
	}
	lk := s.locks[id]
	if lk.holder == nil {
		lk.holder = t
		t.held = append(t.held, id)
		s.exec.Advance(t)
		s.emit(Event{Type: EvLockAcquired, Task: t.ID, Lock: id, Base: t.Base, Effective: t.Effective})
		return
	}
	// Contention: block in the lock queue and donate to the owner (via recompute).
	t.State = StateBlocked
	t.waitingOn = id
	lk.waitQ = append(lk.waitQ, t)
	s.emit(Event{Type: EvBlock, Task: t.ID, Lock: id, Owner: lk.holder.ID,
		Base: t.Base, Effective: t.Effective, Waiters: s.waiterSnapshot(lk)})
	s.running = nil
	s.recompute()
	s.detectDeadlock()
}

// popWaiter selects and removes the waiter to grant the lock to.
func (s *Scheduler) popWaiter(lk *lockState) *TaskRuntime {
	if len(lk.waitQ) == 0 {
		return nil
	}
	idx := 0
	if s.qp == QueuePriority {
		for i := 1; i < len(lk.waitQ); i++ {
			cur, best := lk.waitQ[i], lk.waitQ[idx]
			if cur.Effective > best.Effective ||
				(cur.Effective == best.Effective && cur.ID < best.ID) {
				idx = i
			}
		}
	}
	w := lk.waitQ[idx]
	lk.waitQ = append(lk.waitQ[:idx], lk.waitQ[idx+1:]...)
	return w
}

func (s *Scheduler) releaseLock(t *TaskRuntime, id string) {
	for i, h := range t.held {
		if h == id {
			t.held = append(t.held[:i], t.held[i+1:]...)
			break
		}
	}
	lk := s.locks[id]
	lk.holder = nil
	s.emit(Event{Type: EvLockReleased, Task: t.ID, Lock: id, Base: t.Base,
		Effective: t.Effective, Waiters: s.waiterSnapshot(lk)})

	if w := s.popWaiter(lk); w != nil {
		lk.holder = w
		w.waitingOn = ""
		w.held = append(w.held, id)
		w.State = StateReady
		// The waiter's blocking lock instruction now succeeds; consume it once.
		s.exec.Advance(w)
		s.emit(Event{Type: EvWakeup, Task: w.ID, Lock: id, Owner: t.ID,
			Base: w.Base, Effective: w.Effective, Waiters: s.waiterSnapshot(lk)})
	}
	s.recompute()
}

func (s *Scheduler) finish(t *TaskRuntime) {
	t.State = StateFinished
	t.finishTick = s.clock.Now()
	s.emit(Event{Type: EvFinish, Task: t.ID, Base: t.Base, Effective: t.Effective})
	s.running = nil

	// Exit releases every still-held lock in LIFO (nested) order.
	for len(t.held) > 0 {
		id := t.held[len(t.held)-1]
		lk := s.locks[id]
		t.held = t.held[:len(t.held)-1]
		lk.holder = nil
		s.emit(Event{Type: EvLockReleased, Task: t.ID, Lock: id, Base: t.Base,
			Effective: t.Effective, Waiters: s.waiterSnapshot(lk),
			Reason: "auto-release on exit"})
		if w := s.popWaiter(lk); w != nil {
			lk.holder = w
			w.waitingOn = ""
			w.held = append(w.held, id)
			w.State = StateReady
			s.exec.Advance(w)
			s.emit(Event{Type: EvWakeup, Task: w.ID, Lock: id, Owner: t.ID,
				Base: w.Base, Effective: w.Effective, Waiters: s.waiterSnapshot(lk)})
		}
	}
	s.recompute()
}

// recompute rebuilds donation edges from the current wait/ownership graph,
// emits donor join/leave deltas, and solves effective priorities as a
// fixpoint: eff(owner) >= eff(each donor). This yields transitive (chained)
// inheritance through nested locks. With mode "none", eff == base always.
func (s *Scheduler) recompute() {
	// 1. Build edges donor -> owner (annotated with the lock).
	edges := map[string]map[string]string{}
	add := func(donor, owner, lock string) {
		if edges[owner] == nil {
			edges[owner] = map[string]string{}
		}
		edges[owner][donor] = lock
	}
	for _, lid := range s.lockOrder {
		lk := s.locks[lid]
		if lk.holder == nil {
			continue
		}
		for _, w := range lk.waitQ {
			add(w.ID, lk.holder.ID, lid)
		}
	}

	// 2. Diff against the previous edge set to emit donor events. In "none"
	// mode there is no inheritance relationship, so no donor events are emitted
	// (the graph is still rebuilt for internal consistency).
	if s.mode == InheritancePIP {
		for _, oid := range s.order {
			old := s.donorEdges[oid]
			new := edges[oid]
			for did, lock := range new {
				if old == nil || old[did] != lock {
					s.emit(Event{Type: EvDonorJoin, Task: oid, Donor: did, Owner: oid,
						Lock: lock, Effective: s.tasks[oid].Effective})
				}
			}
			if old != nil {
				for did, lock := range old {
					if new == nil || new[did] != lock {
						s.emit(Event{Type: EvDonorLeave, Task: oid, Donor: did, Owner: oid,
							Lock: lock, Effective: s.tasks[oid].Effective})
					}
				}
			}
		}
	}
	s.donorEdges = edges

	// 3. Solve effective priorities.
	eff := map[string]int{}
	for _, id := range s.order {
		eff[id] = s.tasks[id].Base
	}
	if s.mode == InheritancePIP {
		changed := true
		for changed {
			changed = false
			for _, oid := range s.order {
				for did := range edges[oid] {
					if eff[did] > eff[oid] {
						eff[oid] = eff[did]
						changed = true
					}
				}
			}
		}
	}

	// 4. Apply, emitting priority change events.
	for _, id := range s.order {
		t := s.tasks[id]
		old := t.Effective
		nv := eff[id]
		if nv == old {
			continue
		}
		t.Effective = nv
		if nv > old {
			t.boosts++
			s.emit(Event{Type: EvPriorityBoost, Task: t.ID, Old: old, New: nv,
				Base: t.Base, Effective: nv})
		} else {
			s.emit(Event{Type: EvPriorityReset, Task: t.ID, Old: old, New: nv,
				Base: t.Base, Effective: nv})
		}
	}
}

// detectDeadlock finds cycles in the wait-for graph (blocked task -> lock
// holder). Each distinct cycle is reported once.
func (s *Scheduler) detectDeadlock() bool {
	adj := map[string][]string{}
	for _, id := range s.order {
		t := s.tasks[id]
		if t.State != StateBlocked || t.waitingOn == "" {
			continue
		}
		if h := s.locks[t.waitingOn].holder; h != nil {
			adj[t.ID] = append(adj[t.ID], h.ID)
		}
	}
	for k := range adj {
		sort.Strings(adj[k])
	}

	found := false
	for _, cyc := range simpleCycles(s.order, adj) {
		// canonical signature: rotate so the smallest id leads.
		start := 0
		for i := 1; i < len(cyc); i++ {
			if cyc[i] < cyc[start] {
				start = i
			}
		}
		rot := append(append([]string{}, cyc[start:]...), cyc[:start]...)
		sig := strings.Join(rot, "->")
		if s.deadSeen[sig] {
			continue
		}
		s.deadSeen[sig] = true
		found = true
		s.emit(Event{Type: EvDeadlock, Cycle: append(rot, rot[0]),
			Reason: "wait-for cycle: each task blocks on a lock held by the next"})
	}
	return found
}

// simpleCycles returns elementary directed cycles using Tarjan SCC followed by
// a within-component path search.
func simpleCycles(nodes []string, adj map[string][]string) [][]string {
	index := map[string]int{}
	low := map[string]int{}
	onStack := map[string]bool{}
	var stack []string
	idx := 0
	var sccs [][]string

	// deterministic DFS order
	var strong func(v string)
	strong = func(v string) {
		index[v] = idx
		low[v] = idx
		idx++
		stack = append(stack, v)
		onStack[v] = true
		for _, w := range adj[v] {
			if _, ok := index[w]; !ok {
				strong(w)
				if low[w] < low[v] {
					low[v] = low[w]
				}
			} else if onStack[w] && index[w] < low[v] {
				low[v] = index[w]
			}
		}
		if low[v] == index[v] {
			var comp []string
			for {
				w := stack[len(stack)-1]
				stack = stack[:len(stack)-1]
				onStack[w] = false
				comp = append(comp, w)
				if w == v {
					break
				}
			}
			sccs = append(sccs, comp)
		}
	}
	for _, v := range nodes {
		if _, ok := index[v]; !ok {
			if _, has := adj[v]; has || hasIncoming(v, adj) {
				strong(v)
			}
		}
	}

	var out [][]string
	for _, comp := range sccs {
		set := map[string]bool{}
		for _, v := range comp {
			set[v] = true
		}
		if len(comp) == 1 {
			v := comp[0]
			for _, w := range adj[v] { // self edge
				if w == v {
					out = append(out, []string{v})
				}
			}
			continue
		}
		sort.Strings(comp)
		start := comp[0]
		member := map[string]bool{}
		for _, v := range comp {
			member[v] = true
		}
		path := cyclePath(start, start, map[string]bool{start: true}, adj, member)
		if path != nil {
			out = append(out, path)
		}
	}
	return out
}

func hasIncoming(v string, adj map[string][]string) bool {
	for _, ws := range adj {
		for _, w := range ws {
			if w == v {
				return true
			}
		}
	}
	return false
}

// cyclePath finds a path from cur back to start using only member nodes.
func cyclePath(start, cur string, visited map[string]bool, adj map[string][]string, member map[string]bool) []string {
	for _, nx := range adj[cur] {
		if !member[nx] {
			continue
		}
		if nx == start {
			return []string{cur}
		}
		if visited[nx] {
			continue
		}
		visited[nx] = true
		if p := cyclePath(start, nx, visited, adj, member); p != nil {
			return append([]string{cur}, p...)
		}
		visited[nx] = false
	}
	return nil
}

func (s *Scheduler) buildReport() *Report {
	r := &Report{
		Inheritance: s.mode,
		QueuePolicy: s.qp,
		EndTick:     s.clock.Now(),
		Completed:   s.fatal == "" && !s.deadlocked && s.activeCount() == 0,
		Deadlocked:  s.deadlocked,
		Fatal:       s.fatal,
		Events:      s.events,
	}
	for _, id := range s.order {
		t := s.tasks[id]
		r.Tasks = append(r.Tasks, TaskReport{
			ID:             t.ID,
			BasePriority:   t.Base,
			Effective:      t.Effective,
			State:          t.State,
			Arrival:        t.Arrival,
			FinishTick:     t.finishTick,
			CPUTicks:       t.cpuTicks,
			BlockedTicks:   t.blockedTicks,
			ReadyTicks:     t.readyTicks,
			HeldLocks:      t.HeldLocks(),
			WaitingOn:      t.waitingOn,
			PriorityBoosts: t.boosts,
		})
	}
	for _, lid := range s.lockOrder {
		lk := s.locks[lid]
		lr := LockRuntime{ID: lid, Waiters: s.waiterSnapshot(lk)}
		if lk.holder != nil {
			lr.Holder = lk.holder.ID
		}
		r.Locks = append(r.Locks, lr)
	}
	return r
}

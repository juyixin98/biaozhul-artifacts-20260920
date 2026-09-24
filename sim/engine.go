package sim

import (
	"fmt"
	"sort"
	"strings"
)

// Task runtime statuses.
const (
	statusUnreleased = "unreleased"
	statusReady      = "ready"
	statusBlocked    = "blocked"
	statusDone       = "done"
)

type taskRT struct {
	spec       TaskSpec
	pc         int // index of the next op to start
	remaining  int // ticks left on the in-progress compute op
	status     string
	waitsOn    string
	effPrio    int
	startedAt  int
	finishedAt int
	executed   int
	blocked    int
	blockCnt   int
}

type resourceRT struct {
	owner   string
	waiters []string // task ids, in request (FIFO) order
}

type engine struct {
	w           Workload
	inheritance bool
	tasks       map[string]*taskRT
	order       []string
	res         map[string]*resourceRT
	resOrder    []string
	events      []Event
	completions []string
	lastRunner  string
	t           int
	deadlocked  bool
	cycle       []string
}

// Execute runs one deterministic simulation of the workload.
// With inheritance=true the scheduler applies the priority inheritance
// protocol; with false it uses plain fixed priorities.
func Execute(w Workload, inheritance bool) (*Result, error) {
	if err := Validate(w); err != nil {
		return nil, err
	}
	if w.MaxTicks <= 0 {
		w.MaxTicks = DefaultMaxTicks
	}
	e := &engine{
		w:           w,
		inheritance: inheritance,
		tasks:       map[string]*taskRT{},
		res:         map[string]*resourceRT{},
	}
	for _, t := range w.Tasks {
		e.order = append(e.order, t.ID)
		e.tasks[t.ID] = &taskRT{
			spec:       t,
			status:     statusUnreleased,
			effPrio:    t.BasePriority,
			startedAt:  -1,
			finishedAt: -1,
		}
	}
	for _, r := range w.Resources {
		e.resOrder = append(e.resOrder, r)
		e.res[r] = &resourceRT{}
	}
	if err := e.run(); err != nil {
		return nil, err
	}
	return e.result(), nil
}

func ip(v int) *int { return &v }

func (e *engine) emit(ev Event) {
	e.events = append(e.events, ev)
}

// readyIDs returns ready task ids sorted by effective priority desc, id asc.
func (e *engine) readyIDs() []string {
	var ids []string
	for _, id := range e.order {
		if e.tasks[id].status == statusReady {
			ids = append(ids, id)
		}
	}
	sort.SliceStable(ids, func(i, j int) bool {
		a, b := e.tasks[ids[i]], e.tasks[ids[j]]
		if a.effPrio != b.effPrio {
			return a.effPrio > b.effPrio
		}
		return ids[i] < ids[j]
	})
	return ids
}

func (e *engine) blockedSnapshot() map[string]string {
	m := map[string]string{}
	for _, id := range e.order {
		if ts := e.tasks[id]; ts.status == statusBlocked {
			m[id] = ts.waitsOn
		}
	}
	if len(m) == 0 {
		return nil
	}
	return m
}

// nextEff priorities: base priority, plus propagated inheritance along
// blocked-task -> resource-owner edges. Fixed-point iteration handles
// arbitrarily long (or cyclic, in a deadlock) wait-for chains.
func (e *engine) nextEff() map[string]int {
	m := map[string]int{}
	for _, id := range e.order {
		m[id] = e.tasks[id].spec.BasePriority
	}
	if !e.inheritance {
		return m
	}
	for iter := 0; iter < len(e.tasks); iter++ {
		changed := false
		for _, id := range e.order {
			ts := e.tasks[id]
			if ts.status != statusBlocked || ts.waitsOn == "" {
				continue
			}
			if owner := e.res[ts.waitsOn].owner; owner != "" && m[id] > m[owner] {
				m[owner] = m[id]
				changed = true
			}
		}
		if !changed {
			break
		}
	}
	return m
}

// inheritanceChain finds a blocked waiter whose wait-for chain ends at target
// and renders it, e.g. H --waits R2--> M --waits R1--> L.
func (e *engine) inheritanceChain(target string, prio int) string {
	var best []string
	bestPrio := e.tasks[target].spec.BasePriority
	for _, start := range e.order {
		if e.tasks[start].status != statusBlocked {
			continue
		}
		nodes := []string{start}
		edges := []string{}
		cur := start
		ok := false
		seen := map[string]bool{cur: true}
		for {
			ts := e.tasks[cur]
			if ts.status != statusBlocked || ts.waitsOn == "" {
				break
			}
			r := e.res[ts.waitsOn]
			edges = append(edges, ts.waitsOn)
			if r.owner == "" || seen[r.owner] {
				break
			}
			cur = r.owner
			nodes = append(nodes, cur)
			seen[cur] = true
			if cur == target {
				ok = true
				break
			}
		}
		if !ok {
			continue
		}
		if p := e.tasks[start].spec.BasePriority; p > bestPrio {
			bestPrio = p
			best = nil
			for i, n := range nodes {
				best = append(best, n)
				if i < len(edges) {
					best = append(best, "--waits "+edges[i]+"-->")
				}
			}
		}
	}
	if best == nil {
		return fmt.Sprintf("inherited effective priority %d", prio)
	}
	return fmt.Sprintf("inherited priority %d via: %s", prio, strings.Join(best, " "))
}

// applyPriorities recomputes effective priorities and emits "priority" events
// for every change (boost or restore).
func (e *engine) applyPriorities() {
	next := e.nextEff()
	for _, id := range e.order {
		ts := e.tasks[id]
		if next[id] == ts.effPrio {
			continue
		}
		old := ts.effPrio
		ts.effPrio = next[id]
		ev := Event{
			Tick:     e.t,
			Type:     "priority",
			Task:     id,
			FromPrio: ip(old),
			ToPrio:   ip(ts.effPrio),
			BasePrio: ip(ts.spec.BasePriority),
		}
		if ts.effPrio > old {
			ev.Reason = e.inheritanceChain(id, ts.effPrio)
		} else {
			ev.Reason = "priority restored to base after inherited lock was released"
		}
		e.emit(ev)
	}
}

// settle runs all currently possible zero-duration operations (lock/unlock,
// grants) at time e.t and then recomputes effective priorities once.
//
// Batching is essential for transitive inheritance: when several tasks lock
// resources at the same instant, the wait-for graph must be fully formed
// before priorities propagate (High -> Medium -> Low). Updating priorities
// between individual lock attempts would schedule from an unfinished chain.
func (e *engine) settle() {
	for {
		id := e.pickZeroTask()
		if id == "" {
			break
		}
		ts := e.tasks[id]
		op := ts.spec.Ops[ts.pc]
		switch op.Kind {
		case OpLock:
			e.doLock(ts, op)
		case OpUnlock:
			e.doUnlock(ts, op)
		default:
			break
		}
	}
	e.applyPriorities()
}

// pickZeroTask returns the highest-priority ready task parked on a zero-duration
// op, or "".
func (e *engine) pickZeroTask() string {
	var ids []string
	for _, id := range e.order {
		ts := e.tasks[id]
		if ts.status != statusReady || ts.pc >= len(ts.spec.Ops) {
			continue
		}
		if k := ts.spec.Ops[ts.pc].Kind; k == OpLock || k == OpUnlock {
			ids = append(ids, id)
		}
	}
	sort.SliceStable(ids, func(i, j int) bool {
		a, b := e.tasks[ids[i]], e.tasks[ids[j]]
		if a.effPrio != b.effPrio {
			return a.effPrio > b.effPrio
		}
		return ids[i] < ids[j]
	})
	if len(ids) == 0 {
		return ""
	}
	return ids[0]
}

func (e *engine) doLock(ts *taskRT, op Op) {
	idx := ts.pc
	r := e.res[op.Resource]
	if r.owner == "" {
		r.owner = ts.spec.ID
		e.emit(Event{
			Tick:     e.t,
			Type:     "lock-acquired",
			Task:     ts.spec.ID,
			Resource: op.Resource,
			OpIndex:  ip(idx),
			EffPrio:  ip(ts.effPrio),
			Reason:   "resource was free",
		})
		ts.pc++
		e.finishIfDone(ts)
		return
	}
	ts.status = statusBlocked
	ts.waitsOn = op.Resource
	ts.blockCnt++
	r.waiters = append(r.waiters, ts.spec.ID)
	owner := e.tasks[r.owner]
	e.emit(Event{
		Tick:     e.t,
		Type:     "lock-blocked",
		Task:     ts.spec.ID,
		Resource: op.Resource,
		OpIndex:  ip(idx),
		EffPrio:  ip(ts.effPrio),
		Reason: fmt.Sprintf("resource held by %s (base %d, effective %d)",
			owner.name(), owner.spec.BasePriority, owner.effPrio),
	})
	if e.lastRunner == ts.spec.ID {
		e.lastRunner = ""
	}
}

func (t *taskRT) name() string { return t.spec.ID }

// finishIfDone completes a task whose program ends on a zero-duration op
// (e.g. an unlock). Compute-ending tasks are completed in run() at a tick edge.
func (e *engine) finishIfDone(ts *taskRT) {
	if ts.status != statusReady || ts.pc < len(ts.spec.Ops) {
		return
	}
	ts.status = statusDone
	ts.finishedAt = e.t
	if e.lastRunner == ts.name() {
		e.lastRunner = ""
	}
	e.completions = append(e.completions, ts.name())
	e.emit(Event{
		Tick:    e.t,
		Type:    "complete",
		Task:    ts.name(),
		EffPrio: ip(ts.effPrio),
		Ready:   e.readyIDs(),
		Blocked: e.blockedSnapshot(),
		Reason:  "task program finished after zero-duration operation",
	})
}

func (e *engine) doUnlock(ts *taskRT, op Op) {
	idx := ts.pc
	r := e.res[op.Resource]
	r.owner = ""
	e.emit(Event{
		Tick:     e.t,
		Type:     "unlock",
		Task:     ts.spec.ID,
		Resource: op.Resource,
		OpIndex:  ip(idx),
		EffPrio:  ip(ts.effPrio),
	})
	ts.pc++
	if len(r.waiters) > 0 {
		// Grant to the highest *effective* priority waiter (PIP can boost a
		// waiter that itself holds another lock); ties go to the earliest
		// FIFO request.
		chosen := r.waiters[0]
		chosenAt := 0
		for i := 1; i < len(r.waiters); i++ {
			w := e.tasks[r.waiters[i]]
			c := e.tasks[chosen]
			if w.effPrio > c.effPrio || (w.effPrio == c.effPrio && r.waiters[i] < chosen) {
				chosen = r.waiters[i]
				chosenAt = i
			}
		}
		r.waiters = append(r.waiters[:chosenAt], r.waiters[chosenAt+1:]...)
		wt := e.tasks[chosen]
		wt.status = statusReady
		wt.waitsOn = ""
		r.owner = chosen
		lockIdx := wt.pc
		wt.pc++ // lock attempt now succeeds
		e.emit(Event{
			Tick:      e.t,
			Type:      "lock-granted",
			Task:      chosen,
			GrantedTo: chosen,
			Resource:  op.Resource,
			OpIndex:   ip(lockIdx),
			EffPrio:   ip(wt.effPrio),
			Reason:    "granted to highest-priority waiter after release",
		})
		e.finishIfDone(wt)
	}
	// Effective priorities are recomputed once the whole time-t batch of
	// zero-duration operations has settled (see settle).
	e.finishIfDone(ts)
}

func (e *engine) run() error {
	for e.t < e.w.MaxTicks {
		// (1) Releases at time t.
		for _, id := range e.order {
			ts := e.tasks[id]
			if ts.status == statusUnreleased && ts.spec.Release == e.t {
				ts.status = statusReady
				e.emit(Event{
					Tick:     e.t,
					Type:     "release",
					Task:     id,
					BasePrio: ip(ts.spec.BasePriority),
					EffPrio:  ip(ts.effPrio),
					Reason:   "task released",
				})
			}
		}

		// (2) Drain zero-duration operations and inheritance updates.
		e.settle()

		// (3) Classify remaining tasks.
		var ready, blocked, unreleased []string
		for _, id := range e.order {
			switch e.tasks[id].status {
			case statusReady:
				ready = append(ready, id)
			case statusBlocked:
				blocked = append(blocked, id)
			case statusUnreleased:
				unreleased = append(unreleased, id)
			}
		}

		if len(ready) == 0 {
			if len(blocked) == 0 {
				if len(unreleased) == 0 {
					return nil // all done
				}
				next := e.tasks[unreleased[0]].spec.Release
				for _, id := range unreleased[1:] {
					if r := e.tasks[id].spec.Release; r < next {
						next = r
					}
				}
				e.emit(Event{
					Tick:     e.t,
					Type:     "idle",
					FromTick: ip(e.t),
					NextTick: ip(next),
					Reason:   "no ready or blocked tasks; advance time to next release",
				})
				e.t = next
				continue
			}
			// Ready set empty while tasks are blocked: a wait-for cycle exists.
			e.cycle = e.findCycle(blocked)
			e.deadlocked = true
			e.emit(Event{
				Tick:   e.t,
				Type:   "deadlock",
				Cycle:  e.cycle,
				Reason: "every remaining task is blocked; wait-for cycle detected",
			})
			return nil
		}

		// (4) Select the highest-priority ready task. Equal priorities favor
		// the currently running task (no unnecessary context switch), then the
		// earlier release time, then the lexicographically smaller id.
		sort.SliceStable(ready, func(i, j int) bool {
			a, b := e.tasks[ready[i]], e.tasks[ready[j]]
			if a.effPrio != b.effPrio {
				return a.effPrio > b.effPrio
			}
			if (a.name() == e.lastRunner) != (b.name() == e.lastRunner) {
				return a.name() == e.lastRunner
			}
			if a.spec.Release != b.spec.Release {
				return a.spec.Release < b.spec.Release
			}
			return ready[i] < ready[j]
		})
		sel := ready[0]
		ts := e.tasks[sel]

		reason := e.selectReason(sel)
		if ts.startedAt < 0 {
			ts.startedAt = e.t
		}

		// (5) Every blocked task waits through the whole [t, t+1) interval.
		for _, id := range blocked {
			e.tasks[id].blocked++
		}

		// (6) Execute one tick of the current compute segment. Zero-duration
		// ops were all drained by settle(), so pc points at a compute op.
		op := ts.spec.Ops[ts.pc]
		if ts.remaining == 0 {
			ts.remaining = op.Duration
		}
		e.emit(Event{
			Tick:      e.t,
			Type:      "tick",
			Task:      sel,
			OpIndex:   ip(ts.pc),
			Remaining: ip(ts.remaining - 1),
			EffPrio:   ip(ts.effPrio),
			Selected:  sel,
			Ready:     e.readyIDs(),
			Blocked:   e.blockedSnapshot(),
			Reason:    reason,
		})
		ts.remaining--
		ts.executed++
		e.lastRunner = sel
		e.t++

		if ts.remaining == 0 {
			ts.pc++
			if ts.pc >= len(ts.spec.Ops) {
				ts.status = statusDone
				ts.finishedAt = e.t
				e.lastRunner = ""
				e.completions = append(e.completions, sel)
				e.emit(Event{
					Tick:    e.t,
					Type:    "complete",
					Task:    sel,
					EffPrio: ip(ts.effPrio),
					Ready:   e.readyIDs(),
					Blocked: e.blockedSnapshot(),
					Reason:  "last compute segment finished",
				})
			}
		}
	}
	return fmt.Errorf("simulation aborted after %d ticks (max_ticks); possible livelock or unfinished workload", e.w.MaxTicks)
}

func (e *engine) selectReason(sel string) string {
	ts := e.tasks[sel]
	prev := e.lastRunner
	switch {
	case prev == sel:
		return fmt.Sprintf("%s continues running (holds highest effective priority %d)", sel, ts.effPrio)
	case prev == "":
		return fmt.Sprintf("%s selected (highest effective priority %d among ready tasks)", sel, ts.effPrio)
	}
	p := e.tasks[prev]
	switch p.status {
	case statusBlocked:
		return fmt.Sprintf("%s blocked on %s; %s selected (highest effective priority %d)",
			prev, p.waitsOn, sel, ts.effPrio)
	case statusDone:
		return fmt.Sprintf("%s completed; %s selected next (highest effective priority %d)",
			prev, sel, ts.effPrio)
	default:
		return fmt.Sprintf("%s preempts %s (effective priority %d > %d)",
			sel, prev, ts.effPrio, p.effPrio)
	}
}

// findCycle returns a closed wait-for cycle, e.g. [A, B, A].
func (e *engine) findCycle(blocked []string) []string {
	const (
		white = 0
		gray  = 1
		black = 2
	)
	color := map[string]int{}
	stack := []string{}
	pos := map[string]int{}

	var dfs func(u string) []string
	dfs = func(u string) []string {
		color[u] = gray
		pos[u] = len(stack)
		stack = append(stack, u)
		ts := e.tasks[u]
		if ts.status == statusBlocked && ts.waitsOn != "" {
			v := e.res[ts.waitsOn].owner
			if v != "" {
				switch color[v] {
				case white:
					if cyc := dfs(v); cyc != nil {
						return cyc
					}
				case gray:
					cyc := append([]string{}, stack[pos[v]:]...)
					return append(cyc, v)
				}
			}
		}
		stack = stack[:len(stack)-1]
		delete(pos, u)
		color[u] = black
		return nil
	}

	for _, id := range blocked {
		if color[id] == white {
			if cyc := dfs(id); cyc != nil {
				return cyc
			}
		}
	}
	return nil
}

func (e *engine) result() *Result {
	metrics := map[string]TaskMetrics{}
	for _, id := range e.order {
		ts := e.tasks[id]
		m := TaskMetrics{
			ID:            id,
			Release:       ts.spec.Release,
			StartedAt:     ts.startedAt,
			FinishedAt:    ts.finishedAt,
			ExecutedTicks: ts.executed,
			BlockedTicks:  ts.blocked,
			BlockedCount:  ts.blockCnt,
		}
		if ts.finishedAt >= 0 {
			m.ResponseTime = ts.finishedAt - ts.spec.Release
		} else {
			m.ResponseTime = -1
		}
		metrics[id] = m
	}
	return &Result{
		Inheritance:     e.inheritance,
		Ticks:           e.t,
		CompletionOrder: append([]string{}, e.completions...),
		Metrics:         metrics,
		Deadlocked:      e.deadlocked,
		DeadlockCycle:   e.cycle,
		Events:          e.events,
	}
}

// Compare runs the workload twice (PIP off / on) and summarizes the difference.
func Compare(w Workload) (*ComparisonReport, error) {
	off, err := Execute(w, false)
	if err != nil {
		return nil, fmt.Errorf("run without inheritance: %w", err)
	}
	on, err := Execute(w, true)
	if err != nil {
		return nil, fmt.Errorf("run with inheritance: %w", err)
	}
	rep := &ComparisonReport{
		Workload:           w,
		WithoutInheritance: off,
		WithInheritance:    on,
	}
	for _, t := range w.Tasks {
		a, b := off.Metrics[t.ID], on.Metrics[t.ID]
		rep.Tasks = append(rep.Tasks, TaskComparison{
			ID:                 t.ID,
			BlockedWithoutPIP:  a.BlockedTicks,
			BlockedWithPIP:     b.BlockedTicks,
			BlockedDelta:       a.BlockedTicks - b.BlockedTicks,
			StartedWithoutPIP:  a.StartedAt,
			StartedWithPIP:     b.StartedAt,
			FinishedWithoutPIP: a.FinishedAt,
			FinishedWithPIP:    b.FinishedAt,
		})
		rep.TotalBlockedWithoutPIP += a.BlockedTicks
		rep.TotalBlockedWithPIP += b.BlockedTicks
	}
	rep.BlockedDelta = rep.TotalBlockedWithoutPIP - rep.TotalBlockedWithPIP
	return rep, nil
}

package scheduler

import (
	"fmt"
	"sort"
	"strings"

	"pim/internal/clock"
	"pim/internal/event"
)

// Run executes the simulation described by spec with the default virtual
// clock, an in-memory event sink and the no-op VirtualExecutor. It is a
// shorthand for RunWith(spec, Config{}).
//
// The simulation is deterministic: ordering is decided by strict effective
// priority with specification index as tie break.
func Run(spec Spec, clk clock.Clock, sink event.Sink) (*Result, error) {
	return RunWith(spec, Config{Clock: clk, Sink: sink})
}

// RunWith executes the simulation with injected collaborators. Any nil
// Config field gets a virtual default: a fresh VirtualClock at tick 0, a
// MemorySink (also always captured into Result.Events) and a
// VirtualExecutor. The injected sink, if any, additionally receives every
// event via a fan-out.
func RunWith(spec Spec, cfg Config) (*Result, error) {
	spec = spec.WithDefaults()
	if err := spec.validate(); err != nil {
		return nil, err
	}
	if cfg.Clock == nil {
		cfg.Clock = clock.NewVirtualClock(0)
	}
	mem := event.NewMemorySink()
	if cfg.Sink == nil {
		cfg.Sink = mem
	} else {
		// Always keep a local copy for Result.Events while also forwarding.
		cfg.Sink = event.MultiSink{cfg.Sink, mem}
	}
	if cfg.Executor == nil {
		cfg.Executor = VirtualExecutor{}
	}

	st := newState(spec, cfg)
	st.run()

	res := &Result{
		Name:       spec.Name,
		Options:    spec.Options,
		FinishTime: cfg.Clock.Now(),
		Deadlock:   st.deadlock,
		Error:      st.progErr,
		Events:     mem.Events(),
		Summary: Summary{
			MakespanTicks:  cfg.Clock.Now(),
			InversionTicks: st.inversions,
			BlockedTicks:   st.blockedTicks,
			CompletedAt:    st.completedAt,
		},
		Tasks: st.taskSnapshots(),
		Locks: st.lockSnapshots(),
	}
	return res, nil
}

func (st *state) run() {
	st.recomputePriories("init")

	for {
		// 1. Admit tasks whose arrival time has been reached.
		st.admit()

		// 2. Termination: deadlock / program error / everything finished.
		if st.deadlock != nil || st.progErr != "" {
			return
		}
		if st.running == nil && !st.anyReady() {
			if !st.anyAlive() {
				return
			}
			st.idleJump()
			st.admit()
			if st.deadlock != nil || st.progErr != "" {
				return
			}
		}

		// 3. Preemptive dispatch at every scheduling boundary.
		st.reschedule()
		if st.running == nil {
			// Should be unreachable: idleJump/admit guarantee progress,
			// but guard against a scheduler logic error.
			st.progErr = "scheduler: no runnable task but work remains"
			return
		}

		// 4. Drain zero-time lock operations until the running task asks for
		//    CPU work, blocks, or exits. A task resuming the processor with
		//    BurstLeft > 0 (it was preempted mid-burst) continues that burst
		//    instead of reading the next program action.
		for st.running != nil && st.running.BurstLeft == 0 {
			t := st.running
			act, ok := t.Spec.Program.Next(t.PC)
			if !ok {
				st.complete(t)
				break
			}
			switch {
			case act.CPU > 0:
				t.BurstLeft = act.CPU
				t.PC++
			case act.Acquire != "":
				// The P() is considered complete once granted, so advance
				// the program counter before the (possibly blocking) call.
				t.PC++
				st.doAcquire(t, st.locks[act.Acquire])
				if st.deadlock != nil || st.progErr != "" {
					return
				}
				if st.running == nil {
					break // task blocked during acquire
				}
				continue
			case act.Release != "":
				if err := st.doRelease(t, st.locks[act.Release]); err != nil {
					st.progErr = err.Error()
					st.emit(event.ProgramError, t.ID(), act.Release,
						map[string]any{"message": err.Error()})
					return
				}
				t.PC++
				// The hand-off may have woken a strictly higher-priority
				// task; re-pick the running task before continuing.
				// If the releasing task has exhausted its script and holds
				// no more locks, it completes even if it loses the CPU here.
				if !st.taskHasWork(t) {
					st.complete(t)
					break
				}
				st.reschedule()
				if st.running == nil {
					break
				}
				continue
			}
			break
		}
		if st.running == nil || st.running.BurstLeft <= 0 {
			continue
		}

		// 5. Execute exactly one processor tick, then loop back so newly
		//    arriving tasks can preempt.
		st.stepOneTick()
	}
}

// admit promotes waiting tasks that have arrived to ready.
func (st *state) admit() {
	now := st.clk.Now()
	for _, t := range st.tasks {
		if t.State == StWaiting && t.Spec.Arrival <= now {
			t.State = StReady
			st.emit(event.TaskArrive, t.ID(), "", map[string]any{
				"arrival":  t.Spec.Arrival,
				"priority": t.Spec.Priority,
			})
		}
	}
}

func (st *state) anyReady() bool {
	for _, t := range st.tasks {
		if t.State == StReady {
			return true
		}
	}
	return false
}

func (st *state) anyAlive() bool {
	for _, t := range st.tasks {
		if t.State != StDone {
			return true
		}
	}
	return false
}

// idleJump advances time to the next future arrival (only called when the
// processor is idle and work remains).
func (st *state) idleJump() {
	var next int64 = -1
	for _, t := range st.tasks {
		if t.State == StWaiting && t.Spec.Arrival > st.clk.Now() {
			if next < 0 || t.Spec.Arrival < next {
				next = t.Spec.Arrival
			}
		}
	}
	if next < 0 {
		// Remaining tasks are blocked with no pending arrival. If they form
		// a wait-for cycle no progress is possible; terminate either with a
		// structured deadlock event or, when detection is disabled, with an
		// error rather than spinning forever.
		if cyc := st.cycle(); cyc != nil {
			if st.detect {
				st.reportDeadlock(cyc)
			} else {
				st.progErr = "scheduler: deadlocked wait-for cycle with deadlock detection disabled: " +
					strings.Join(cyc, " -> ")
			}
			return
		}
		st.progErr = "scheduler: idle processor with blocked tasks and no future arrivals"
		return
	}
	jump := next - st.clk.Now()
	st.emit(event.IdleJump, "", "", map[string]any{
		"from": st.clk.Now(),
		"to":   next,
	})
	st.clk.Advance(jump)
}

// reschedule picks the best ready task; if it differs from the running one,
// the running task is preempted (or the idle processor is filled).
func (st *state) reschedule() {
	best := st.pickReady()
	if best == st.running {
		return
	}
	if st.running != nil {
		old := st.running
		old.State = StReady
		st.running = nil
		st.emit(event.TaskPreempted, old.ID(), "", map[string]any{
			"by": func() string {
				if best != nil {
					return best.ID()
				}
				return ""
			}(),
			"effectivePriority": old.EffPri,
		})
	}
	st.running = best
	if best != nil {
		best.State = StRunning
		st.emit(event.TaskDispatch, best.ID(), "", map[string]any{
			"effectivePriority": best.EffPri,
			"basePriority":      best.Spec.Priority,
		})
	}
}

// pickReady returns the runnable task with the highest effective priority;
// ties go to the earlier specification index. A running task that has kept
// the processor through a rescheduling boundary is itself a candidate:
// preemption only happens when a strictly better candidate exists.
func (st *state) pickReady() *Task {
	var best *Task
	for _, t := range st.tasks {
		if t.State != StReady && t.State != StRunning {
			continue
		}
		if best == nil || t.EffPri > best.EffPri ||
			(t.EffPri == best.EffPri && t.Index < best.Index) {
			best = t
		}
	}
	return best
}

// doAcquire performs a blocking P() on lk.
func (st *state) doAcquire(t *Task, lk *Lock) {
	if lk.Owner == t {
		st.progErr = fmt.Sprintf("task %q attempted to re-acquire lock %q: locks are non-reentrant", t.ID(), lk.ID)
		st.emit(event.ProgramError, t.ID(), lk.ID, map[string]any{"message": st.progErr})
		return
	}
	if lk.Owner == nil {
		lk.Owner = t
		t.Held = append(t.Held, lk)
		st.emit(event.LockAcquire, t.ID(), lk.ID, nil)
		st.recomputePriories("acquire")
		return
	}
	// Block on the lock.
	t.State = StBlocked
	t.WaitingFor = lk
	lk.Waiters = append(lk.Waiters, t)
	if st.running == t {
		st.running = nil
	}
	st.emit(event.TaskBlock, t.ID(), lk.ID, map[string]any{
		"owner":             lk.Owner.ID(),
		"ownerEffectivePri": lk.Owner.EffPri,
	})
	st.recomputePriories("block")
	if cyc := st.cycle(); cyc != nil {
		if st.detect {
			st.reportDeadlock(cyc)
		} else {
			st.progErr = "scheduler: deadlocked wait-for cycle with deadlock detection disabled: " +
				strings.Join(cyc, " -> ")
		}
	}
}

// doRelease performs V() on lk, handing it directly to the highest-priority
// waiter when one exists.
func (st *state) doRelease(t *Task, lk *Lock) error {
	if lk.Owner != t {
		return fmt.Errorf("task %q released lock %q it does not hold", t.ID(), lk.ID)
	}
	// Remove from held list.
	for i, h := range t.Held {
		if h == lk {
			t.Held = append(t.Held[:i], t.Held[i+1:]...)
			break
		}
	}
	st.emit(event.LockRelease, t.ID(), lk.ID, nil)

	if len(lk.Waiters) == 0 {
		lk.Owner = nil
		st.recomputePriories("release")
		return nil
	}

	// Priority inheritance semantics: wake the waiter with the highest
	// effective priority; ties keep FIFO request order.
	w := lk.Waiters[0]
	for _, cand := range lk.Waiters[1:] {
		if cand.EffPri > w.EffPri {
			w = cand
		}
	}
	lk.Waiters = removeWaiter(lk.Waiters, w)
	lk.Owner = w
	w.State = StReady
	w.WaitingFor = nil
	w.Held = append(w.Held, lk)
	st.emit(event.LockGrant, w.ID(), lk.ID, map[string]any{
		"from":              t.ID(),
		"effectivePriority": w.EffPri,
		"waitersRemaining":  len(lk.Waiters),
	})
	st.emit(event.TaskWakeup, w.ID(), lk.ID, map[string]any{"grantedBy": t.ID()})
	st.recomputePriories("grant")
	return nil
}

func removeWaiter(xs []*Task, t *Task) []*Task {
	out := xs[:0]
	for _, x := range xs {
		if x != t {
			out = append(out, x)
		}
	}
	return out
}

// taskHasWork reports whether t still has program actions to execute.
// A burst in progress counts as work.
func (st *state) taskHasWork(t *Task) bool {
	return t.BurstLeft > 0 || t.PC < len(t.Spec.Program.Actions)
}

// complete marks a task done and releases all its locks (process-exit
// semantics; also keeps the simulation well-formed if a program ends without
// explicit releases).
func (st *state) complete(t *Task) {
	if st.running == t {
		st.running = nil
	}
	t.State = StDone
	if len(t.Held) > 0 {
		locks := append([]*Lock(nil), t.Held...)
		for _, lk := range locks {
			if err := st.doRelease(t, lk); err != nil {
				st.progErr = err.Error()
				return
			}
		}
	}
	st.completedAt[t.ID()] = st.clk.Now()
	st.emit(event.TaskExit, t.ID(), "", map[string]any{
		"completedAt": st.clk.Now(),
	})
}

// stepOneTick executes the running task for a single tick.
func (st *state) stepOneTick() {
	t := st.running
	if t == nil || t.BurstLeft <= 0 {
		return
	}
	t.BurstLeft--
	st.clk.Advance(1)
	for _, b := range st.tasks {
		if b.State == StBlocked {
			st.blockedTicks[b.ID()]++
		}
	}
	st.recordInversion(t)
	st.emit(event.Tick, t.ID(), "", map[string]any{
		"burstLeft":         t.BurstLeft,
		"effectivePriority": t.EffPri,
	})
	if st.executor != nil {
		st.executor.Tick(TaskExecution{
			TaskID:            t.ID(),
			Tick:              st.clk.Now(),
			EffectivePriority: t.EffPri,
			BurstLeft:         t.BurstLeft,
		})
	}
}

// recordInversion counts textbook unbounded priority inversion: a ready task
// with strictly higher base priority than the running task is blocked on a
// chain whose root owner is *not* the running task (the running task is an
// unrelated low-priority job delaying it).
func (st *state) recordInversion(run *Task) {
	for _, b := range st.tasks {
		if b.State != StBlocked || b.WaitingFor == nil {
			continue
		}
		if b.Spec.Priority <= run.Spec.Priority {
			continue
		}
		root := st.chainRoot(b)
		if root != nil && root != run && root.State == StReady {
			st.inversions++
			return // count the tick once even if several jobs are inverted
		}
	}
}

// chainRoot follows waiter->owner links until it reaches a task that is not
// blocked; nil if the chain ends on a free lock (or cycles).
func (st *state) chainRoot(start *Task) *Task {
	visited := map[string]bool{start.ID(): true}
	cur := start
	for cur.State == StBlocked && cur.WaitingFor != nil {
		owner := cur.WaitingFor.Owner
		if owner == nil || visited[owner.ID()] {
			return nil
		}
		visited[owner.ID()] = true
		cur = owner
	}
	if cur == start {
		return nil
	}
	return cur
}

// cycle detects a cycle in the wait-for graph and returns the cyclic task
// sequence (t0 waits on t1 ... waits on t0).
func (st *state) cycle() []string {
	// Color DFS: 0=white, 1=gray (on stack), 2=black.
	color := map[string]int{}
	stack := []*Task{}

	var dfs func(t *Task) []*Task
	dfs = func(t *Task) []*Task {
		color[t.ID()] = 1
		stack = append(stack, t)
		if t.State == StBlocked && t.WaitingFor != nil && t.WaitingFor.Owner != nil {
			n := t.WaitingFor.Owner
			switch color[n.ID()] {
			case 0:
				if cyc := dfs(n); cyc != nil {
					return cyc
				}
			case 1:
				for i, x := range stack {
					if x == n {
						return append(append([]*Task{}, stack[i:]...), n)
					}
				}
			}
		}
		stack = stack[:len(stack)-1]
		color[t.ID()] = 2
		return nil
	}

	for _, t := range st.tasks {
		if color[t.ID()] == 0 {
			if cyc := dfs(t); cyc != nil {
				ids := make([]string, len(cyc))
				for i, x := range cyc {
					ids[i] = x.ID()
				}
				return ids
			}
		}
	}
	return nil
}

func (st *state) reportDeadlock(cyc []string) {
	if cyc == nil {
		cyc = st.cycle()
	}
	if cyc == nil {
		return
	}
	st.deadlock = &DeadlockInfo{Cycle: cyc, At: st.clk.Now()}
	// Stable edge list for the structured record.
	edges := []map[string]string{}
	for i := 0; i < len(cyc)-1; i++ {
		id := cyc[i]
		if t := st.byID[id]; t.WaitingFor != nil {
			edges = append(edges, map[string]string{
				"waiter": id,
				"holds":  t.WaitingFor.ID,
				"owner":  cyc[i+1],
			})
		}
	}
	st.emit(event.Deadlock, "", "", map[string]any{
		"cycle": cyc,
		"edges": edges,
		"at":    st.clk.Now(),
	})
}

func (st *state) taskSnapshots() []TaskSnapshot {
	out := make([]TaskSnapshot, 0, len(st.tasks))
	for _, t := range st.tasks {
		sn := TaskSnapshot{
			ID:           t.ID(),
			State:        t.State,
			BasePriority: t.Spec.Priority,
			EffPriority:  t.EffPri,
			Donors:       append([]string{}, t.Donors...),
			Arrival:      t.Spec.Arrival,
			BlockedTicks: st.blockedTicks[t.ID()],
		}
		for _, lk := range t.Held {
			sn.Held = append(sn.Held, lk.ID)
		}
		sort.Strings(sn.Held)
		if t.WaitingFor != nil {
			sn.WaitingFor = t.WaitingFor.ID
		}
		if t.State == StDone {
			sn.CompletedAt = st.completedAt[t.ID()]
		}
		out = append(out, sn)
	}
	return out
}

func (st *state) lockSnapshots() []LockSnapshot {
	ids := make([]string, 0, len(st.locks))
	for id := range st.locks {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	out := make([]LockSnapshot, 0, len(ids))
	for _, id := range ids {
		lk := st.locks[id]
		sn := LockSnapshot{ID: id}
		if lk.Owner != nil {
			sn.Owner = lk.Owner.ID()
		}
		for _, w := range lk.Waiters {
			sn.Waiters = append(sn.Waiters, w.ID())
		}
		out = append(out, sn)
	}
	return out
}

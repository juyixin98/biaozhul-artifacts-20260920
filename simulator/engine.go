package simulator

import "fmt"

// Task runtime states.
const (
	statePending = "PENDING" // not yet released
	stateReady   = "READY"   // runnable but not selected
	stateRunning = "RUNNING" // owns the CPU for the current tick
	stateBlocked = "BLOCKED" // waiting on a held mutex
	stateDone    = "DONE"    // finished
)

// Event kinds emitted into the decision trace.
const (
	evRelease       = "RELEASE"          // task became runnable at its release time
	evSchedule      = "SCHEDULE"         // scheduler picked a task for a tick
	evIdle          = "IDLE"             // no runnable task; simulation time jumped
	evCompute       = "COMPUTE"          // task executed one tick of compute
	evLock          = "LOCK"             // mutex acquired (immediately available)
	evBlock         = "BLOCK"            // mutex busy; task starts waiting
	evWake          = "WAKE"             // mutex handed off to a waiter at unlock
	evUnlock        = "UNLOCK"           // mutex released
	evFinish        = "FINISH"           // task completed its last step
	evPriorityBoost = "PRIORITY_INHERIT" // holder effective priority raised
	evPriorityDrop  = "PRIORITY_RESTORE" // holder effective priority lowered
	evDeadlock      = "DEADLOCK"         // cyclic resource wait detected
	evTimeout       = "TIMEOUT"          // MaxTime reached before all tasks finished
)

// Event is one recorded scheduling decision / state transition.
type Event struct {
	Time           int            `json:"time"`
	Kind           string         `json:"kind"`
	Task           string         `json:"task,omitempty"`
	Resource       string         `json:"resource,omitempty"`
	Holder         string         `json:"holder,omitempty"`
	OldPriority    int            `json:"oldPriority,omitempty"`
	NewPriority    int            `json:"newPriority,omitempty"`
	BasePriority   int            `json:"basePriority,omitempty"`
	CausedBy       string         `json:"causedBy,omitempty"`
	Remaining      int            `json:"remaining,omitempty"`
	Candidates     []string       `json:"candidates,omitempty"`
	CandPriorities map[string]int `json:"candidatePriorities,omitempty"`
	Preempted      string         `json:"preempted,omitempty"`
	TickConsumed   bool           `json:"tickConsumed,omitempty"`
	JumpTo         int            `json:"jumpTo,omitempty"`
	Cycle          []string       `json:"cycle,omitempty"`
	Message        string         `json:"message,omitempty"`
}

// LockAttempt records one lock acquisition for the lock-wait statistics.
type LockAttempt struct {
	Resource    string `json:"resource"`
	RequestedAt int    `json:"requestedAt"`
	AcquiredAt  *int   `json:"acquiredAt,omitempty"`
	WaitedTicks int    `json:"waitedTicks"`
	Acquired    bool   `json:"acquired"`
}

// rtTask is the runtime state of a task.
type rtTask struct {
	spec         TaskSpec
	idx          int
	state        string
	pc           int  // index of next step in spec.Steps
	remaining    int  // remaining ticks of the current compute step
	computing    bool // pc/remaining describe an in-flight compute step
	effective    int
	prevEff      int // effective priority at the previous time boundary
	blockedOn    string
	waitSince    int
	blockedTicks int
	runningTicks int
	startTime    *int
	finishTime   *int
	holds        []string
	attempts     []LockAttempt
}

type engine struct {
	cfg         Config
	tasks       []*rtTask
	byName      map[string]*rtTask
	holders     map[string]*rtTask // resource -> current holder
	events      []Event
	time        int
	deadlocked  bool
	timedOut    bool
	pendingWake map[*rtTask]string // waiter handed a lock at an unlock boundary
}

// Run validates the configuration and executes one deterministic simulation.
func Run(cfg Config) (Result, error) {
	if err := Validate(cfg); err != nil {
		return Result{}, err
	}
	e := newEngine(cfg)
	e.run()
	return e.buildResult(), nil
}

func newEngine(cfg Config) *engine {
	e := &engine{
		cfg:         cfg,
		byName:      map[string]*rtTask{},
		holders:     map[string]*rtTask{},
		pendingWake: map[*rtTask]string{},
	}
	for i, ts := range cfg.Tasks {
		t := &rtTask{spec: ts, idx: i, state: statePending,
			effective: ts.Priority, prevEff: ts.Priority}
		e.tasks = append(e.tasks, t)
		e.byName[ts.Name] = t
	}
	return e
}

func (e *engine) run() {
	for {
		if e.time > 0 && e.allDone() {
			return
		}

		// 1. Time-triggered releases at this boundary.
		e.releaseDue()

		// 2. Recompute effective priorities across the wait-for graph
		//    (fixed point of transitive inheritance).
		e.recomputePriorities()

		// 3. Scheduling decision for this boundary.
		running := e.pickTask()

		if running == nil {
			// Nothing runnable: either a resource deadlock cycle or idle time.
			if cyc, ok := e.findDeadlockCycle(); ok {
				e.events = append(e.events, Event{
					Time: e.time, Kind: evDeadlock, Cycle: cyc,
					Message: "cyclic mutex wait detected; priority inheritance does not prevent deadlock",
				})
				e.deadlocked = true
				return
			}
			if !e.jumpToNextRelease() {
				if e.allDone() {
					return
				}
				e.events = append(e.events, Event{Time: e.time, Kind: evDeadlock,
					Message: "system stuck with blocked tasks and no future release"})
				e.deadlocked = true
				return
			}
			continue
		}

		// 4. Execute the selected task's next primitive.
		consumed := e.stepTask(running)
		if !consumed {
			continue // instantaneous lock/unlock boundary; decide again at once
		}

		// 5. A compute tick [t, t+1) was consumed: bill waiting time.
		for _, t := range e.tasks {
			if t.state == stateBlocked {
				t.blockedTicks++
			}
		}

		if e.time >= e.maxTimeValue() {
			e.events = append(e.events, Event{Time: e.time, Kind: evTimeout,
				Message: "maxTime reached before completion"})
			e.timedOut = true
			return
		}
		e.time++
	}
}

func (e *engine) releaseDue() {
	for _, t := range e.tasks {
		if t.state == statePending && t.spec.ReleaseTime <= e.time {
			t.state = stateReady
			st := e.time
			t.startTime = &st
			e.events = append(e.events, Event{Time: e.time, Kind: evRelease, Task: t.spec.Name})
		}
	}
}

// recomputePriorities resets every effective priority to its base priority
// and propagates inheritance to a fixed point. A blocked task waiting on a
// resource donates its effective priority to the holder; holders may
// themselves be blocked, so donations transitively cross nested lock chains.
// A PRIORITY_INHERIT / PRIORITY_RESTORE event is emitted whenever a task's
// effective priority changes between two time boundaries.
func (e *engine) recomputePriorities() {
	for _, t := range e.tasks {
		t.effective = t.spec.Priority
	}
	if e.cfg.EnableInheritance {
		changed := true
		for changed {
			changed = false
			for _, waiter := range e.tasks {
				if waiter.state != stateBlocked || waiter.blockedOn == "" {
					continue
				}
				holder := e.holders[waiter.blockedOn]
				if holder == nil || holder == waiter {
					continue
				}
				if waiter.effective > holder.effective {
					holder.effective = waiter.effective
					changed = true
				}
			}
		}
	}
	for _, t := range e.tasks {
		switch {
		case t.effective > t.prevEff:
			e.events = append(e.events, Event{
				Time: e.time, Kind: evPriorityBoost, Task: t.spec.Name,
				OldPriority: t.prevEff, BasePriority: t.spec.Priority,
				NewPriority: t.effective, CausedBy: e.topWaiterCause(t),
			})
		case t.effective < t.prevEff:
			ev := Event{
				Time: e.time, Kind: evPriorityDrop, Task: t.spec.Name,
				OldPriority: t.prevEff, BasePriority: t.spec.Priority,
				NewPriority: t.effective,
			}
			if t.effective > t.spec.Priority {
				ev.CausedBy = e.topWaiterCause(t)
			}
			e.events = append(e.events, ev)
		}
		t.prevEff = t.effective
	}
}

// topWaiterCause names the highest-priority task blocked on a resource held
// by holder — the direct cause of its current donation.
func (e *engine) topWaiterCause(holder *rtTask) string {
	best := -1
	cause := ""
	for res, h := range e.holders {
		if h != holder {
			continue
		}
		for _, w := range e.tasks {
			if w.state == stateBlocked && w.blockedOn == res && w.effective > best {
				best = w.effective
				cause = w.spec.Name
			}
		}
	}
	return cause
}

// pickTask demotes the current runner and chooses the runnable task with the
// highest effective priority (ties broken by task index for determinism).
// A SCHEDULE event records every decision.
func (e *engine) pickTask() *rtTask {
	var prev *rtTask
	for _, t := range e.tasks {
		if t.state == stateRunning {
			prev = t
			t.state = stateReady
		}
	}
	var best *rtTask
	for _, t := range e.tasks {
		if t.state != stateReady {
			continue
		}
		if best == nil || t.effective > best.effective {
			best = t
		}
	}
	candidates := make([]string, 0)
	prios := map[string]int{}
	for _, t := range e.tasks {
		if t.state == stateReady {
			candidates = append(candidates, t.spec.Name)
			prios[t.spec.Name] = t.effective
		}
	}
	ev := Event{Time: e.time, Kind: evSchedule, Candidates: candidates,
		CandPriorities: prios}
	if best != nil {
		ev.Task = best.spec.Name
		ev.NewPriority = best.effective
		ev.TickConsumed = e.willConsumeTick(best)
		best.state = stateRunning
	}
	if prev != nil && best != prev {
		ev.Preempted = prev.spec.Name
	}
	e.events = append(e.events, ev)
	return best
}

// willConsumeTick reports whether selecting t consumes a tick. Only compute
// executes in tick time; an in-flight lock handoff resumes into compute on
// the same tick. Plain lock/unlock boundaries are instantaneous.
func (e *engine) willConsumeTick(t *rtTask) bool {
	if _, ok := e.pendingWake[t]; ok {
		return true
	}
	if t.pc >= len(t.spec.Steps) {
		return false
	}
	return t.spec.Steps[t.pc].Op == OpCompute
}

// stepTask executes one primitive for the running task and reports whether
// simulation time advanced by one tick.
func (e *engine) stepTask(t *rtTask) bool {
	// Complete a handoff from a previous unlock, then continue into the
	// task's following step on this same tick.
	if res, ok := e.pendingWake[t]; ok {
		delete(e.pendingWake, t)
		e.acquireLock(t, res, true)
	}

	if t.pc >= len(t.spec.Steps) {
		t.state = stateDone
		if t.finishTime == nil {
			ft := e.time
			t.finishTime = &ft
			e.events = append(e.events, Event{Time: e.time, Kind: evFinish, Task: t.spec.Name})
		}
		return false
	}
	switch t.spec.Steps[t.pc].Op {
	case OpCompute:
		return e.doCompute(t, t.spec.Steps[t.pc].Duration)
	case OpLock:
		e.doLock(t, t.spec.Steps[t.pc].Resource)
		return false
	case OpUnlock:
		e.doUnlock(t, t.spec.Steps[t.pc].Resource)
		return false
	default:
		t.state = stateDone
		return false
	}
}

func (e *engine) doCompute(t *rtTask, duration int) bool {
	if !t.computing {
		t.computing = true
		t.remaining = duration
	}
	t.remaining--
	t.runningTicks++
	finishing := t.remaining == 0
	if finishing {
		t.computing = false
		t.pc++
	}
	ev := Event{Time: e.time, Kind: evCompute, Task: t.spec.Name,
		Remaining: t.remaining}
	if finishing && t.pc >= len(t.spec.Steps) {
		// The tick [t, t+1) completes the program.
		ft := e.time + 1
		t.finishTime = &ft
		t.state = stateDone
		e.events = append(e.events, ev)
		e.events = append(e.events, Event{Time: ft, Kind: evFinish, Task: t.spec.Name})
		return true
	}
	e.events = append(e.events, ev)
	return true
}

// doLock attempts to acquire res. A resource held by another task blocks the
// caller; a free resource is acquired immediately. Nested acquisition by the
// current holder is permitted (held in acquisition order).
func (e *engine) doLock(t *rtTask, res string) {
	t.attempts = append(t.attempts, LockAttempt{Resource: res, RequestedAt: e.time})
	if holder := e.holders[res]; holder != nil && holder != t {
		t.state = stateBlocked
		t.blockedOn = res
		t.waitSince = e.time
		e.events = append(e.events, Event{Time: e.time, Kind: evBlock, Task: t.spec.Name,
			Resource: res, Holder: holder.spec.Name})
		return
	}
	e.acquireLock(t, res, false)
}

func (e *engine) acquireLock(t *rtTask, res string, handedOff bool) {
	e.holders[res] = t
	t.holds = append(t.holds, res)
	if len(t.attempts) > 0 {
		a := &t.attempts[len(t.attempts)-1]
		if a.Resource == res && !a.Acquired {
			acq := e.time
			a.AcquiredAt = &acq
			a.Acquired = true
			a.WaitedTicks = e.time - a.RequestedAt
		}
	}
	t.state = stateRunning
	t.blockedOn = ""
	kind := evLock
	if handedOff {
		kind = evWake
	}
	e.events = append(e.events, Event{Time: e.time, Kind: kind, Task: t.spec.Name,
		Resource: res})
	t.pc++ // the lock step itself costs no simulated time
}

// doUnlock releases res and hands it directly to the highest-priority waiter
// (deterministic, priority-ordered wakeup). The woken task is reconsidered at
// the same time boundary with donations recomputed away.
func (e *engine) doUnlock(t *rtTask, res string) {
	delete(e.holders, res)
	for i, r := range t.holds {
		if r == res {
			t.holds = append(t.holds[:i], t.holds[i+1:]...)
			break
		}
	}
	t.pc++
	e.events = append(e.events, Event{Time: e.time, Kind: evUnlock, Task: t.spec.Name,
		Resource: res})

	var woken *rtTask
	for _, w := range e.tasks { // index order is the deterministic tie-break
		if w.state == stateBlocked && w.blockedOn == res {
			if woken == nil || w.effective > woken.effective {
				woken = w
			}
		}
	}
	if woken != nil {
		woken.state = stateReady
		woken.blockedOn = ""
		woken.waitSince = 0
		e.pendingWake[woken] = res
	}
}

func (e *engine) allDone() bool {
	for _, t := range e.tasks {
		if t.state != stateDone {
			return false
		}
	}
	return true
}

const defaultMaxTime = 100000

func (e *engine) maxTimeValue() int {
	if e.cfg.MaxTime > 0 {
		return e.cfg.MaxTime
	}
	return defaultMaxTime
}

// jumpToNextRelease advances idle simulation time to the next pending task
// release, emitting an IDLE event. It returns false if nothing can release.
func (e *engine) jumpToNextRelease() bool {
	next := -1
	for _, t := range e.tasks {
		if t.state == statePending && t.spec.ReleaseTime > e.time {
			if next == -1 || t.spec.ReleaseTime < next {
				next = t.spec.ReleaseTime
			}
		}
	}
	if next == -1 {
		return false
	}
	e.events = append(e.events, Event{Time: e.time, Kind: evIdle, JumpTo: next,
		Message: fmt.Sprintf("no runnable task; idle until t=%d", next)})
	e.time = next
	return true
}

// findDeadlockCycle performs a DFS over the wait-for graph induced by BLOCKED
// tasks and their resource holders, returning the tasks on a cycle if any.
func (e *engine) findDeadlockCycle() ([]string, bool) {
	color := map[*rtTask]int{} // 0 white, 1 gray, 2 black
	var stack []*rtTask
	var cycle []string
	var dfs func(u *rtTask) bool
	dfs = func(u *rtTask) bool {
		color[u] = 1
		stack = append(stack, u)
		if u.state == stateBlocked {
			if v := e.holders[u.blockedOn]; v != nil {
				if color[v] == 1 {
					started := false
					for _, n := range stack {
						if n == v {
							started = true
						}
						if started {
							cycle = append(cycle, n.spec.Name)
						}
					}
					return true
				}
				if color[v] == 0 && dfs(v) {
					return true
				}
			}
		}
		color[u] = 2
		stack = stack[:len(stack)-1]
		return false
	}
	for _, t := range e.tasks {
		if color[t] == 0 {
			stack = stack[:0]
			if dfs(t) {
				return cycle, true
			}
		}
	}
	return nil, false
}

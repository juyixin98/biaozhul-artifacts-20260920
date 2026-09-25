package scheduler

// TaskState is the run-time state of a task.
type TaskState string

const (
	StatePending  TaskState = "pending"  // not yet arrived
	StateReady    TaskState = "ready"    // runnable, waiting for the processor
	StateRunning  TaskState = "running"  // executing on the processor
	StateBlocked  TaskState = "blocked"  // waiting to acquire a lock
	StateFinished TaskState = "finished" // completed
)

// EventType enumerates every state change the simulation records.
type EventType string

const (
	EvArrive        EventType = "arrive"        // task became ready
	EvDispatch      EventType = "dispatch"      // task started/resumed running
	EvPreempt       EventType = "preempt"       // running task was preempted by another
	EvCPUTick       EventType = "cpuTick"       // one tick of CPU was consumed
	EvBlock         EventType = "block"         // task blocked trying to acquire a lock
	EvWakeup        EventType = "wakeup"        // blocked task was granted the lock and made ready
	EvLockAcquired  EventType = "lockAcquired"  // task now holds a lock
	EvLockReleased  EventType = "lockReleased"  // task released a lock
	EvDonorJoin     EventType = "donorJoin"     // a task began donating priority to an owner
	EvDonorLeave    EventType = "donorLeave"    // a task stopped donating priority to an owner
	EvPriorityBoost EventType = "priorityBoost" // a task's effective priority rose
	EvPriorityReset EventType = "priorityReset" // a task's effective priority fell (typically back to base)
	EvIdle          EventType = "idle"          // processor idle; time advanced to next arrival
	EvFinish        EventType = "finish"        // task completed its program
	EvDeadlock      EventType = "deadlock"      // a wait-for cycle was detected
	EvFatal         EventType = "fatal"         // simulation aborted due to an invalid program
)

// Event is one structured record in the scheduling timeline.
// Only the fields relevant to the event type are populated.
type Event struct {
	Tick      int64     `json:"tick"`
	Seq       int       `json:"seq"`
	Type      EventType `json:"type"`
	Task      string    `json:"task,omitempty"`    // subject task
	Lock      string    `json:"lock,omitempty"`    // subject lock
	Donor     string    `json:"donor,omitempty"`   // donor task (donorJoin/Leave)
	Owner     string    `json:"owner,omitempty"`   // lock owner (donorJoin/Leave)
	By        string    `json:"by,omitempty"`      // task that caused a preemption
	From      string    `json:"from,omitempty"`    // task preempted out of the processor
	To        string    `json:"to,omitempty"`      // task dispatched onto the processor
	Old       int       `json:"oldPrio,omitempty"` // previous effective priority
	New       int       `json:"newPrio,omitempty"` // new effective priority
	Base      int       `json:"basePrio,omitempty"`
	Effective int       `json:"effPrio,omitempty"`
	Remaining int       `json:"remaining,omitempty"` // CPU ticks remaining in current instruction
	Delta     int64     `json:"delta,omitempty"`     // time advanced (idle)
	Cycle     []string  `json:"cycle,omitempty"`     // deadlock cycle, first node repeated at end
	Reason    string    `json:"reason,omitempty"`    // fatal message / extra detail
	Waiters   []string  `json:"waiters,omitempty"`   // lock wait queue snapshot
}

// LockRuntime is the final state of one lock.
type LockRuntime struct {
	ID      string   `json:"id"`
	Holder  string   `json:"holder,omitempty"`
	Waiters []string `json:"waiters"`
}

// TaskReport is the per-task summary of a run.
type TaskReport struct {
	ID             string    `json:"id"`
	BasePriority   int       `json:"basePriority"`
	Effective      int       `json:"effectivePriority"`
	State          TaskState `json:"state"`
	Arrival        int64     `json:"arrival"`
	FinishTick     int64     `json:"finishTick,omitempty"` // 0 if unfinished
	CPUTicks       int64     `json:"cpuTicks"`
	BlockedTicks   int64     `json:"blockedTicks"`
	ReadyTicks     int64     `json:"readyTicks"`
	HeldLocks      []string  `json:"heldLocks"`
	WaitingOn      string    `json:"waitingOn,omitempty"`
	PriorityBoosts int       `json:"priorityBoosts"`
}

// Report is the complete result of a run: configuration, timeline, stats.
type Report struct {
	Inheritance InheritMode   `json:"inheritance"`
	QueuePolicy QueuePolicy   `json:"queuePolicy"`
	EndTick     int64         `json:"endTick"`
	Completed   bool          `json:"completed"`
	Deadlocked  bool          `json:"deadlocked"`
	Fatal       string        `json:"fatal,omitempty"`
	Tasks       []TaskReport  `json:"tasks"`
	Locks       []LockRuntime `json:"locks"`
	Events      []Event       `json:"events"`
}

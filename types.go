// Package deadlineadm implements a single-machine, non-preemptive EDF
// scheduler with conservative admission control.
//
// Jobs declare an execution-time upper bound (budget), a demand (how much of
// the machine's capacity they occupy while running) and an absolute deadline.
// Before a job is accepted, an admission test simulates the EDF schedule of
// every already accepted unfinished job plus the candidate; the job is
// rejected as *predicted infeasible* when that schedule would miss a
// deadline. A job that runs longer than its own declared bound is a distinct
// *runtime timeout*, handled by the configured overrun policy.
//
// All state changes run on a single actor goroutine, so resource acquisition
// and release can never race: cancellation of a queued job never touches
// resources it never held, and cancellation of a running job releases its
// resources exactly once, when the executor reports termination.
package deadlineadm

import "time"

// OverrunPolicy selects what happens when a running job exceeds its declared
// execution upper bound.
type OverrunPolicy string

const (
	// PolicyKillAtBudget kills the job at its declared upper bound and counts
	// it as a runtime timeout. This is the safe default: accepted jobs cannot
	// use more time than the admission test assumed.
	PolicyKillAtBudget OverrunPolicy = "kill_at_budget"
	// PolicyObserve lets an overrunning job continue (up to its deadline as a
	// safety bound). If it completes after the deadline it is recorded as a
	// deadline miss rather than a timeout, and later jobs may cascade-miss.
	PolicyObserve OverrunPolicy = "observe"
)

// Status is the lifecycle state of a job.
type Status string

const (
	StatusQueued         Status = "queued"
	StatusRunning        Status = "running"
	StatusCompleted      Status = "completed"
	StatusDeadlineMissed Status = "deadline_missed"
	StatusTimeout        Status = "timeout"
	StatusCanceled       Status = "canceled"
	StatusFailed         Status = "failed"
)

// Terminal reports whether no further transitions are possible.
func (s Status) Terminal() bool {
	switch s {
	case StatusCompleted, StatusDeadlineMissed, StatusTimeout, StatusCanceled, StatusFailed:
		return true
	}
	return false
}

// Config configures a scheduler.
type Config struct {
	// Capacity is the machine's total resource units (default 1).
	Capacity int
	// Overrun selects runtime-overrun handling (default kill_at_budget).
	Overrun OverrunPolicy
	// EventCapacity bounds the in-memory event history (default 10000).
	EventCapacity int
}

func (c *Config) withDefaults() {
	if c.Capacity <= 0 {
		c.Capacity = 1
	}
	if c.Overrun == "" {
		c.Overrun = PolicyKillAtBudget
	}
	if c.EventCapacity <= 0 {
		c.EventCapacity = 10000
	}
}

// Job is the scheduler's internal job record.
type Job struct {
	seq       int64
	id        string
	payload   string
	demand    int
	releaseAt time.Time
	deadline  time.Time
	budget    time.Duration
	status    Status
	reason    string
	heapIndex int // index in the EDF heap while queued, -1 otherwise

	startMs int64 // simulated start time, 0 until started
	endMs   int64 // simulated terminal time, 0 until terminal
	ranMs   int64 // simulated occupied time at termination

	// running-only bookkeeping (touched solely by the actor goroutine)
	kill         func(reason string) // kill the executor with a recorded reason
	killReason   string              // budget_exceeded | deadline_reached | canceled
	stopDeadline func()              // cancel the executor context
	started      bool                // true once resources were acquired
	released     bool                // single-release guard (defensive)
	boundAt      time.Time           // running: scheduled kill instant
	killPending  bool                // killed (wake or cancel), awaiting result
}

func (j *Job) terminalTime(now time.Time) int64 {
	if j.endMs != 0 {
		return j.endMs
	}
	if j.status.Terminal() {
		return now.UnixMilli()
	}
	return 0
}

// JobView is the externally visible snapshot of a job.
type JobView struct {
	ID         string `json:"id"`
	Payload    string `json:"payload"`
	Demand     int    `json:"demand"`
	ReleaseMs  int64  `json:"release_ms"`
	DeadlineMs int64  `json:"deadline_ms"`
	BudgetMs   int64  `json:"budget_ms"`
	Status     Status `json:"status"`
	Reason     string `json:"reason,omitempty"`
	StartMs    *int64 `json:"start_ms,omitempty"`
	EndMs      *int64 `json:"end_ms,omitempty"`
	RanMs      int64  `json:"ran_ms,omitempty"`
}

func (s *Scheduler) view(j *Job) JobView {
	v := JobView{
		ID:         j.id,
		Payload:    j.payload,
		Demand:     j.demand,
		ReleaseMs:  j.releaseAt.UnixMilli(),
		DeadlineMs: j.deadline.UnixMilli(),
		BudgetMs:   j.budget.Milliseconds(),
		Status:     j.status,
		Reason:     j.reason,
		RanMs:      j.ranMs,
	}
	if j.started {
		v.StartMs = &j.startMs
	}
	if j.endMs != 0 {
		v.EndMs = &j.endMs
	}
	return v
}

// Stats are scheduler counters, including the resource-conservation invariant.
type Stats struct {
	NowMs int64 `json:"now_ms"`

	Capacity int `json:"capacity"`
	InUse    int `json:"in_use"`
	Queued   int `json:"queued"`
	Running  int `json:"running"`

	Submitted      int `json:"submitted"`
	Rejected       int `json:"rejected"`
	Completed      int `json:"completed"`
	DeadlineMissed int `json:"deadline_missed"`
	Timeout        int `json:"timeout"`
	Canceled       int `json:"canceled"`
	Failed         int `json:"failed"`

	// Deadline accounting over every accepted job.
	MetDeadline    int `json:"met_deadline"`
	MissedDeadline int `json:"missed_deadline"`

	// Resource ledger: every acquire must eventually be released exactly once.
	AcquiredTotal int  `json:"acquired_total"`
	ReleasedTotal int  `json:"released_total"`
	Conserved     bool `json:"resource_conserved"`
}

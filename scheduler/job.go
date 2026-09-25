package scheduler

import "time"

// State is the lifecycle state of a Job.
type State string

const (
	StateQueued   State = "QUEUED"
	StateRunning  State = "RUNNING"
	StateComplete State = "COMPLETED"
	// StateTimeout means the job was started but its execution exceeded the
	// declared upper bound and it was stopped. Distinct from a job that was
	// never admitted.
	StateTimeout State = "TIMEOUT"
	// StateCancelled covers both queued and running cancellations.
	StateCancelled State = "CANCELLED"
	// StateRejectedInfeasible means admission control predicted the job's
	// deadline could not be met and refused it; it never held any resource.
	StateRejectedInfeasible State = "REJECTED_INFEASIBLE"
)

// JobSpec is what a client declares at submission time.
type JobSpec struct {
	ID        string
	ExecBound time.Duration // declared upper bound on execution time
	Deadline  time.Time     // absolute deadline
	// SimActual is an optional simulation hint consumed only by demo/test
	// executors: how long the job would actually run if left alone.
	// The scheduler itself never reads it.
	SimActual time.Duration
}

// Job is a scheduled unit of work.
type Job struct {
	JobSpec

	SubmittedAt time.Time
	State       State
	StartedAt   time.Time
	FinishedAt  time.Time

	// cancelRequested is set when a running job is asked to stop; the
	// worker turns it into StateCancelled exactly once.
	cancelRequested bool
}

// DeadlineMet reports whether a terminally finished job met its deadline.
// The second return value is false for jobs that have no deadline verdict
// (queued, running, cancelled, rejected).
func (j *Job) DeadlineMet() (met bool, ok bool) {
	switch j.State {
	case StateComplete:
		return !j.FinishedAt.After(j.Deadline), true
	case StateTimeout:
		// A job that overran its declared bound never delivered, so it
		// counts as a deadline miss regardless of wall-clock time.
		return false, true
	default:
		return false, false
	}
}

package queue

import (
	"container/list"
	"context"
	"encoding/json"
	"errors"
	"time"
)

// Status is the lifecycle state of a job.
type Status string

const (
	// Queued means the job is waiting (runnable or delayed) for a worker.
	Queued Status = "queued"
	// Running means a worker is executing an attempt.
	Running Status = "running"
	// Succeeded means the last attempt succeeded. Terminal.
	Succeeded Status = "succeeded"
	// Failed means attempts were exhausted (or a fatal error occurred). Terminal.
	Failed Status = "failed"
	// Canceled means the user canceled before/during execution. Terminal.
	Canceled Status = "canceled"
)

// IsTerminal reports whether s is a terminal state.
func (s Status) IsTerminal() bool {
	return s == Succeeded || s == Failed || s == Canceled
}

// Duration is a time.Duration that marshals as a human-readable string
// ("10ms") and unmarshals from either that string or an integer nanoseconds.
type Duration time.Duration

func (d Duration) Std() time.Duration { return time.Duration(d) }

func (d Duration) MarshalJSON() ([]byte, error) {
	return json.Marshal(time.Duration(d).String())
}

func (d *Duration) UnmarshalJSON(b []byte) error {
	var v any
	if err := json.Unmarshal(b, &v); err != nil {
		return err
	}
	switch x := v.(type) {
	case string:
		parsed, err := time.ParseDuration(x)
		if err != nil {
			return err
		}
		*d = Duration(parsed)
	case float64:
		*d = Duration(int64(x))
	default:
		return errors.New("duration must be a string or integer nanoseconds")
	}
	return nil
}

// Job is the unit of work and the state record exposed to clients.
//
// Priority convention: larger numbers are more important. A job's EFFECTIVE
// priority grows as it waits: effective = BasePriority + floor(waited /
// AgeInterval), capped at MaxPriority.
type Job struct {
	ID           string          `json:"id"`
	Type         string          `json:"type"`
	Payload      json.RawMessage `json:"payload,omitempty"`
	BasePriority int             `json:"basePriority"`
	// MaxPriority bounds how high aging may promote the job; 0 means the
	// scheduler-wide MaxPriority applies.
	MaxPriority int    `json:"maxPriority,omitempty"`
	Status      Status `json:"status"`
	Attempts    int    `json:"attempts"`
	MaxAttempts int    `json:"maxAttempts"`
	// EnqueuedAt is the immutable birth time. Aging and the FIFO tie-break
	// are both anchored here and are NEVER reset, not even across retries.
	EnqueuedAt    time.Time  `json:"enqueuedAt"`
	StartedAt     *time.Time `json:"startedAt,omitempty"`
	FinishedAt    *time.Time `json:"finishedAt,omitempty"`
	LastError     string     `json:"lastError,omitempty"`
	WaitTime      Duration   `json:"waitTime,omitempty"`
	RunTime       Duration   `json:"runTime,omitempty"`
	EffectivePrio int        `json:"effectivePriority"`

	// ---- scheduler-internal state (not part of the public contract) ----

	// seq is the global birth ordering used for same-priority FIFO. It is
	// assigned once at submission and kept across retries.
	seq uint64
	// waited is accumulated queued time across delays (initial + backoffs).
	// Running time and backoff time are both counted as waiting: see Config.
	waited time.Duration
	// waitSince marks the start of the current waiting interval; zero when
	// the job is running.
	waitSince time.Time
	// runSince marks the start of the current running interval.
	runSince time.Time
	// level is the current whole aging level (effective - base).
	level int
	// availableAt is the earliest time a delayed job may be dispatched.
	availableAt time.Time
	// gen invalidates stale delayed/promotion heap entries (incremented on
	// every state transition).
	gen int
	// elem is this job's node in its effective-priority bucket list.
	elem *list.Element
	// cancel cancels the in-flight attempt's context.
	cancel context.CancelFunc
}

// Submit is the request to enqueue a job.
type Submit struct {
	Type        string          `json:"type"`
	Payload     json.RawMessage `json:"payload,omitempty"`
	Priority    int             `json:"priority"`
	MaxPriority int             `json:"maxPriority,omitempty"`
	MaxAttempts int             `json:"maxAttempts,omitempty"`
	// Delay holds the job out of the runnable set until now+Delay.
	Delay Duration `json:"delay,omitempty"`
	// ID optionally pins the job id; empty lets the scheduler generate one.
	ID string `json:"id,omitempty"`
}

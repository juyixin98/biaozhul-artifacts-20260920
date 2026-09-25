// Package domain defines the core statuses and event shape for the
// test-result merging service.
package domain

// AttemptStatus is the terminal status of a single attempt (one execution of
// a test inside one shard).
type AttemptStatus string

const (
	AttemptPassed    AttemptStatus = "passed"
	AttemptFailed    AttemptStatus = "failed"
	AttemptCancelled AttemptStatus = "cancelled"
)

// IsTerminal reports whether s is a terminal attempt status.
func (s AttemptStatus) IsTerminal() bool {
	switch s {
	case AttemptPassed, AttemptFailed, AttemptCancelled:
		return true
	}
	return false
}

// TestStatus is the effective status of a test, derived from its latest
// attempt.
type TestStatus string

const (
	TestPassed    TestStatus = "passed"
	TestFailed    TestStatus = "failed"
	TestCancelled TestStatus = "cancelled"
	// TestIncomplete means the test was observed or declared but has no
	// terminal result on its latest attempt. A missing result is NEVER
	// treated as a pass.
	TestIncomplete TestStatus = "incomplete"
)

// RunStatus is the aggregated status of a whole run.
type RunStatus string

const (
	RunPassed     RunStatus = "passed"
	RunFailed     RunStatus = "failed"
	RunCancelled  RunStatus = "cancelled"
	RunIncomplete RunStatus = "incomplete"
)

// EventType enumerates the accepted event types.
type EventType string

const (
	EventRunStarted      EventType = "run_started"
	EventRunFinished     EventType = "run_finished"
	EventShardStarted    EventType = "shard_started"
	EventShardFinished   EventType = "shard_finished"
	EventAttemptStarted  EventType = "attempt_started"
	EventAttemptFinished EventType = "attempt_finished"
)

// Event is one test-execution event. All JSON fields are kept in a single
// struct; which fields are meaningful depends on Type (validated elsewhere).
//
// TestID identifies the test; AttemptID identifies one execution of that
// test. They are deliberately independent: one test may have several
// attempts across retries, shards or executors.
type Event struct {
	// EventID is the globally unique id of this event, used for idempotent
	// deduplication. Replaying the same EventID twice has no extra effect.
	EventID string `json:"event_id"`
	// SeqNo is the executor-assigned monotonic sequence number within a run.
	// All events except run_started must carry it; seq order (not arrival
	// order) determines the canonical reduction.
	SeqNo int64     `json:"seq_no,omitempty"`
	Type  EventType `json:"type"`
	RunID string    `json:"run_id"`
	// Shard is present for shard_* and attempt_* events.
	Shard string `json:"shard,omitempty"`
	// TestID / AttemptID are present for attempt_* events.
	TestID    string `json:"test_id,omitempty"`
	AttemptID string `json:"attempt_id,omitempty"`
	// Status is an AttemptStatus on attempt_finished and a RunStatus on
	// run_finished.
	Status string `json:"status,omitempty"`
	// AttemptNo orders attempts of one test (e.g. 1 for the first run,
	// 2 for the first retry). The highest number is the "latest" attempt.
	AttemptNo int `json:"attempt_no,omitempty"`
	// Cancelled is set on run_finished to mark a cancellation.
	Cancelled bool `json:"cancelled,omitempty"`
	// Message is an optional human-readable detail (failure reason, ...).
	Message string `json:"message,omitempty"`
	At      string `json:"at,omitempty"`
	// Tests optionally declares the full set of test ids that belong to a
	// run (on run_started). Declared-but-never-observed tests are reported
	// as incomplete, never as passed.
	Tests []string `json:"tests,omitempty"`
}

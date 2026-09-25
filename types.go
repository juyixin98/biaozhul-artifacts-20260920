// Package trmerge implements multi-shard test event merging.
//
// The domain model keeps two identities separate:
//
//   - Test ID (test_id): the logical test definition, e.g. "pkg/api/login".
//   - Attempt ID (attempt_id): one concrete execution of that test.
//
// A test can therefore have several attempts (retries after an executor
// crash, flaky reruns). Attempts are ordered with an attempt number.
package trmerge

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// Status values for attempts, tests, shards and runs.
//
// Passed/Failed/Canceled/Incomplete are the four explicit terminal outcomes
// for a test. Incomplete means "no definite answer was ever recorded" —
// an executor crash before a result arrived, a missing shard, a result that
// never showed up. Incomplete is never counted as passed.
const (
	StatusRunning    = "running"
	StatusPassed     = "passed"
	StatusFailed     = "failed"
	StatusCanceled   = "canceled"
	StatusIncomplete = "incomplete"

	// Lifecycle statuses used by shards and runs (not tests/attempts).
	StatusPending   = "pending"
	StatusCompleted = "completed"
	StatusCrashed   = "crashed"
	StatusFinalized = "finalized"
)

// Event types carried by an Event.
const (
	EvRunStarted         = "run_started"
	EvRunCancelRequested = "run_cancel_requested"
	EvShardStarted       = "shard_started"
	EvAttemptStarted     = "attempt_started"
	EvAttemptResult      = "attempt_result"
	EvShardFinished      = "shard_finished"
	EvRunFinalized       = "run_finalized"
	EvShardWarning       = "shard_warning"
)

// Event is one JSON line emitted by an executor/fixture, or synthesized by
// the local runner. Unknown JSON fields are accepted and ignored; required
// fields are validated by DecodeEvent.
type Event struct {
	// EventID is a client-supplied idempotency key for the event itself
	// (distinct from attempt_id). Optional; the store assigns one when empty.
	EventID string `json:"event_id,omitempty"`

	Type string `json:"type"`

	RunID   string `json:"run_id,omitempty"`
	ShardID string `json:"shard_id,omitempty"`

	TestID    string `json:"test_id,omitempty"`
	AttemptID string `json:"attempt_id,omitempty"`
	AttemptNo int    `json:"attempt_no,omitempty"`

	// Outcome of an attempt_result: passed | failed | canceled.
	Result string `json:"result,omitempty"`
	// Optional coarse reason, e.g. "timeout" / "assertion". Informational.
	Reason  string `json:"reason,omitempty"`
	Message string `json:"message,omitempty"`

	// shard_finished carries the executor process outcome.
	ExitCode *int   `json:"exit_code,omitempty"`
	Outcome  string `json:"outcome,omitempty"` // completed | crashed | canceled
	Error    string `json:"error,omitempty"`
}

// DecodeEvent validates one decoded event and returns a typed error listing
// the field problem. The caller decides whether a validation failure makes
// the whole batch fail or is just recorded.
func DecodeEvent(ev *Event) error {
	if strings.TrimSpace(ev.Type) == "" {
		return errors.New("event has empty type")
	}
	require := func(cond bool, field string) error {
		if !cond {
			return fmt.Errorf("event %s: missing required field %s", ev.Type, field)
		}
		return nil
	}
	switch ev.Type {
	case EvRunStarted, EvRunCancelRequested, EvRunFinalized:
		// run-scoped only.
	case EvShardStarted, EvShardFinished, EvShardWarning:
		if err := require(ev.ShardID != "", "shard_id"); err != nil {
			return err
		}
		if ev.Type == EvShardFinished {
			switch ev.Outcome {
			case StatusCompleted, StatusCrashed, StatusCanceled:
			default:
				return fmt.Errorf("event shard_finished: outcome must be one of completed/crashed/canceled, got %q", ev.Outcome)
			}
		}
	case EvAttemptStarted:
		if err := require(ev.ShardID != "", "shard_id"); err != nil {
			return err
		}
		if err := require(ev.TestID != "", "test_id"); err != nil {
			return err
		}
		if err := require(ev.AttemptID != "", "attempt_id"); err != nil {
			return err
		}
		if err := require(ev.AttemptNo >= 1, "attempt_no>=1"); err != nil {
			return err
		}
	case EvAttemptResult:
		if err := require(ev.TestID != "", "test_id"); err != nil {
			return err
		}
		if err := require(ev.AttemptID != "", "attempt_id"); err != nil {
			return err
		}
		if err := require(ev.AttemptNo >= 1, "attempt_no>=1"); err != nil {
			return err
		}
		switch ev.Result {
		case StatusPassed, StatusFailed, StatusCanceled:
		default:
			return fmt.Errorf("event attempt_result: result must be one of passed/failed/canceled, got %q", ev.Result)
		}
	default:
		return fmt.Errorf("unknown event type %q", ev.Type)
	}
	return nil
}

// IsTerminalResult reports whether an attempt result string is terminal.
func IsTerminalResult(r string) bool {
	return r == StatusPassed || r == StatusFailed || r == StatusCanceled
}

// itoa keeps numbers stable and allocation-light in log lines.
func itoa(n int) string { return strconv.Itoa(n) }

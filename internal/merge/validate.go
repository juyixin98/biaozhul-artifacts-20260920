package merge

import (
	"fmt"

	"github.com/local/testmerge/internal/domain"
)

// Validate checks one event for structural/type correctness. It does not
// depend on any accumulated state, so it can run at ingestion time as well
// as during replay.
func Validate(e domain.Event) error {
	if e.EventID == "" {
		return fmt.Errorf("event_id is required")
	}
	if e.RunID == "" {
		return fmt.Errorf("run_id is required (event %s)", e.EventID)
	}
	switch e.Type {
	case domain.EventRunStarted:
		// seq_no 0 is conventional and optional
		if e.SeqNo < 0 {
			return fmt.Errorf("run_started %s: seq_no must be >= 0", e.EventID)
		}
	case domain.EventRunFinished:
		if e.SeqNo <= 0 {
			return fmt.Errorf("run_finished %s: seq_no must be > 0", e.EventID)
		}
		if e.Status != "" {
			switch domain.RunStatus(e.Status) {
			case domain.RunPassed, domain.RunFailed, domain.RunCancelled, domain.RunIncomplete:
			default:
				return fmt.Errorf("run_finished %s: invalid status %q", e.EventID, e.Status)
			}
		}
	case domain.EventShardStarted, domain.EventShardFinished:
		if e.SeqNo <= 0 {
			return fmt.Errorf("%s %s: seq_no must be > 0", e.Type, e.EventID)
		}
		if e.Shard == "" {
			return fmt.Errorf("%s %s: shard is required", e.Type, e.EventID)
		}
	case domain.EventAttemptStarted:
		if e.SeqNo <= 0 {
			return fmt.Errorf("attempt_started %s: seq_no must be > 0", e.EventID)
		}
		if e.Shard == "" || e.TestID == "" || e.AttemptID == "" {
			return fmt.Errorf("attempt_started %s: shard, test_id and attempt_id are required", e.EventID)
		}
	case domain.EventAttemptFinished:
		if e.SeqNo <= 0 {
			return fmt.Errorf("attempt_finished %s: seq_no must be > 0", e.EventID)
		}
		if e.Shard == "" || e.TestID == "" || e.AttemptID == "" {
			return fmt.Errorf("attempt_finished %s: shard, test_id and attempt_id are required", e.EventID)
		}
		if !domain.AttemptStatus(e.Status).IsTerminal() {
			return fmt.Errorf("attempt_finished %s: status must be one of passed, failed, cancelled (got %q)", e.EventID, e.Status)
		}
	default:
		return fmt.Errorf("event %s: unknown event type %q", e.EventID, e.Type)
	}
	return nil
}

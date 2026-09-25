package merge

import (
	"fmt"
	"sort"

	"github.com/local/testmerge/internal/domain"
)

// ReduceResult reports what happened during a reduction.
type ReduceResult struct {
	// Unique is the number of distinct events applied (duplicates excluded).
	Unique int
	// Duplicate is the number of events whose EventID appeared more than
	// once in the input and were therefore applied only once.
	Duplicate int
}

// Reduce deterministically folds events into a Run for runID.
//
// Semantics:
//   - duplicate EventIDs are applied exactly once (replay/out-of-order safe);
//   - events are applied in canonical order (SeqNo, EventID), so arrival
//     order cannot change the outcome;
//   - two different events with the same SeqNo are rejected (a broken
//     executor), because canonical order would otherwise be ambiguous.
func Reduce(runID string, events []domain.Event) (*Run, ReduceResult, error) {
	seen := map[string]struct{}{}
	uniq := make([]domain.Event, 0, len(events))
	dups := 0
	for _, e := range events {
		if e.RunID != "" && e.RunID != runID {
			return nil, ReduceResult{}, fmt.Errorf("event %s belongs to run %q, not %q", e.EventID, e.RunID, runID)
		}
		if err := Validate(e); err != nil {
			return nil, ReduceResult{}, err
		}
		if _, ok := seen[e.EventID]; ok {
			dups++
			continue
		}
		seen[e.EventID] = struct{}{}
		uniq = append(uniq, e)
	}

	sort.SliceStable(uniq, func(i, j int) bool {
		if uniq[i].SeqNo != uniq[j].SeqNo {
			return uniq[i].SeqNo < uniq[j].SeqNo
		}
		return uniq[i].EventID < uniq[j].EventID
	})

	// Detect seq collisions among non-run_started events.
	prevSeq := int64(-1)
	prevID := ""
	for _, e := range uniq {
		if e.Type == domain.EventRunStarted {
			continue
		}
		if e.SeqNo == prevSeq {
			return nil, ReduceResult{}, fmt.Errorf("seq_no collision: events %q and %q both use seq_no=%d", prevID, e.EventID, e.SeqNo)
		}
		prevSeq = e.SeqNo
		prevID = e.EventID
	}

	r := newRun(runID)
	for _, e := range uniq {
		if err := r.apply(e); err != nil {
			return nil, ReduceResult{}, err
		}
	}
	return r, ReduceResult{Unique: len(uniq), Duplicate: dups}, nil
}

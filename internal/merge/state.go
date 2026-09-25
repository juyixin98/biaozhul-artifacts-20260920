// Package merge contains the pure event-reduction engine.
//
// The reducer is deterministic: given the same multiset of events it always
// produces the same summary, regardless of the order in which events arrived.
// Canonical order is (SeqNo, EventID); duplicate EventIDs are applied once.
package merge

import (
	"fmt"

	"github.com/local/testmerge/internal/domain"
)

// attempt is the reduced state of one attempt of one test.
type attempt struct {
	testID      string
	attemptID   string
	shard       string
	no          int
	status      domain.AttemptStatus // empty while no terminal result
	message     string
	startedSeq  int64
	finishedSeq int64 // -1 while no terminal result
	// lateWrites counts terminal results that arrived for an attempt which
	// was already terminal. They are counted but NEVER overwrite the first
	// recorded terminal status.
	lateWrites int
	// dupStarts counts attempt_started events arriving after the attempt
	// already existed (including after an out-of-order finish).
	dupStarts int
	// startSeen records whether attempt_started was observed. A finish
	// without it (e.g. shard crashed mid-start event) is an orphan finish.
	startSeen bool
	seenOrder int // order in which the attempt was first observed
}

func (a *attempt) terminal() bool { return a.status.IsTerminal() }

type shardState struct {
	name        string
	started     bool
	startedSeq  int64
	finished    bool
	finishedSeq int64
}

// Run is the reduced state of one test run.
type Run struct {
	RunID string

	started      bool
	startedSeq   int64
	finished     bool
	finishedSeq  int64
	cancelled    bool
	finishStatus domain.RunStatus

	shards map[string]*shardState

	// attempts keyed by testID + "\x00" + attemptID
	attempts map[string]*attempt
	// testOrder preserves first-observation order of attempt ids per test.
	testOrder map[string][]string
	// known tests: declared in run_started union observed in events
	tests    map[string]struct{}
	declared []string

	eventIDs map[string]struct{}
}

func newRun(runID string) *Run {
	return &Run{
		RunID:     runID,
		shards:    map[string]*shardState{},
		attempts:  map[string]*attempt{},
		testOrder: map[string][]string{},
		tests:     map[string]struct{}{},
		eventIDs:  map[string]struct{}{},
	}
}

func attemptKey(testID, attemptID string) string { return testID + "\x00" + attemptID }

func (r *Run) shard(name string) *shardState {
	s := r.shards[name]
	if s == nil {
		s = &shardState{name: name, finishedSeq: -1, startedSeq: -1}
		r.shards[name] = s
	}
	return s
}

func (r *Run) getOrCreateAttempt(e domain.Event, seq int64) *attempt {
	key := attemptKey(e.TestID, e.AttemptID)
	a := r.attempts[key]
	if a == nil {
		a = &attempt{
			testID:      e.TestID,
			attemptID:   e.AttemptID,
			shard:       e.Shard,
			no:          e.AttemptNo,
			startedSeq:  seq,
			finishedSeq: -1,
			seenOrder:   len(r.attempts),
		}
		r.attempts[key] = a
		r.testOrder[e.TestID] = append(r.testOrder[e.TestID], e.AttemptID)
		r.tests[e.TestID] = struct{}{}
	}
	return a
}

func (r *Run) apply(e domain.Event) error {
	if _, dup := r.eventIDs[e.EventID]; dup {
		return nil // idempotent replay
	}
	r.eventIDs[e.EventID] = struct{}{}

	switch e.Type {
	case domain.EventRunStarted:
		if !r.started {
			r.started = true
			r.startedSeq = e.SeqNo
		}
		for _, t := range e.Tests {
			if _, ok := r.tests[t]; !ok {
				r.tests[t] = struct{}{}
				r.declared = append(r.declared, t)
			}
		}

	case domain.EventRunFinished:
		if !r.finished {
			r.finished = true
			r.finishedSeq = e.SeqNo
			r.cancelled = e.Cancelled
			if e.Status != "" {
				r.finishStatus = domain.RunStatus(e.Status)
			}
		}

	case domain.EventShardStarted:
		s := r.shard(e.Shard)
		if !s.started {
			s.started = true
			s.startedSeq = e.SeqNo
		}

	case domain.EventShardFinished:
		s := r.shard(e.Shard)
		if !s.finished {
			s.finished = true
			s.finishedSeq = e.SeqNo
		}

	case domain.EventAttemptStarted:
		a := r.getOrCreateAttempt(e, e.SeqNo)
		if a.shard == "" {
			a.shard = e.Shard
		}
		if a.no == 0 && e.AttemptNo > 0 {
			a.no = e.AttemptNo
		}
		if a.startSeen {
			a.dupStarts++
		} else {
			a.startSeen = true
		}

	case domain.EventAttemptFinished:
		st := domain.AttemptStatus(e.Status)
		if !st.IsTerminal() {
			return fmt.Errorf("attempt_finished %s: invalid status %q", e.EventID, e.Status)
		}
		a := r.getOrCreateAttempt(e, e.SeqNo)
		if a.shard == "" {
			a.shard = e.Shard
		}
		if a.no == 0 && e.AttemptNo > 0 {
			a.no = e.AttemptNo
		}
		if a.terminal() {
			// Late or duplicated result for an attempt whose outcome is
			// already known: ignore the payload, count the write.
			a.lateWrites++
			return nil
		}
		a.status = st
		a.message = e.Message
		a.finishedSeq = e.SeqNo

	default:
		return fmt.Errorf("unknown event type %q (event %s)", e.Type, e.EventID)
	}
	return nil
}

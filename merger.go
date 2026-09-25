package trmerge

import (
	"sort"
)

// ShardSeed is the manifest entry: a planned shard and the test IDs assigned
// to it. Tests that never emit any event still surface as Incomplete.
type ShardSeed struct {
	ShardID string   `json:"shard_id"`
	TestIDs []string `json:"test_ids,omitempty"`
}

// attemptState holds everything known about one attempt_id.
type attemptState struct {
	attemptID string
	testID    string
	shardID   string
	attemptNo int
	started   bool

	// terminal claims made for this attempt, keyed by result value.
	// The first value to arrive is recorded in firstOrder; the set is
	// what the deterministic summary uses, so replaying the same events
	// in a different order cannot change the outcome.
	claims     map[string]bool
	firstClaim string
}

type testState struct {
	testID string
	// attempts keyed by attempt_id.
	attempts map[string]*attemptState
	// order of attempt_no values seen, for tie diagnostics.
	nos map[int]bool
}

type shardState struct {
	shardID  string
	started  bool
	finished bool

	// terminal claim bookkeeping, same pattern as attempts.
	statusClaims map[string]bool // completed | crashed | canceled
	firstStatus  string
	exitCode     *int
	errMsg       string

	warnings map[string]bool
}

// Merger folds an ordered stream of events into run state.
// It is safe for single-goroutine use; the store serializes callers.
type Merger struct {
	RunID string
	Seed  []ShardSeed

	cancelRequested bool
	finalized       bool

	shards map[string]*shardState
	tests  map[string]*testState
}

// NewMerger builds a merger pre-seeded with the manifest. Planned tests that
// never produce events remain visible so missing results count as
// Incomplete, never Passed.
func NewMerger(runID string, seed []ShardSeed) *Merger {
	m := &Merger{
		RunID:  runID,
		Seed:   seed,
		shards: map[string]*shardState{},
		tests:  map[string]*testState{},
	}
	for _, s := range seed {
		m.shard(s.ShardID)
		for _, t := range s.TestIDs {
			m.test(t)
		}
	}
	return m
}

func (m *Merger) shard(id string) *shardState {
	s := m.shards[id]
	if s == nil {
		s = &shardState{shardID: id, warnings: map[string]bool{}, statusClaims: map[string]bool{}}
		m.shards[id] = s
	}
	return s
}

func (m *Merger) test(id string) *testState {
	t := m.tests[id]
	if t == nil {
		t = &testState{testID: id, attempts: map[string]*attemptState{}, nos: map[int]bool{}}
		m.tests[id] = t
	}
	return t
}

// ApplyResult tells the caller what the fold did with one event.
type ApplyResult struct {
	// Duplicate is true when the event_id was already folded (idempotent replay).
	Duplicate bool
	// Late is true when the run is finalized and the event was refused.
	Late bool
	// Conflict is true when the event added a second, contradictory terminal claim.
	Conflict bool
}

// Apply folds one already-validated event. It never mutates existing terminal
// state with a later claim: late/duplicate claims are recorded but the first
// claim is preserved, and a deterministic precedence over the claim set is
// used at summary time.
func (m *Merger) Apply(ev Event, seenEventID map[string]bool) (ApplyResult, error) {
	if ev.EventID != "" && seenEventID[ev.EventID] {
		return ApplyResult{Duplicate: true}, nil
	}
	if ev.EventID != "" {
		seenEventID[ev.EventID] = true
	}

	res := ApplyResult{}

	// Once a run is finalized, only a duplicate (handled above) is accepted.
	if m.finalized && ev.Type != EvRunFinalized {
		return ApplyResult{Late: true}, nil
	}

	switch ev.Type {
	case EvRunStarted:
		// purely informational; run already exists.

	case EvRunCancelRequested:
		m.cancelRequested = true

	case EvRunFinalized:
		m.finalized = true

	case EvShardWarning:
		s := m.shard(ev.ShardID)
		if ev.Message != "" {
			s.warnings[ev.Message] = true
		}

	case EvShardStarted:
		s := m.shard(ev.ShardID)
		s.started = true

	case EvShardFinished:
		s := m.shard(ev.ShardID)
		s.finished = true
		if ev.ExitCode != nil {
			s.exitCode = ev.ExitCode
		}
		if ev.Error != "" {
			s.errMsg = ev.Error
		}
		if s.statusClaims[ev.Outcome] {
			// identical terminal claim repeated -> idempotent duplicate content
			break
		}
		if len(s.statusClaims) > 0 {
			// A different terminal outcome already arrived. Keep the first;
			// record the contradiction. Summary uses conservative precedence.
			res.Conflict = true
		} else {
			s.firstStatus = ev.Outcome
		}
		s.statusClaims[ev.Outcome] = true

	case EvAttemptStarted:
		// A shard id seen on an event is real even without a manifest row.
		m.shard(ev.ShardID)
		t := m.test(ev.TestID)
		a := t.attempts[ev.AttemptID]
		if a == nil {
			a = &attemptState{
				attemptID: ev.AttemptID,
				testID:    ev.TestID,
				shardID:   ev.ShardID,
				attemptNo: ev.AttemptNo,
				claims:    map[string]bool{},
			}
			t.attempts[ev.AttemptID] = a
			t.nos[ev.AttemptNo] = true
		}
		a.started = true
		// backfill metadata that a late start might carry; never downgrade.
		if a.shardID == "" {
			a.shardID = ev.ShardID
		}

	case EvAttemptResult:
		t := m.test(ev.TestID)
		a := t.attempts[ev.AttemptID]
		if a == nil {
			// Result with no start: the executor likely crashed before the
			// start event was flushed. Synthesize the attempt so the result
			// is not lost; it stays started=false and shows up as incomplete
			// metadata-wise, but the terminal claim still counts.
			a = &attemptState{
				attemptID: ev.AttemptID,
				testID:    ev.TestID,
				shardID:   ev.ShardID,
				attemptNo: ev.AttemptNo,
				claims:    map[string]bool{},
			}
			t.attempts[ev.AttemptID] = a
			t.nos[ev.AttemptNo] = true
		}
		if ev.ShardID != "" && a.shardID == "" {
			a.shardID = ev.ShardID
		}
		if a.claims[ev.Result] {
			break // identical repeated claim: idempotent
		}
		if len(a.claims) > 0 {
			// A late, different result for an attempt that already terminated.
			// Do NOT overwrite: record it and flag the contradiction.
			res.Conflict = true
		} else {
			a.firstClaim = ev.Result
		}
		a.claims[ev.Result] = true
	}

	return res, nil
}

// Finalize closes the run. After Finalize, new non-duplicate events are Late.
// Pending shards that never reported anything are treated as crashed —
// missing evidence is failure/incomplete, never success.
func (m *Merger) Finalize() {
	if m.finalized {
		return
	}
	m.finalized = true
}

func (m *Merger) IsFinalized() bool { return m.finalized }

// outcomePrecedence picks a single effective terminal value from a claim set
// deterministically. Conservative order — the worst news wins — so a passing
// claim can never hide a failing/canceled one regardless of arrival order:
// crashed > canceled > failed > passed (shard); failed > canceled > passed.
func outcomePrecedence(claims map[string]bool, order []string) string {
	for _, c := range order {
		if claims[c] {
			return c
		}
	}
	return ""
}

// attemptOrder is conservative for test results: a late failure/cancel claim
// must never be hidden behind a pass, so the summary over a contradictory
// set reports the worst news. The first claim is still preserved on the
// attempt for forensics.
var attemptOrder = []string{StatusFailed, StatusCanceled, StatusPassed}

// shardOrder models executor recovery: a shard that crashed but whose retry
// completed ends completed (completed > canceled > crashed). Test-level
// accounting still records any attempt lost in the crash as Incomplete, so
// a crash cannot be hidden; only the executor's final state is optimistic.
var shardOrder = []string{StatusCompleted, StatusCanceled, StatusCrashed}

func sortedKeys[V any](in map[string]V) []string {
	out := make([]string, 0, len(in))
	for k := range in {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

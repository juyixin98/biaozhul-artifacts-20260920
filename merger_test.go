package trmerge

import (
	"encoding/json"
	"testing"
)

// helpers ---------------------------------------------------------------

func evAttemptStarted(shard, test, attempt string, no int) Event {
	return Event{Type: EvAttemptStarted, ShardID: shard, TestID: test, AttemptID: attempt, AttemptNo: no}
}

func evResult(shard, test, attempt string, no int, result string) Event {
	return Event{Type: EvAttemptResult, ShardID: shard, TestID: test, AttemptID: attempt, AttemptNo: no, Result: result}
}

func seed1() []ShardSeed {
	return []ShardSeed{{ShardID: "s1", TestIDs: []string{"t1"}}}
}

func fold(t *testing.T, seed []ShardSeed, evs []Event) *Merger {
	t.Helper()
	m := NewMerger("r", seed)
	seen := map[string]bool{}
	for i, e := range evs {
		// give every event a stable id so dedupe is testable when wanted
		if e.EventID == "" {
			e.EventID = "ev" + itoa(i)
		}
		if _, err := m.Apply(e, seen); err != nil {
			t.Fatalf("apply %d (%s): %v", i, e.Type, err)
		}
	}
	return m
}

func testStatusOf(sum Summary, testID string) string {
	for _, ts := range sum.Tests {
		if ts.TestID == testID {
			return ts.Status
		}
	}
	return "<missing>"
}

func shardStatusOf(sum Summary, shardID string) string {
	for _, ss := range sum.Shards {
		if ss.ShardID == shardID {
			return ss.Status
		}
	}
	return "<missing>"
}

// tests -----------------------------------------------------------------

func TestPassedOnRetryUsesLatestAttempt(t *testing.T) {
	// test_id is stable across retries; attempt_id/attempt_no distinguish tries.
	evs := []Event{
		{Type: EvShardStarted, ShardID: "s1", EventID: "e0"},
		evAttemptStarted("s1", "t1", "t1-a1", 1),
		evResult("s1", "t1", "t1-a1", 1, StatusFailed),
		evAttemptStarted("s1", "t1", "t1-a2", 2),
		evResult("s1", "t1", "t1-a2", 2, StatusPassed),
		{Type: EvShardFinished, ShardID: "s1", Outcome: StatusCompleted, EventID: "e6"},
		{Type: EvRunFinalized, EventID: "e7"},
	}
	m := fold(t, seed1(), evs)
	sum := m.Snapshot()
	if got := testStatusOf(sum, "t1"); got != StatusPassed {
		t.Fatalf("test status = %q, want passed (latest attempt passed)", got)
	}
	if sum.Counts.Passed != 1 || sum.Counts.Failed != 0 {
		t.Fatalf("counts = %+v, want 1 passed", sum.Counts)
	}
	if sum.Status != StatusCompleted {
		t.Fatalf("run status = %q, want completed", sum.Status)
	}
	// both attempts are retained for forensics
	var ts TestSnapshot
	for _, x := range sum.Tests {
		if x.TestID == "t1" {
			ts = x
		}
	}
	if len(ts.Attempts) != 2 || ts.LatestAttemptNo != 2 {
		t.Fatalf("attempts = %+v", ts.Attempts)
	}
}

func TestFailedStaysFailed(t *testing.T) {
	evs := []Event{
		evAttemptStarted("s1", "t1", "a1", 1),
		evResult("s1", "t1", "a1", 1, StatusFailed),
		{Type: EvShardFinished, ShardID: "s1", Outcome: StatusCompleted},
		{Type: EvRunFinalized},
	}
	sum := fold(t, seed1(), evs).Snapshot()
	if got := testStatusOf(sum, "t1"); got != StatusFailed {
		t.Fatalf("got %q want failed", got)
	}
	if sum.Status != StatusFailed {
		t.Fatalf("run = %q want failed", sum.Status)
	}
}

func TestCrashBeforeResultIsIncompleteNotPassed(t *testing.T) {
	evs := []Event{
		{Type: EvShardStarted, ShardID: "s1"},
		evAttemptStarted("s1", "t1", "a1", 1),
		// executor crashes: no result ever arrives
		{Type: EvShardFinished, ShardID: "s1", Outcome: StatusCrashed, ExitCode: intPtr(137)},
		{Type: EvRunFinalized},
	}
	sum := fold(t, seed1(), evs).Snapshot()
	if got := testStatusOf(sum, "t1"); got != StatusIncomplete {
		t.Fatalf("got %q want incomplete", got)
	}
	if shardStatusOf(sum, "s1") != StatusCrashed {
		t.Fatalf("shard = %q want crashed", shardStatusOf(sum, "s1"))
	}
	if sum.Status != StatusFailed {
		t.Fatalf("run = %q want failed (incomplete must not pass)", sum.Status)
	}
	if sum.Counts.Passed != 0 || sum.Counts.Incomplete != 1 {
		t.Fatalf("counts = %+v", sum.Counts)
	}
}

func TestMissingManifestTestIsIncomplete(t *testing.T) {
	// t2 is planned but emits nothing; shard completes fine.
	seed := []ShardSeed{{ShardID: "s1", TestIDs: []string{"t1", "t2"}}}
	evs := []Event{
		evAttemptStarted("s1", "t1", "a1", 1),
		evResult("s1", "t1", "a1", 1, StatusPassed),
		{Type: EvShardFinished, ShardID: "s1", Outcome: StatusCompleted},
		{Type: EvRunFinalized},
	}
	sum := fold(t, seed, evs).Snapshot()
	if testStatusOf(sum, "t1") != StatusPassed || testStatusOf(sum, "t2") != StatusIncomplete {
		t.Fatalf("t1=%s t2=%s", testStatusOf(sum, "t1"), testStatusOf(sum, "t2"))
	}
	if sum.Status != StatusFailed {
		t.Fatalf("run = %q, a missing result means failure not success", sum.Status)
	}
}

func TestMissingShardIsCrashed(t *testing.T) {
	// Two planned shards; s2 never reports after finalize.
	seed := []ShardSeed{
		{ShardID: "s1", TestIDs: []string{"t1"}},
		{ShardID: "s2", TestIDs: []string{"t2"}},
	}
	evs := []Event{
		evAttemptStarted("s1", "t1", "a1", 1),
		evResult("s1", "t1", "a1", 1, StatusPassed),
		{Type: EvShardFinished, ShardID: "s1", Outcome: StatusCompleted},
		{Type: EvRunFinalized},
	}
	sum := fold(t, seed, evs).Snapshot()
	if shardStatusOf(sum, "s2") != StatusCrashed {
		t.Fatalf("s2 = %q want crashed", shardStatusOf(sum, "s2"))
	}
	if testStatusOf(sum, "t2") != StatusIncomplete {
		t.Fatalf("t2 = %q want incomplete", testStatusOf(sum, "t2"))
	}
	if sum.Status != StatusFailed {
		t.Fatalf("run = %q want failed", sum.Status)
	}
}

func TestLateResultForOldAttemptDoesNotChangeTest(t *testing.T) {
	// a1 crashes (no result); retry a2 passes. Then a1's result shows up LATE
	// (delayed delivery). It must not affect the test — the retry is current.
	evs := []Event{
		{Type: EvShardStarted, ShardID: "s1"},
		evAttemptStarted("s1", "t1", "t1-a1", 1),
		{Type: EvShardFinished, ShardID: "s1", Outcome: StatusCrashed},
		// retry on a (possibly new) executor
		evAttemptStarted("s1", "t1", "t1-a2", 2),
		evResult("s1", "t1", "t1-a2", 2, StatusPassed),
		// late arrival for the dead attempt
		evResult("s1", "t1", "t1-a1", 1, StatusFailed),
		{Type: EvShardFinished, ShardID: "s1", Outcome: StatusCompleted},
		{Type: EvRunFinalized},
	}
	sum := fold(t, seed1(), evs).Snapshot()
	if got := testStatusOf(sum, "t1"); got != StatusPassed {
		t.Fatalf("test = %q, late result for an old attempt must not change it", got)
	}
}

func TestDuplicateResultIsIdempotent(t *testing.T) {
	base := []Event{
		evAttemptStarted("s1", "t1", "a1", 1),
		evResult("s1", "t1", "a1", 1, StatusPassed),
	}
	m := fold(t, seed1(), base)
	seen := map[string]bool{"ev0": true, "ev1": true}
	// same event_id delivered again
	dup := evResult("s1", "t1", "a1", 1, StatusPassed)
	dup.EventID = "ev1"
	ar, err := m.Apply(dup, seen)
	if err != nil {
		t.Fatal(err)
	}
	if !ar.Duplicate {
		t.Fatalf("expected duplicate, got %+v", ar)
	}
}

func TestContradictoryResultFlagsConflictAndConservative(t *testing.T) {
	// Two DISTINCT messages claim different terminal results for one attempt.
	// The first claim is preserved and a conflict is recorded; conservative
	// precedence ensures a pass can never hide a failure.
	m := fold(t, seed1(), []Event{
		evAttemptStarted("s1", "t1", "a1", 1),
		evResult("s1", "t1", "a1", 1, StatusPassed),
	})
	seen := map[string]bool{"ev0": true, "ev1": true}
	late := evResult("s1", "t1", "a1", 1, StatusFailed)
	late.EventID = "ev-late"
	ar, err := m.Apply(late, seen)
	if err != nil {
		t.Fatal(err)
	}
	if !ar.Conflict {
		t.Fatalf("expected conflict flag, got %+v", ar)
	}
	m.Finalize()
	sum := m.Snapshot()
	if got := testStatusOf(sum, "t1"); got != StatusFailed {
		t.Fatalf("contradictory claims resolve conservatively, got %q", got)
	}
	if len(sum.Conflicts) == 0 {
		t.Fatal("expected a conflict entry")
	}
	// the original first claim is preserved, not overwritten
	var ts TestSnapshot
	for _, x := range sum.Tests {
		if x.TestID == "t1" {
			ts = x
		}
	}
	if ts.Attempts[0].FirstClaim != StatusPassed {
		t.Fatalf("first claim not preserved: %+v", ts.Attempts[0])
	}
}

func TestCanceledStatus(t *testing.T) {
	evs := []Event{
		{Type: EvShardStarted, ShardID: "s1"},
		evAttemptStarted("s1", "t1", "a1", 1),
		{Type: EvRunCancelRequested},
		// no result
		{Type: EvShardFinished, ShardID: "s1", Outcome: StatusCanceled},
		{Type: EvRunFinalized},
	}
	sum := fold(t, seed1(), evs).Snapshot()
	if got := testStatusOf(sum, "t1"); got != StatusCanceled {
		t.Fatalf("test = %q want canceled", got)
	}
	if sum.Status != StatusCanceled {
		t.Fatalf("run = %q want canceled", sum.Status)
	}
	if sum.Counts.Canceled != 1 || sum.Counts.Passed != 0 {
		t.Fatalf("counts = %+v", sum.Counts)
	}
}

func TestLateEventsAfterFinalizeAreRejected(t *testing.T) {
	m := fold(t, seed1(), []Event{
		evAttemptStarted("s1", "t1", "a1", 1),
		{Type: EvRunFinalized},
	})
	seen := map[string]bool{"ev0": true, "ev1": true}
	ar, err := m.Apply(evResult("s1", "t1", "a1", 1, StatusPassed), seen)
	if err != nil {
		t.Fatal(err)
	}
	if !ar.Late {
		t.Fatalf("post-finalize result must be late, got %+v", ar)
	}
	sum := m.Snapshot()
	if testStatusOf(sum, "t1") != StatusIncomplete {
		t.Fatalf("late post-finalize pass must not count, got %q", testStatusOf(sum, "t1"))
	}
}

func TestResultWithoutStartIsKept(t *testing.T) {
	// start event lost in a crash, result still arrives — the result counts.
	evs := []Event{
		evResult("s1", "t1", "a1", 1, StatusPassed),
		{Type: EvShardFinished, ShardID: "s1", Outcome: StatusCompleted},
		{Type: EvRunFinalized},
	}
	sum := fold(t, seed1(), evs).Snapshot()
	if got := testStatusOf(sum, "t1"); got != StatusPassed {
		t.Fatalf("got %q want passed (result without start is retained)", got)
	}
}

// Determinism: the same multiset of events must produce an identical summary
// regardless of arrival order. This is the headline reproducibility check.
// run_finalized is a closure event and is held fixed at the end (the Replay
// API does the same), so shuffling cannot "close the run" mid-batch.
func TestDeterministicAcrossShuffles(t *testing.T) {
	seed := []ShardSeed{
		{ShardID: "s1", TestIDs: []string{"t1", "t2"}},
		{ShardID: "s2", TestIDs: []string{"t3"}},
	}
	events := []Event{
		{Type: EvShardStarted, ShardID: "s1", EventID: "shard-s1-start"},
		evAttemptStarted("s1", "t1", "a1", 1),
		{Type: EvAttemptResult, ShardID: "s1", TestID: "t1", AttemptID: "a1", AttemptNo: 1, Result: StatusFailed, EventID: "a1-failed"},
		evAttemptStarted("s1", "t1", "a2", 2),
		evResult("s1", "t1", "a2", 2, StatusPassed),
		evAttemptStarted("s1", "t2", "b1", 1),
		{Type: EvShardStarted, ShardID: "s2", EventID: "shard-s2-start"},
		evAttemptStarted("s2", "t3", "c1", 1),
		evResult("s2", "t3", "c1", 1, StatusCanceled),
		// genuine redelivery: same event_id as the first a1 result.
		{Type: EvAttemptResult, ShardID: "s1", TestID: "t1", AttemptID: "a1", AttemptNo: 1, Result: StatusFailed, EventID: "a1-failed"},
		{Type: EvShardFinished, ShardID: "s1", Outcome: StatusCompleted, EventID: "s1-fin"},
		{Type: EvShardFinished, ShardID: "s2", Outcome: StatusCompleted, EventID: "s2-fin"},
	}
	// t2 started with no result + completed shard => incomplete after finalize

	finalEv := Event{Type: EvRunFinalized, EventID: "fin"}

	canonical := func(evs []Event) string {
		m := NewMerger("r", seed)
		seen := map[string]bool{}
		for _, e := range evs {
			if _, err := m.Apply(e, seen); err != nil {
				t.Fatal(err)
			}
		}
		b, _ := json.Marshal(m.Snapshot())
		return string(b)
	}

	inOrder := append(append([]Event{}, events...), finalEv)
	want := canonical(inOrder)

	for seedInt := int64(1); seedInt <= 50; seedInt++ {
		head := append([]Event{}, events...)
		shuffleEvents(head, seedInt)
		shuffled := append(head, finalEv)
		if got := canonical(shuffled); got != want {
			t.Fatalf("summary differs under shuffle seed %d\nwant: %s\ngot:  %s", seedInt, want, got)
		}
	}
}

func TestReplayRepeatedShufflesIdentical(t *testing.T) {
	seed := seed1()
	evs := []Event{
		evAttemptStarted("s1", "t1", "a1", 1),
		evResult("s1", "t1", "a1", 1, StatusFailed),
		evAttemptStarted("s1", "t1", "a2", 2),
		evResult("s1", "t1", "a2", 2, StatusPassed),
		{Type: EvShardFinished, ShardID: "s1", Outcome: StatusCompleted},
		{Type: EvRunFinalized},
	}
	var first string
	for i := int64(0); i < 10; i++ {
		sum, outcomes, err := Replay(seed, evs, true, i)
		if err != nil {
			t.Fatal(err)
		}
		b, _ := json.Marshal(sum)
		if i == 0 {
			first = string(b)
		} else if string(b) != first {
			t.Fatalf("replay %d not identical", i)
		}
		if sum.Status != StatusCompleted {
			t.Fatalf("replay %d status %s", i, sum.Status)
		}
		_ = outcomes
	}
}

func intPtr(i int) *int { return &i }

package merge

import (
	"math/rand"
	"testing"

	"github.com/local/testmerge/internal/domain"
)

func ev(id string, seq int64, typ domain.EventType, extra ...func(*domain.Event)) domain.Event {
	e := domain.Event{EventID: id, SeqNo: seq, Type: typ, RunID: "r1"}
	for _, f := range extra {
		f(&e)
	}
	return e
}

func shard(id string, seq int64, typ domain.EventType, name string) domain.Event {
	e := ev(id, seq, typ)
	e.Shard = name
	return e
}

func attStart(id string, seq int64, test, attempt string, no int, shardName ...string) domain.Event {
	e := ev(id, seq, domain.EventAttemptStarted)
	e.TestID, e.AttemptID, e.AttemptNo = test, attempt, no
	if len(shardName) > 0 {
		e.Shard = shardName[0]
	} else {
		e.Shard = "s"
	}
	return e
}

func attFinish(id string, seq int64, test, attempt string, no int, status domain.AttemptStatus, shardName ...string) domain.Event {
	e := ev(id, seq, domain.EventAttemptFinished)
	e.TestID, e.AttemptID, e.AttemptNo, e.Status = test, attempt, no, string(status)
	if len(shardName) > 0 {
		e.Shard = shardName[0]
	} else {
		e.Shard = "s"
	}
	return e
}

func runStart() domain.Event { return ev("rs", 0, domain.EventRunStarted) }
func runFinish(id string, seq int64, status domain.RunStatus, cancelled ...bool) domain.Event {
	e := ev(id, seq, domain.EventRunFinished)
	e.Status = string(status)
	if len(cancelled) > 0 {
		e.Cancelled = cancelled[0]
	}
	return e
}

func happyEvents() []domain.Event {
	return []domain.Event{
		runStart(),
		shard("s1", 1, domain.EventShardStarted, "a"),
		attStart("a1s", 2, "t-alpha", "a-1", 1, "a"),
		attFinish("a1f", 3, "t-alpha", "a-1", 1, domain.AttemptPassed, "a"),
		attStart("b1s", 4, "t-bravo", "b-1", 1, "a"),
		attFinish("b1f", 5, "t-bravo", "b-1", 1, domain.AttemptFailed, "a"),
		shard("s1e", 6, domain.EventShardFinished, "a"),
		shard("s2", 7, domain.EventShardStarted, "b"),
		attStart("c1s", 8, "t-charlie", "c-1", 1, "b"),
		attFinish("c1f", 9, "t-charlie", "c-1", 1, domain.AttemptPassed, "b"),
		shard("s2e", 10, domain.EventShardFinished, "b"),
		runFinish("rf1", 11, domain.RunFailed),
	}
}

func reduce(t *testing.T, events []domain.Event) *Run {
	t.Helper()
	r, res, err := Reduce("r1", events)
	if err != nil {
		t.Fatalf("Reduce: %v", err)
	}
	if res.Unique != len(events) {
		t.Fatalf("Unique=%d, want %d", res.Unique, len(events))
	}
	return r
}

func TestHappyPathCounts(t *testing.T) {
	sum := reduce(t, happyEvents()).Summarize()
	if sum.Status != domain.RunFailed {
		t.Fatalf("run status=%s, want failed", sum.Status)
	}
	if sum.Counts != (Counts{Passed: 2, Failed: 1}) {
		t.Fatalf("counts=%+v, want 2 passed/1 failed", sum.Counts)
	}
	if sum.Total != 3 || sum.MissingFinish {
		t.Fatalf("total=%d missingFinish=%v", sum.Total, sum.MissingFinish)
	}
	if sum.LateWrites != 0 {
		t.Fatalf("late writes=%d, want 0", sum.LateWrites)
	}
}

// The central acceptance property: shuffle arrival order arbitrarily,
// duplicate events, and the summary must stay byte-identical.
func TestReorderAndDuplicateInvariance(t *testing.T) {
	base := happyEvents()
	want := reduce(t, base).Summarize()

	rng := rand.New(rand.NewSource(42))
	for iter := 0; iter < 20; iter++ {
		shuffled := append([]domain.Event(nil), base...)
		rng.Shuffle(len(shuffled), func(i, j int) { shuffled[i], shuffled[j] = shuffled[j], shuffled[i] })
		// Sprinkle duplicates of random events.
		for k := 0; k < 5; k++ {
			shuffled = append(shuffled, base[rng.Intn(len(base))])
		}
		r, res, err := Reduce("r1", shuffled)
		if err != nil {
			t.Fatalf("iter %d: %v", iter, err)
		}
		if res.Duplicate != 5 {
			t.Fatalf("iter %d duplicates=%d, want 5", iter, res.Duplicate)
		}
		got := r.Summarize()
		if !summariesEqual(got, want) {
			t.Fatalf("iter %d: summary differs after shuffle/dup\n got=%+v\nwant=%+v", iter, got, want)
		}
	}
}

func TestRetryLatestAttemptWins(t *testing.T) {
	events := []domain.Event{
		runStart(),
		shard("s1", 1, domain.EventShardStarted, "a"),
		attStart("r1s", 2, "t-flaky", "f-1", 1, "a"),
		attFinish("r1f", 3, "t-flaky", "f-1", 1, domain.AttemptFailed, "a"),
		attStart("r2s", 4, "t-flaky", "f-2", 2, "a"),
		attFinish("r2f", 5, "t-flaky", "f-2", 2, domain.AttemptPassed, "a"),
		shard("s1e", 6, domain.EventShardFinished, "a"),
		runFinish("rf1", 7, domain.RunPassed),
	}
	sum := reduce(t, events).Summarize()
	if sum.Status != domain.RunPassed {
		t.Fatalf("status=%s, want passed (retry succeeded)", sum.Status)
	}
	if sum.Counts.Passed != 1 || sum.Counts.Failed != 0 {
		t.Fatalf("counts=%+v", sum.Counts)
	}
	ts := findTest(sum.Tests, "t-flaky")
	if ts.LatestAttemptID != "f-2" || ts.Status != domain.TestPassed {
		t.Fatalf("latest attempt resolution wrong: %+v", ts)
	}
}

// A late, *different* result event targeting the OLD attempt must neither
// overwrite the old attempt's first terminal status nor affect the test
// outcome driven by the newer attempt.
func TestLateResultDoesNotOverwrite(t *testing.T) {
	events := []domain.Event{
		runStart(),
		shard("s1", 1, domain.EventShardStarted, "a"),
		attStart("l1s", 2, "t-late", "t-1", 1, "a"),
		attFinish("l1f", 3, "t-late", "t-1", 1, domain.AttemptFailed, "a"),
		attStart("l2s", 4, "t-late", "t-2", 2, "a"),
		attFinish("l2f", 5, "t-late", "t-2", 2, domain.AttemptPassed, "a"),
		attFinish("l1-late", 6, "t-late", "t-1", 1, domain.AttemptCancelled, "a"),
		shard("s1e", 7, domain.EventShardFinished, "a"),
		runFinish("rf1", 8, domain.RunPassed),
	}
	sum := reduce(t, events).Summarize()
	if sum.Status != domain.RunPassed {
		t.Fatalf("status=%s, want passed", sum.Status)
	}
	ts := findTest(sum.Tests, "t-late")
	if ts.Status != domain.TestPassed || ts.LatestAttemptID != "t-2" {
		t.Fatalf("test=%+v", ts)
	}
	old := findAttempt(ts.Attempts, "t-1")
	if old.Status != string(domain.AttemptFailed) {
		t.Fatalf("old attempt overwritten to %s; first terminal result (failed) must win", old.Status)
	}
	if old.LateWrites != 1 {
		t.Fatalf("late_writes=%d, want 1 (ignored late cancel counted)", old.LateWrites)
	}
	if sum.LateWrites != 1 {
		t.Fatalf("run late_writes=%d, want 1", sum.LateWrites)
	}
}

// Executor crash mid-run: no run_finished and an unfinished attempt. The run
// must be incomplete even though every *finished* result passed.
func TestExecutorCrashIsIncomplete(t *testing.T) {
	events := []domain.Event{
		runStart(),
		shard("s1", 1, domain.EventShardStarted, "a"),
		attStart("x1s", 2, "t-alpha", "a-1", 1, "a"),
		attFinish("x1f", 3, "t-alpha", "a-1", 1, domain.AttemptPassed, "a"),
		attStart("x2s", 4, "t-delta", "d-1", 1, "a"),
	}
	sum := reduce(t, events).Summarize()
	if sum.Status != domain.RunIncomplete {
		t.Fatalf("status=%s, want incomplete after crash", sum.Status)
	}
	if !sum.MissingFinish {
		t.Fatal("missing_finish should be true")
	}
	if sum.Counts != (Counts{Passed: 1, Incomplete: 1}) {
		t.Fatalf("counts=%+v, want 1 passed/1 incomplete", sum.Counts)
	}
	delta := findTest(sum.Tests, "t-delta")
	if delta.Status != domain.TestIncomplete || delta.Reason == "" {
		t.Fatalf("delta=%+v, want incomplete with reason", delta)
	}
}

// A run without run_started but with results is also incomplete (never
// silently passed).
func TestNoRunStartedIsIncomplete(t *testing.T) {
	events := []domain.Event{
		shard("s1", 1, domain.EventShardStarted, "a"),
		attStart("a1s", 2, "t-alpha", "a-1", 1, "a"),
		attFinish("a1f", 3, "t-alpha", "a-1", 1, domain.AttemptPassed, "a"),
	}
	sum := reduce(t, events).Summarize()
	if sum.Status != domain.RunIncomplete {
		t.Fatalf("status=%s, want incomplete", sum.Status)
	}
}

func TestCancellationStatuses(t *testing.T) {
	events := []domain.Event{
		runStart(),
		shard("s1", 1, domain.EventShardStarted, "a"),
		attStart("k1s", 2, "t-keep", "k-1", 1, "a"),
		attFinish("k1f", 3, "t-keep", "k-1", 1, domain.AttemptPassed, "a"),
		attStart("z1s", 4, "t-zap", "z-1", 1, "a"),
		runFinish("rf1", 5, domain.RunCancelled, true),
		shard("s1e", 6, domain.EventShardFinished, "a"),
	}
	sum := reduce(t, events).Summarize()
	if sum.Status != domain.RunCancelled {
		t.Fatalf("status=%s, want cancelled", sum.Status)
	}
	if !sum.Cancelled {
		t.Fatal("cancelled flag not set")
	}
	zap := findTest(sum.Tests, "t-zap")
	if zap.Status != domain.TestCancelled {
		t.Fatalf("t-zap=%s, want cancelled (unfinished at cancellation)", zap.Status)
	}
	keep := findTest(sum.Tests, "t-keep")
	if keep.Status != domain.TestPassed {
		t.Fatalf("t-keep=%s, already-passed tests stay passed", keep.Status)
	}
}

// Explicit acceptance: a missing result is never a pass.
func TestMissingResultsAreNotPassed(t *testing.T) {
	rs := runStart()
	rs.Tests = []string{"t-maybe", "t-ghost"}
	events := []domain.Event{
		rs,
		shard("s1", 1, domain.EventShardStarted, "a"),
		attStart("m1s", 2, "t-maybe", "m-1", 1, "a"),
		shard("s1e", 3, domain.EventShardFinished, "a"),
		runFinish("rf1", 4, domain.RunIncomplete),
	}
	sum := reduce(t, events).Summarize()
	if sum.Counts.Passed != 0 {
		t.Fatalf("passed=%d, must be 0 when results are missing", sum.Counts.Passed)
	}
	if sum.Counts.Incomplete != 2 {
		t.Fatalf("incomplete=%d, want 2 (one started-unfinished, one never observed)", sum.Counts.Incomplete)
	}
	if sum.Status != domain.RunIncomplete {
		t.Fatalf("status=%s, want incomplete", sum.Status)
	}
	ghost := findTest(sum.Tests, "t-ghost")
	if ghost.TestID != "t-ghost" || ghost.Status != domain.TestIncomplete {
		t.Fatalf("declared-but-observed test must be incomplete: %+v", ghost)
	}
}

// Cancelled attempt status must be distinct from failed/incomplete/passed.
func TestExplicitCancelledAttempt(t *testing.T) {
	events := []domain.Event{
		runStart(),
		shard("s1", 1, domain.EventShardStarted, "a"),
		attStart("q1s", 2, "t-q", "q-1", 1, "a"),
		attFinish("q1f", 3, "t-q", "q-1", 1, domain.AttemptCancelled, "a"),
		shard("s1e", 4, domain.EventShardFinished, "a"),
		runFinish("rf1", 5, domain.RunCancelled, true),
	}
	sum := reduce(t, events).Summarize()
	if sum.Counts.Cancelled != 1 || sum.Counts.Failed != 0 || sum.Counts.Passed != 0 {
		t.Fatalf("counts=%+v, want exactly 1 cancelled", sum.Counts)
	}
}

func TestEmptyRunIsIncompleteNotPassed(t *testing.T) {
	sum := reduce(t, []domain.Event{runStart()}).Summarize()
	if sum.Status != domain.RunIncomplete {
		t.Fatalf("empty run status=%s, want incomplete", sum.Status)
	}
}

func TestDuplicateEventIDAppliedOnce(t *testing.T) {
	events := append(happyEvents(), happyEvents()...)
	r, res, err := Reduce("r1", events)
	if err != nil {
		t.Fatal(err)
	}
	if res.Unique != len(happyEvents()) || res.Duplicate != len(happyEvents()) {
		t.Fatalf("unique=%d dup=%d", res.Unique, res.Duplicate)
	}
	if r.Summarize().EventsApplied != len(happyEvents()) {
		t.Fatalf("events_applied=%d", r.Summarize().EventsApplied)
	}
}

func TestSeqCollisionRejected(t *testing.T) {
	bad := []domain.Event{
		runStart(),
		shard("s1", 1, domain.EventShardStarted, "a"),
		shard("s2", 1, domain.EventShardStarted, "b"), // same seq, different id
	}
	if _, _, err := Reduce("r1", bad); err == nil {
		t.Fatal("expected seq_no collision error")
	}
}

func TestValidationRejectsBadEvents(t *testing.T) {
	cases := map[string]domain.Event{
		"missing event id":   {SeqNo: 1, Type: domain.EventShardStarted, RunID: "r1", Shard: "a"},
		"missing run id":     {EventID: "e", SeqNo: 1, Type: domain.EventShardStarted, Shard: "a"},
		"unknown type":       {EventID: "e", SeqNo: 1, Type: "bogus", RunID: "r1"},
		"bad attempt status": attFinish("e", 1, "t", "a", 1, "weird"),
		"finish missing ids": {EventID: "e", SeqNo: 1, Type: domain.EventAttemptFinished, RunID: "r1", Shard: "a", Status: "passed"},
		"finish zero seq":    attFinish("e", 0, "t", "a", 1, domain.AttemptPassed),
	}
	for name, e := range cases {
		t.Run(name, func(t *testing.T) {
			if err := Validate(e); err == nil {
				t.Fatal("expected validation error")
			}
		})
	}
}

func TestForeignRunRejected(t *testing.T) {
	e := attFinish("e", 1, "t", "a", 1, domain.AttemptPassed)
	e.RunID = "other"
	if _, _, err := Reduce("r1", []domain.Event{e}); err == nil {
		t.Fatal("expected foreign-run error")
	}
}

// Orphan finish: a finish event with no start (start lost in a crash). The
// result still counts, but is flagged for auditing.
func TestOrphanFinishFlagged(t *testing.T) {
	events := []domain.Event{
		runStart(),
		shard("s1", 1, domain.EventShardStarted, "a"),
		attFinish("a1f", 2, "t-alpha", "a-1", 1, domain.AttemptPassed, "a"),
		shard("s1e", 3, domain.EventShardFinished, "a"),
		runFinish("rf1", 4, domain.RunPassed),
	}
	sum := reduce(t, events).Summarize()
	ts := findTest(sum.Tests, "t-alpha")
	a := findAttempt(ts.Attempts, "a-1")
	if !a.OrphanFinish || !a.Finished || a.Started {
		t.Fatalf("attempt=%+v, want orphan finish flagged", a)
	}
}

func TestSummaryRepeatableAcrossReReduces(t *testing.T) {
	first := reduce(t, happyEvents()).Summarize()
	for i := 0; i < 5; i++ {
		again := reduce(t, happyEvents()).Summarize()
		if !summariesEqual(again, first) {
			t.Fatal("summary not deterministic across reductions")
		}
	}
}

func findTest(tests []TestSummary, id string) TestSummary {
	for _, ts := range tests {
		if ts.TestID == id {
			return ts
		}
	}
	return TestSummary{}
}

func findAttempt(as []AttemptSummary, id string) AttemptSummary {
	for _, a := range as {
		if a.AttemptID == id {
			return a
		}
	}
	return AttemptSummary{}
}

func summariesEqual(a, b RunSummary) bool {
	if a.RunID != b.RunID || a.Status != b.Status || a.Counts != b.Counts ||
		a.Total != b.Total || a.LateWrites != b.LateWrites ||
		a.MissingFinish != b.MissingFinish || a.Cancelled != b.Cancelled ||
		len(a.Tests) != len(b.Tests) || len(a.Shards) != len(b.Shards) {
		return false
	}
	for i := range a.Tests {
		x, y := a.Tests[i], b.Tests[i]
		if x.TestID != y.TestID || x.Status != y.Status || x.LatestAttemptID != y.LatestAttemptID ||
			len(x.Attempts) != len(y.Attempts) {
			return false
		}
		for j := range x.Attempts {
			p, q := x.Attempts[j], y.Attempts[j]
			if p.AttemptID != q.AttemptID || p.Status != q.Status || p.Latest != q.Latest ||
				p.LateWrites != q.LateWrites || p.OrphanFinish != q.OrphanFinish {
				return false
			}
		}
	}
	return true
}

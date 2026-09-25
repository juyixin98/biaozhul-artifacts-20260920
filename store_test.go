package trmerge

import (
	"path/filepath"
	"strings"
	"testing"
)

func TestCreateAndIngestSummary(t *testing.T) {
	dir := t.TempDir()
	st, err := NewStore(filepath.Join(dir, "cache"))
	if err != nil {
		t.Fatal(err)
	}
	seed := []ShardSeed{{ShardID: "s1", TestIDs: []string{"t1"}}}
	meta, err := st.CreateRun("events", seed, "", false)
	if err != nil {
		t.Fatal(err)
	}
	out, err := st.Ingest(meta.ID, []Event{
		evAttemptStarted("s1", "t1", "a1", 1),
		evResult("s1", "t1", "a1", 1, StatusPassed),
		{Type: EvShardFinished, ShardID: "s1", Outcome: StatusCompleted},
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, o := range out {
		if o.Error != "" {
			t.Fatalf("ingest error: %s", o.Error)
		}
	}
	if _, err := st.Finalize(meta.ID); err != nil {
		t.Fatal(err)
	}
	sum, err := st.Summary(meta.ID)
	if err != nil {
		t.Fatal(err)
	}
	if sum.Status != StatusCompleted || sum.Counts.Passed != 1 {
		t.Fatalf("sum = %+v", sum)
	}
}

func TestRecoveryRebuildsFromLog(t *testing.T) {
	dir := t.TempDir()
	cache := filepath.Join(dir, "cache")
	seed := []ShardSeed{{ShardID: "s1", TestIDs: []string{"t1", "t2"}}}

	var runID string
	func() {
		st, err := NewStore(cache)
		if err != nil {
			t.Fatal(err)
		}
		meta, err := st.CreateRun("events", seed, "", false)
		if err != nil {
			t.Fatal(err)
		}
		runID = meta.ID
		if _, err := st.Ingest(runID, []Event{
			evAttemptStarted("s1", "t1", "a1", 1),
			evResult("s1", "t1", "a1", 1, StatusPassed),
			evAttemptStarted("s1", "t2", "b1", 1), // no result
		}); err != nil {
			t.Fatal(err)
		}
		if _, err := st.Finalize(runID); err != nil {
			t.Fatal(err)
		}
	}()

	// Simulate process restart: a brand new store over the same cache.
	st2, err := NewStore(cache)
	if err != nil {
		t.Fatal(err)
	}
	sum, err := st2.Summary(runID)
	if err != nil {
		t.Fatal(err)
	}
	if testStatusOf(*sum, "t1") != StatusPassed {
		t.Fatalf("t1 = %s", testStatusOf(*sum, "t1"))
	}
	if testStatusOf(*sum, "t2") != StatusIncomplete {
		t.Fatalf("t2 = %s after recovery, want incomplete", testStatusOf(*sum, "t2"))
	}
	if !sum.Finalized || sum.Status != StatusFailed {
		t.Fatalf("finalized/run status wrong: %+v", sum)
	}

	// Event log is readable and includes the synthetic run_started + finalize.
	envs, err := st2.ReadEvents(runID)
	if err != nil {
		t.Fatal(err)
	}
	var types []string
	for _, e := range envs {
		types = append(types, e.Event.Type)
	}
	joined := strings.Join(types, ",")
	if !strings.Contains(joined, EvRunStarted) || !strings.Contains(joined, EvRunFinalized) {
		t.Fatalf("log missing lifecycle events: %s", joined)
	}
}

func TestFinalizeIdempotent(t *testing.T) {
	dir := t.TempDir()
	st, _ := NewStore(filepath.Join(dir, "cache"))
	meta, _ := st.CreateRun("events", nil, "", false)
	changed1, err := st.Finalize(meta.ID)
	if err != nil || !changed1 {
		t.Fatalf("first finalize: %v %v", changed1, err)
	}
	changed2, _ := st.Finalize(meta.ID)
	if changed2 {
		t.Fatal("second finalize should be a no-op")
	}
}

func TestCancelIdempotent(t *testing.T) {
	dir := t.TempDir()
	st, _ := NewStore(filepath.Join(dir, "cache"))
	meta, _ := st.CreateRun("events", nil, "", false)
	c1, err := st.RequestCancel(meta.ID)
	if err != nil || !c1 {
		t.Fatalf("first cancel applied=%v err=%v", c1, err)
	}
	c2, _ := st.RequestCancel(meta.ID)
	if c2 {
		t.Fatal("second cancel must be a no-op")
	}
	envs, _ := st.ReadEvents(meta.ID)
	var cancelEvents int
	for _, e := range envs {
		if e.Event.Type == EvRunCancelRequested {
			cancelEvents++
		}
	}
	if cancelEvents != 1 {
		t.Fatalf("want exactly 1 cancel event in log, got %d", cancelEvents)
	}
}

func TestEventIDDeduplicationAcrossIngest(t *testing.T) {
	dir := t.TempDir()
	st, _ := NewStore(filepath.Join(dir, "cache"))
	meta, _ := st.CreateRun("events", seed1(), "", false)
	ev := func() []Event {
		return []Event{{
			Type: EvAttemptResult, ShardID: "s1", TestID: "t1",
			AttemptID: "a1", AttemptNo: 1, Result: StatusPassed, EventID: "fixed-id",
		}}
	}
	o1, _ := st.Ingest(meta.ID, ev())
	o2, _ := st.Ingest(meta.ID, ev())
	if !o1[0].Applied || !o2[0].Duplicate {
		t.Fatalf("dedupe failed: %+v %+v", o1[0], o2[0])
	}
}

func TestCacheWorkSeparation(t *testing.T) {
	dir := t.TempDir()
	cache := filepath.Join(dir, "cache")
	st, err := NewStore(cache)
	if err != nil {
		t.Fatal(err)
	}

	// Either directory containing the other (or identical) is refused;
	// a genuinely separate directory is allowed.
	cases := []struct {
		work string
		want bool // expect error
	}{
		{cache, true},                     // identical
		{filepath.Join(cache, "x"), true}, // work inside cache
		{filepath.Dir(cache), true},       // work is parent of cache
		{filepath.Join(dir, "work"), false},
		{filepath.Join(dir, "a", "b", "c"), false},
	}
	for i, c := range cases {
		err := st.EnsureSeparateFromWork(c.work)
		if c.want && err == nil {
			t.Errorf("case %d: expected rejection for work=%s", i, c.work)
		}
		if !c.want && err != nil {
			t.Errorf("case %d: unexpected error: %v", i, err)
		}
	}
}

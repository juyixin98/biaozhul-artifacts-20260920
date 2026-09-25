package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/local/testmerge/internal/domain"
	"github.com/local/testmerge/internal/executor"
	"github.com/local/testmerge/internal/merge"
	"github.com/local/testmerge/internal/store"
)

type testEnv struct {
	router http.Handler
	st     *store.Store
}

func newTestEnv(t *testing.T) *testEnv {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	fixtures, _ := filepath.Abs(filepath.Join(filepath.Dir(file), "..", "..", "testdata", "fixtures"))
	cache := t.TempDir()
	work := t.TempDir()
	// Double-check cache and work dirs are distinct.
	if cache == work {
		t.Fatal("cache and work dir unexpectedly equal")
	}
	st, err := store.Open(cache)
	if err != nil {
		t.Fatal(err)
	}
	ex, err := executor.New(fixtures, work)
	if err != nil {
		t.Fatal(err)
	}
	srv := &Server{Store: st, Executor: ex}
	return &testEnv{router: srv.NewRouter(), st: st}
}

func (e *testEnv) do(t *testing.T, method, path string, body any) *httptest.ResponseRecorder {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	rr := httptest.NewRecorder()
	e.router.ServeHTTP(rr, req)
	return rr
}

func mustStatus(t *testing.T, rr *httptest.ResponseRecorder, want int) {
	t.Helper()
	if rr.Code != want {
		t.Fatalf("status=%d want %d body=%s", rr.Code, want, rr.Body.String())
	}
}

func decode(t *testing.T, rr *httptest.ResponseRecorder, v any) {
	t.Helper()
	if err := json.Unmarshal(rr.Body.Bytes(), v); err != nil {
		t.Fatalf("decode %s: %v", rr.Body.String(), err)
	}
}

func createRun(t *testing.T, e *testEnv, id string, tests []string) {
	t.Helper()
	rr := e.do(t, http.MethodPost, "/runs", map[string]any{"run_id": id, "tests": tests})
	mustStatus(t, rr, http.StatusCreated)
}

func summary(t *testing.T, e *testEnv, id string) merge.RunSummary {
	t.Helper()
	rr := e.do(t, http.MethodGet, "/runs/"+id, nil)
	mustStatus(t, rr, http.StatusOK)
	var s merge.RunSummary
	decode(t, rr, &s)
	return s
}

func TestHealth(t *testing.T) {
	e := newTestEnv(t)
	rr := e.do(t, http.MethodGet, "/healthz", nil)
	mustStatus(t, rr, http.StatusOK)
}

func TestCreateAndGetRun(t *testing.T) {
	e := newTestEnv(t)
	createRun(t, e, "run-xyz", nil)
	// Duplicate create -> 409.
	rr := e.do(t, http.MethodPost, "/runs", map[string]any{"run_id": "run-xyz"})
	mustStatus(t, rr, http.StatusConflict)
	// Listing includes it.
	rr = e.do(t, http.MethodGet, "/runs", nil)
	mustStatus(t, rr, http.StatusOK)
	var listed struct {
		Runs []struct {
			RunID string `json:"run_id"`
		} `json:"runs"`
	}
	decode(t, rr, &listed)
	if len(listed.Runs) != 1 || listed.Runs[0].RunID != "run-xyz" {
		t.Fatalf("listing=%+v", listed)
	}
}

// Full acceptance scenario through HTTP: execute shuffled events, then
// replay the same batch again (duplicates), summary must be stable and equal
// to an in-order ingestion in a separate run.
func TestExecuteShuffledThenReplayIsStable(t *testing.T) {
	e := newTestEnv(t)
	createRun(t, e, "shuffled", nil)
	rr := e.do(t, http.MethodPost, "/runs/shuffled/execute", map[string]any{
		"command": []string{"gen_events.sh", "shuffled"},
	})
	mustStatus(t, rr, http.StatusOK)
	var first struct {
		Ingested int              `json:"ingested"`
		Summary  merge.RunSummary `json:"summary"`
	}
	decode(t, rr, &first)
	if first.Ingested != 11 {
		t.Fatalf("ingested=%d, want 11", first.Ingested)
	}
	if first.Summary.Status != domain.RunFailed {
		t.Fatalf("status=%s, want failed (arrival order must not matter)", first.Summary.Status)
	}
	if first.Summary.Counts.Passed != 2 || first.Summary.Counts.Failed != 1 {
		t.Fatalf("counts=%+v", first.Summary.Counts)
	}

	// Fetch the raw events and replay the whole batch twice; every replay
	// must only produce duplicates and leave the summary identical.
	rr = e.do(t, http.MethodGet, "/runs/shuffled/events", nil)
	mustStatus(t, rr, http.StatusOK)
	var evWrap struct {
		Events []domain.Event `json:"events"`
	}
	decode(t, rr, &evWrap)
	// Strip server-emitted run_started so replay endpoint accepts them.
	var replayable []domain.Event
	for _, ev := range evWrap.Events {
		if ev.Type != domain.EventRunStarted {
			replayable = append(replayable, ev)
		}
	}
	for i := 0; i < 2; i++ {
		rr = e.do(t, http.MethodPost, "/runs/shuffled/replay", map[string]any{"events": replayable})
		mustStatus(t, rr, http.StatusOK)
		var res struct {
			Duplicates int              `json:"duplicates"`
			Accepted   int              `json:"accepted"`
			Summary    merge.RunSummary `json:"summary"`
		}
		decode(t, rr, &res)
		if res.Accepted != 0 || res.Duplicates != len(replayable) {
			t.Fatalf("replay %d: accepted=%d duplicates=%d want 0/%d", i, res.Accepted, res.Duplicates, len(replayable))
		}
		if res.Summary.Counts != first.Summary.Counts || res.Summary.Status != first.Summary.Status {
			t.Fatalf("summary drifted after replay %d", i)
		}
	}

	// Compare against an in-order "happy" run: same multiset semantics.
	createRun(t, e, "ordered", nil)
	rr = e.do(t, http.MethodPost, "/runs/ordered/execute", map[string]any{
		"command": []string{"gen_events.sh", "happy"},
	})
	mustStatus(t, rr, http.StatusOK)
	var ord struct {
		Summary merge.RunSummary `json:"summary"`
	}
	decode(t, rr, &ord)
	if ord.Summary.Counts != first.Summary.Counts || ord.Summary.Status != first.Summary.Status {
		t.Fatalf("shuffled vs in-order summaries differ: %+v vs %+v", first.Summary.Counts, ord.Summary.Counts)
	}
}

// Executor crash: execute the crashing fixture (no run_finished), then a
// retry fixture re-runs the missing test and finishes the run.
func TestCrashThenRetryCompletesRun(t *testing.T) {
	e := newTestEnv(t)
	createRun(t, e, "crashrun", nil)

	rr := e.do(t, http.MethodPost, "/runs/crashrun/execute", map[string]any{
		"command":      []string{"gen_events.sh", "crash"},
		"execution_id": "exec-attempt-1",
	})
	mustStatus(t, rr, http.StatusOK)
	var crash struct {
		Record struct {
			ExitCode int  `json:"exit_code"`
			TimedOut bool `json:"timed_out"`
		} `json:"record"`
		Summary merge.RunSummary `json:"summary"`
	}
	decode(t, rr, &crash)
	if crash.Record.ExitCode != 91 {
		t.Fatalf("crash exit=%d want 91", crash.Record.ExitCode)
	}
	if crash.Summary.Status != domain.RunIncomplete || !crash.Summary.MissingFinish {
		t.Fatalf("after crash status=%s missing=%v, want incomplete", crash.Summary.Status, crash.Summary.MissingFinish)
	}
	if crash.Summary.Counts.Passed != 1 || crash.Summary.Counts.Incomplete != 1 {
		t.Fatalf("counts=%+v want 1 passed/1 incomplete", crash.Summary.Counts)
	}

	// Re-running the SAME execution (same execution_id) and crashing again
	// must not duplicate any events.
	rr = e.do(t, http.MethodPost, "/runs/crashrun/execute", map[string]any{
		"command":      []string{"gen_events.sh", "crash"},
		"execution_id": "exec-attempt-1",
	})
	mustStatus(t, rr, http.StatusOK)
	var again struct {
		Duplicates int              `json:"duplicates"`
		Ingested   int              `json:"ingested"`
		Summary    merge.RunSummary `json:"summary"`
	}
	decode(t, rr, &again)
	if again.Ingested != 0 || again.Duplicates != 4 {
		t.Fatalf("crash retry: ingested=%d duplicates=%d want 0/4", again.Ingested, again.Duplicates)
	}
	if again.Summary.Counts != crash.Summary.Counts {
		t.Fatalf("duplicate crash replay changed counts: %+v vs %+v", again.Summary.Counts, crash.Summary.Counts)
	}

	// Retry executor sends a completion batch for the unfinished test plus
	// run_finished via the replay endpoint (fresh event ids).
	retryEvents := []map[string]any{
		{"event_id": "d1f", "seq_no": 5, "type": "attempt_finished", "shard": "shard-a",
			"test_id": "test-delta", "attempt_id": "d-2", "attempt_no": 2, "status": "passed"},
		{"event_id": "rf2", "seq_no": 6, "type": "run_finished", "status": "passed"},
	}
	rr = e.do(t, http.MethodPost, "/runs/crashrun/replay", map[string]any{"events": retryEvents})
	mustStatus(t, rr, http.StatusOK)
	var fixed struct {
		Summary merge.RunSummary `json:"summary"`
	}
	decode(t, rr, &fixed)
	if fixed.Summary.Status != domain.RunPassed {
		t.Fatalf("after retry status=%s, want passed", fixed.Summary.Status)
	}
	if fixed.Summary.Counts.Incomplete != 0 || fixed.Summary.Counts.Passed != 2 {
		t.Fatalf("counts=%+v want 2 passed/0 incomplete", fixed.Summary.Counts)
	}
	if fixed.Summary.MissingFinish {
		t.Fatal("run_finished present, missing_finish must be false")
	}
}

func TestLateWriteScenarioViaFixture(t *testing.T) {
	e := newTestEnv(t)
	createRun(t, e, "late", nil)
	rr := e.do(t, http.MethodPost, "/runs/late/execute", map[string]any{
		"command": []string{"gen_events.sh", "late-write"},
	})
	mustStatus(t, rr, http.StatusOK)
	sum := summary(t, e, "late")
	if sum.Status != domain.RunPassed {
		t.Fatalf("status=%s want passed", sum.Status)
	}
	if sum.LateWrites != 1 {
		t.Fatalf("late_writes=%d want 1", sum.LateWrites)
	}
	var lateTest merge.TestSummary
	for _, ts := range sum.Tests {
		if ts.TestID == "test-late" {
			lateTest = ts
		}
	}
	if lateTest.LatestAttemptID != "t-2" || lateTest.Status != domain.TestPassed {
		t.Fatalf("latest attempt wrong: %+v", lateTest)
	}
	for _, a := range lateTest.Attempts {
		if a.AttemptID == "t-1" && a.Status != "failed" {
			t.Fatalf("old attempt overwritten to %s; must keep first result failed", a.Status)
		}
	}
}

func TestMissingScenarioNeverPassed(t *testing.T) {
	e := newTestEnv(t)
	// Declare both test-maybe (will start, no result) and test-ghost
	// (never observed at all).
	createRun(t, e, "miss", []string{"test-maybe", "test-ghost"})
	rr := e.do(t, http.MethodPost, "/runs/miss/execute", map[string]any{
		"command": []string{"gen_events.sh", "missing"},
	})
	mustStatus(t, rr, http.StatusOK)
	sum := summary(t, e, "miss")
	if sum.Status != domain.RunIncomplete {
		t.Fatalf("status=%s, incomplete required when results missing", sum.Status)
	}
	if sum.Counts.Passed != 0 || sum.Counts.Incomplete != 2 {
		t.Fatalf("counts=%+v, want 0 passed / 2 incomplete", sum.Counts)
	}
}

func TestCancelledScenario(t *testing.T) {
	e := newTestEnv(t)
	createRun(t, e, "cancel", nil)
	rr := e.do(t, http.MethodPost, "/runs/cancel/execute", map[string]any{
		"command": []string{"gen_events.sh", "cancelled"},
	})
	mustStatus(t, rr, http.StatusOK)
	sum := summary(t, e, "cancel")
	if sum.Status != domain.RunCancelled || !sum.Cancelled {
		t.Fatalf("status=%s cancelled=%v", sum.Status, sum.Cancelled)
	}
	if sum.Counts.Passed != 1 || sum.Counts.Cancelled != 1 {
		t.Fatalf("counts=%+v want 1 passed/1 cancelled", sum.Counts)
	}
}

func TestDuplicatesFixtureFullyDeduped(t *testing.T) {
	e := newTestEnv(t)
	createRun(t, e, "dup", nil)
	rr := e.do(t, http.MethodPost, "/runs/dup/execute", map[string]any{
		"command": []string{"gen_events.sh", "duplicates"},
	})
	mustStatus(t, rr, http.StatusOK)
	var res struct {
		Ingested   int `json:"ingested"`
		Duplicates int `json:"duplicates"`
	}
	decode(t, rr, &res)
	if res.Ingested != 11 || res.Duplicates != 11 {
		t.Fatalf("ingested=%d duplicates=%d, want 11/11", res.Ingested, res.Duplicates)
	}
	sum := summary(t, e, "dup")
	if sum.EventsApplied != 12 { // 11 fixture events + 1 run_started
		t.Fatalf("events_applied=%d want 12", sum.EventsApplied)
	}
	if sum.Counts.Passed != 2 || sum.Counts.Failed != 1 {
		t.Fatalf("counts=%+v", sum.Counts)
	}
}

func TestMalformedFixturePartialIngest(t *testing.T) {
	e := newTestEnv(t)
	createRun(t, e, "mal", nil)
	rr := e.do(t, http.MethodPost, "/runs/mal/execute", map[string]any{
		"command": []string{"gen_events.sh", "malformed"},
	})
	mustStatus(t, rr, http.StatusOK)
	var res struct {
		Ingested int `json:"ingested"`
		Rejected []struct {
			Line int `json:"line"`
		} `json:"rejected"`
		Record struct {
			ParseErrors []struct {
				LineNo int `json:"line_no"`
			} `json:"parse_errors"`
		} `json:"record"`
	}
	decode(t, rr, &res)
	if len(res.Record.ParseErrors) != 2 {
		t.Fatalf("parse_errors=%d want 2", len(res.Record.ParseErrors))
	}
	if res.Ingested != 5 {
		t.Fatalf("ingested=%d want 5 valid events", res.Ingested)
	}
}

func TestManualEventAppendAndValidation(t *testing.T) {
	e := newTestEnv(t)
	createRun(t, e, "manual", nil)

	good := map[string]any{
		"event_id": "e1", "seq_no": 1, "type": "shard_started", "shard": "a",
	}
	rr := e.do(t, http.MethodPost, "/runs/manual/events", good)
	mustStatus(t, rr, http.StatusOK)

	// Same event id again -> 409 conflict.
	rr = e.do(t, http.MethodPost, "/runs/manual/events", good)
	if rr.Code != http.StatusConflict {
		t.Fatalf("dup status=%d want 409", rr.Code)
	}

	// Invalid status -> 400, not persisted.
	bad := map[string]any{
		"event_id": "e2", "seq_no": 2, "type": "attempt_finished", "shard": "a",
		"test_id": "t", "attempt_id": "a", "attempt_no": 1, "status": "bogus",
	}
	rr = e.do(t, http.MethodPost, "/runs/manual/events", bad)
	mustStatus(t, rr, http.StatusBadRequest)

	// run_started cannot be injected manually.
	rr = e.do(t, http.MethodPost, "/runs/manual/events", map[string]any{
		"event_id": "x", "seq_no": 99, "type": "run_started",
	})
	mustStatus(t, rr, http.StatusBadRequest)
}

func TestUnknownRun404(t *testing.T) {
	e := newTestEnv(t)
	for _, tc := range []struct{ method, path string }{
		{http.MethodGet, "/runs/nope"},
		{http.MethodGet, "/runs/nope/events"},
		{http.MethodGet, "/runs/nope/executions"},
	} {
		rr := e.do(t, http.MethodPost, tc.path, nil) // method irrelevant for path check below
		_ = rr
		rr = e.do(t, tc.method, tc.path, nil)
		if rr.Code != http.StatusNotFound {
			t.Fatalf("%s %s -> %d want 404", tc.method, tc.path, rr.Code)
		}
	}
}

func TestExecuteRejectsForeignAndMissingCommand(t *testing.T) {
	e := newTestEnv(t)
	createRun(t, e, "r", nil)
	// Missing command.
	rr := e.do(t, http.MethodPost, "/runs/r/execute", map[string]any{})
	mustStatus(t, rr, http.StatusBadRequest)
	// Fixture escaping root.
	rr = e.do(t, http.MethodPost, "/runs/r/execute", map[string]any{
		"command": []string{"../../../../bin/true"},
	})
	mustStatus(t, rr, http.StatusBadRequest)
}

func TestExecutionAuditRecorded(t *testing.T) {
	e := newTestEnv(t)
	createRun(t, e, "r", nil)
	rr := e.do(t, http.MethodPost, "/runs/r/execute", map[string]any{
		"command": []string{"gen_events.sh", "crash"},
	})
	mustStatus(t, rr, http.StatusOK)
	rr = e.do(t, http.MethodGet, "/runs/r/executions", nil)
	mustStatus(t, rr, http.StatusOK)
	var wrap struct {
		Count int `json:"count"`
	}
	decode(t, rr, &wrap)
	if wrap.Count != 1 {
		t.Fatalf("execution audit records=%d want 1", wrap.Count)
	}
}

// Guard: no fixture may write into the fixtures root itself (work/cache
// separation is a stated requirement).
func TestFixturesRootStaysClean(t *testing.T) {
	_, file, _, _ := runtime.Caller(0)
	root, _ := filepath.Abs(filepath.Join(filepath.Dir(file), "..", "..", "testdata", "fixtures"))
	before, err := os.ReadDir(root)
	if err != nil {
		t.Fatal(err)
	}
	e := newTestEnv(t)
	createRun(t, e, "r", nil)
	_ = e.do(t, http.MethodPost, "/runs/r/execute", map[string]any{
		"command": []string{"touch_marker.sh", "api"},
	})
	after, err := os.ReadDir(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(before) != len(after) {
		t.Fatalf("fixtures root changed during execution (%d -> %d files)", len(before), len(after))
	}
}

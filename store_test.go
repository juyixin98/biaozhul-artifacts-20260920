package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// newTestStore opens a store on a fresh WAL with a controllable clock.
func newTestStore(t *testing.T) (*Store, *time.Time) {
	t.Helper()
	clock := time.Date(2026, 9, 24, 0, 0, 0, 0, time.UTC)
	s, err := OpenStore(filepath.Join(t.TempDir(), "test.wal"))
	if err != nil {
		t.Fatalf("OpenStore: %v", err)
	}
	s.now = func() time.Time { return clock }
	s.leaseDur = 10 * time.Second
	t.Cleanup(func() { s.Close() })
	return s, &clock
}

func mustSubmit(t *testing.T, s *Store, id string) {
	t.Helper()
	if _, err := s.Submit(id, "payload", 3); err != nil {
		t.Fatalf("Submit: %v", err)
	}
}

// TestStaleWorkerRejectedAfterTimeout: worker A claims attempt 1, its lease
// expires, worker B claims attempt 2 — a result from attempt 1 must be
// rejected, and attempt 2 completes exactly once.
func TestStaleWorkerRejectedAfterTimeout(t *testing.T) {
	s, clock := newTestStore(t)
	mustSubmit(t, s, "t")

	a, err := s.Claim("t", "workerA")
	if err != nil || a.Attempt != 1 {
		t.Fatalf("claim A: %v attempt=%d", err, a.Attempt)
	}
	*clock = clock.Add(11 * time.Second) // past the lease
	if err := s.Sweep(*clock); err != nil {
		t.Fatalf("sweep: %v", err)
	}
	b, err := s.Claim("t", "workerB")
	if err != nil || b.Attempt != 2 {
		t.Fatalf("claim B: %v attempt=%d", err, b.Attempt)
	}
	if _, err := s.Complete("t", "workerA", 1, "stale"); err != ErrStaleAttempt {
		t.Fatalf("stale complete: got %v, want ErrStaleAttempt", err)
	}
	if _, err := s.Complete("t", "workerB", 2, "fresh"); err != nil {
		t.Fatalf("fresh complete: %v", err)
	}
	got, _ := s.Get("t")
	if got.State != StateSucceeded || got.Result != "fresh" {
		t.Fatalf("final: %+v", got)
	}
}

// TestCancelCompleteRace: cancel and complete fired concurrently at a
// running task — exactly one wins, the loser gets a conflict, and the final
// state is exactly the winner's terminal state. Run with -race.
func TestCancelCompleteRace(t *testing.T) {
	for i := 0; i < 200; i++ {
		s, _ := newTestStore(t)
		mustSubmit(t, s, "t")
		if _, err := s.Claim("t", "w"); err != nil {
			t.Fatalf("claim: %v", err)
		}
		var wg sync.WaitGroup
		var cancelErr, completeErr error
		wg.Add(2)
		go func() { defer wg.Done(); _, cancelErr = s.Cancel("t") }()
		go func() { defer wg.Done(); _, completeErr = s.Complete("t", "w", 1, "r") }()
		wg.Wait()

		wins := 0
		if cancelErr == nil {
			wins++
		}
		if completeErr == nil {
			wins++
		}
		if wins != 1 {
			t.Fatalf("iter %d: %d winners (cancel=%v complete=%v)", i, wins, cancelErr, completeErr)
		}
		got, _ := s.Get("t")
		want := StateCancelled
		if completeErr == nil {
			want = StateSucceeded
		}
		if got.State != want {
			t.Fatalf("iter %d: final state %s, want %s", i, got.State, want)
		}
	}
}

// TestRestartNoRegression: drive a task to a terminal state, "crash", reopen
// from the WAL, and check the state is identical and immutable.
func TestRestartNoRegression(t *testing.T) {
	dir := t.TempDir()
	wal := filepath.Join(dir, "test.wal")

	s1, err := OpenStore(wal)
	if err != nil {
		t.Fatal(err)
	}
	clock := time.Date(2026, 9, 24, 0, 0, 0, 0, time.UTC)
	s1.now = func() time.Time { return clock }

	if _, err := s1.Submit("done", "p", 3); err != nil {
		t.Fatal(err)
	}
	if _, err := s1.Submit("cancelled", "p", 3); err != nil {
		t.Fatal(err)
	}
	if _, err := s1.Submit("running", "p", 3); err != nil {
		t.Fatal(err)
	}
	if _, err := s1.Claim("done", "w"); err != nil {
		t.Fatal(err)
	}
	if _, err := s1.Complete("done", "w", 1, "ok"); err != nil {
		t.Fatal(err)
	}
	if _, err := s1.Cancel("cancelled"); err != nil {
		t.Fatal(err)
	}
	if _, err := s1.Claim("running", "w"); err != nil {
		t.Fatal(err)
	}
	before, _ := s1.List()
	s1.Close()

	// Restart.
	s2, err := OpenStore(wal)
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	s2.now = func() time.Time { return clock }
	after, _ := s2.List()

	if len(before) != len(after) {
		t.Fatalf("task count changed across restart: %d -> %d", len(before), len(after))
	}
	for i := range before {
		if *before[i] != *after[i] {
			t.Fatalf("task %s regressed across restart:\n before=%+v\n after=%+v",
				before[i].ID, before[i], after[i])
		}
	}

	// Terminal states must reject everything, still.
	if _, err := s2.Complete("done", "w", 1, "again"); err != ErrTerminal {
		t.Fatalf("complete after restart on SUCCEEDED: %v", err)
	}
	if _, err := s2.Cancel("done"); err != ErrTerminal {
		t.Fatalf("cancel after restart on SUCCEEDED: %v", err)
	}
	if _, err := s2.Claim("cancelled", "w"); err != ErrTerminal {
		t.Fatalf("claim after restart on CANCELLED: %v", err)
	}
	// The running task survives and can finish with the same attempt number.
	if _, err := s2.Complete("running", "w", 1, "late"); err != nil {
		t.Fatalf("complete after restart on RUNNING: %v", err)
	}
}

// raceOp is one operation in the interleaving space.
type raceOp struct {
	name string
	run  func(s *Store, clock *time.Time) error
}

// TestExhaustiveInterleavings enumerates every sequence (up to length 4) of
// the operations that race with each other — claim, heartbeat, complete,
// stale complete, cancel, timeout (sweep), fail — and after every prefix
// checks the invariants:
//
//   - the attempt number never decreases;
//   - once terminal, the state (and committed result) never changes;
//   - a stale-attempt complete never succeeds;
//   - after a restart (WAL replay) the state is identical, i.e. durable and
//     never regressed.
func TestExhaustiveInterleavings(t *testing.T) {
	ops := []raceOp{
		{"claim", func(s *Store, c *time.Time) error {
			_, err := s.Claim("t", "w1")
			return err
		}},
		{"heartbeat", func(s *Store, c *time.Time) error {
			cur, _ := s.Get("t")
			_, err := s.Heartbeat("t", "w1", cur.Attempt)
			return err
		}},
		{"complete", func(s *Store, c *time.Time) error {
			cur, _ := s.Get("t")
			_, err := s.Complete("t", "w1", cur.Attempt, "r")
			return err
		}},
		{"completeStale", func(s *Store, c *time.Time) error {
			cur, _ := s.Get("t")
			_, err := s.Complete("t", "w1", cur.Attempt-1, "stale")
			return err
		}},
		{"cancel", func(s *Store, c *time.Time) error {
			_, err := s.Cancel("t")
			return err
		}},
		{"timeout", func(s *Store, c *time.Time) error {
			*c = c.Add(11 * time.Second) // past the 10s lease
			return s.Sweep(*c)
		}},
		{"fail", func(s *Store, c *time.Time) error {
			cur, _ := s.Get("t")
			_, err := s.Fail("t", "w1", cur.Attempt)
			return err
		}},
	}

	const maxLen = 4
	var checked int

	var enumerate func(prefix []int)
	enumerate = func(prefix []int) {
		if len(prefix) > 0 {
			runSequence(t, ops, prefix)
			checked++
		}
		if len(prefix) == maxLen {
			return
		}
		for i := range ops {
			enumerate(append(prefix, i))
		}
	}
	enumerate(nil)
	t.Logf("checked %d interleavings (lengths 1..%d over %d ops)", checked, maxLen, len(ops))
}

func runSequence(t *testing.T, ops []raceOp, seq []int) {
	t.Helper()
	dir := t.TempDir()
	wal := filepath.Join(dir, "test.wal")
	s, err := OpenStore(wal)
	if err != nil {
		t.Fatal(err)
	}
	clock := time.Date(2026, 9, 24, 0, 0, 0, 0, time.UTC)
	s.now = func() time.Time { return clock }
	s.leaseDur = 10 * time.Second
	if _, err := s.Submit("t", "p", 100); err != nil { // generous budget: Fail rarely terminal here
		t.Fatal(err)
	}

	names := make([]string, len(seq))
	prev, _ := s.Get("t")
	for step, opIdx := range seq {
		op := ops[opIdx]
		names[step] = op.name
		err := op.run(s, &clock)

		cur, gerr := s.Get("t")
		if gerr != nil {
			t.Fatalf("seq %v: get: %v", names[:step+1], gerr)
		}
		// Invariant 1: attempt number is monotonic.
		if cur.Attempt < prev.Attempt {
			t.Fatalf("seq %v: attempt regressed %d -> %d", names[:step+1], prev.Attempt, cur.Attempt)
		}
		// Invariant 2: terminal states are absorbing, result included.
		if prev.State.Terminal() && (cur.State != prev.State || cur.Result != prev.Result) {
			t.Fatalf("seq %v: terminal state mutated %s(%q) -> %s(%q)",
				names[:step+1], prev.State, prev.Result, cur.State, cur.Result)
		}
		// Invariant 3: a stale-attempt complete never succeeds.
		if op.name == "completeStale" && err == nil {
			t.Fatalf("seq %v: stale complete succeeded", names[:step+1])
		}
		// Invariant 4: version is monotonic.
		if cur.Version < prev.Version {
			t.Fatalf("seq %v: version regressed %d -> %d", names[:step+1], prev.Version, cur.Version)
		}
		prev = cur
	}
	s.Close()

	// Invariant 5: restart from the WAL reproduces the exact state.
	s2, err := OpenStore(wal)
	if err != nil {
		t.Fatalf("seq %v: reopen: %v", names, err)
	}
	defer s2.Close()
	s2.now = func() time.Time { return clock }
	after, gerr := s2.Get("t")
	if gerr != nil {
		t.Fatalf("seq %v: get after restart: %v", names, gerr)
	}
	if *after != *prev {
		t.Fatalf("seq %v: state changed across restart:\n before=%+v\n after=%+v", names, prev, after)
	}
	// And the terminal state, if any, is still absorbing after the restart.
	if after.State.Terminal() {
		for i, op := range ops {
			_ = op.run(s2, &clock)
			cur, _ := s2.Get("t")
			if cur.State != after.State || cur.Result != after.Result {
				t.Fatalf("seq %v + post-restart %s: terminal state mutated to %s",
					names, ops[i].name, cur.State)
			}
		}
	}
}

// TestFailRetryCycle: fail below the budget re-queues; exhausting the budget
// fails terminally; manual Retry re-opens; attempt numbers keep climbing.
func TestFailRetryCycle(t *testing.T) {
	s, _ := newTestStore(t)
	if _, err := s.Submit("t", "p", 2); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Claim("t", "w"); err != nil {
		t.Fatal(err)
	}
	task, err := s.Fail("t", "w", 1)
	if err != nil || task.State != StatePending {
		t.Fatalf("fail 1: %v state=%s", err, task.State)
	}
	if _, err := s.Claim("t", "w"); err != nil {
		t.Fatal(err)
	}
	task, err = s.Fail("t", "w", 2)
	if err != nil || task.State != StateFailed {
		t.Fatalf("fail 2: %v state=%s", err, task.State)
	}
	if _, err := s.Claim("t", "w"); err != ErrTerminal {
		t.Fatalf("claim on FAILED: %v", err)
	}
	task, err = s.Retry("t")
	if err != nil || task.State != StatePending || task.Attempt != 2 {
		t.Fatalf("retry: %v %+v", err, task)
	}
	task, err = s.Claim("t", "w")
	if err != nil || task.Attempt != 3 {
		t.Fatalf("claim after retry: %v attempt=%d", err, task.Attempt)
	}
	if _, err := s.Complete("t", "w", 2, "stale"); err != ErrStaleAttempt {
		t.Fatalf("stale complete after retry: %v", err)
	}
	if _, err := s.Complete("t", "w", 3, "ok"); err != nil {
		t.Fatalf("complete after retry: %v", err)
	}
}

// TestHTTP exercises the API end to end, including the cancel/complete race
// over the wire.
func TestHTTP(t *testing.T) {
	s, _ := newTestStore(t)
	srv := &server{store: s}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /tasks", srv.createTask)
	mux.HandleFunc("GET /tasks/{id}", srv.getTask)
	mux.HandleFunc("POST /tasks/{id}/claim", srv.claim)
	mux.HandleFunc("POST /tasks/{id}/heartbeat", srv.heartbeat)
	mux.HandleFunc("POST /tasks/{id}/complete", srv.complete)
	mux.HandleFunc("POST /tasks/{id}/cancel", srv.cancel)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	post := func(path, body string) (int, map[string]any) {
		t.Helper()
		resp, err := http.Post(ts.URL+path, "application/json", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var v map[string]any
		if err := json.NewDecoder(resp.Body).Decode(&v); err != nil {
			t.Fatal(err)
		}
		return resp.StatusCode, v
	}

	code, body := post("/tasks", `{"id":"t1","payload":"hello","max_attempts":3}`)
	if code != 201 || body["state"] != "PENDING" {
		t.Fatalf("create: %d %v", code, body)
	}
	code, body = post("/tasks/t1/claim", `{"worker":"w1"}`)
	if code != 200 || body["attempt"].(float64) != 1 {
		t.Fatalf("claim: %d %v", code, body)
	}
	code, body = post("/tasks/t1/heartbeat", `{"worker":"w1","attempt":1}`)
	if code != 200 {
		t.Fatalf("heartbeat: %d %v", code, body)
	}
	// Cancel wins the race because it is issued first here.
	code, _ = post("/tasks/t1/cancel", `{}`)
	if code != 200 {
		t.Fatalf("cancel: %d", code)
	}
	// The losing complete gets 409 with reason "terminal".
	code, body = post("/tasks/t1/complete", `{"worker":"w1","attempt":1,"result":"r"}`)
	if code != 409 || body["error"] != "terminal" {
		t.Fatalf("complete after cancel: %d %v", code, body)
	}
	// Cancel is idempotent on an already-cancelled task.
	code, body = post("/tasks/t1/cancel", `{}`)
	if code != 200 || body["state"] != "CANCELLED" {
		t.Fatalf("re-cancel: %d %v", code, body)
	}
	// Unknown task -> 404.
	code, _ = post("/tasks/nope/cancel", `{}`)
	if code != 404 {
		t.Fatalf("cancel unknown: %d", code)
	}
}

package store

import (
	"encoding/json"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"taskq/internal/machine"
)

type clock struct{ t time.Time }

func (c *clock) now() time.Time          { return c.t }
func (c *clock) advance(d time.Duration) { c.t = c.t.Add(d) }

func openTestStore(t *testing.T, ttl time.Duration, c *clock) *Store {
	t.Helper()
	dir := t.TempDir()
	st, err := Open(filepath.Join(dir, "db"), Options{
		LeaseTTL: ttl, Now: c.now, CompactEvery: 1_000_000,
		NewID: func(int64) string { return "t1" },
		NewToken: func(_ int64, _ string, attempt int) string {
			return map[int]string{1: "tok", 2: "tok2"}[attempt]
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return st
}

func TestRestartDoesNotRegress(t *testing.T) {
	c := &clock{t: time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC)}
	dir := filepath.Join(t.TempDir(), "db")

	open := func() *Store {
		st, err := Open(dir, Options{LeaseTTL: 10 * time.Second, Now: c.now, CompactEvery: 1_000_000,
			NewID: func(int64) string { return "t1" },
			NewToken: func(_ int64, _ string, attempt int) string {
				return map[int]string{1: "tok", 2: "tok2"}[attempt]
			},
		})
		if err != nil {
			t.Fatal(err)
		}
		return st
	}

	st := open()
	task, berr := st.Submit(json.RawMessage(`{"job":"x"}`))
	if berr != nil {
		t.Fatal(berr)
	}
	id := task.ID
	task, _, berr = st.Claim("w1")
	if berr != nil {
		t.Fatal(berr)
	}
	if task.Attempt != 1 {
		t.Fatalf("attempt = %d", task.Attempt)
	}
	task, berr = st.Heartbeat(id, "w1", "tok")
	if berr != nil {
		t.Fatal(berr)
	}
	hbDeadline := task.LeaseDeadline
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}

	// Restart: same state, same seq, same renewed deadline.
	st = open()
	got := st.Get(id)
	if got == nil {
		t.Fatal("task lost across restart")
	}
	if got.State != machine.Running || got.Attempt != 1 || got.WorkerID != "w1" {
		t.Fatalf("after restart = %+v", got)
	}
	if !got.LeaseDeadline.Equal(hbDeadline) {
		t.Fatalf("deadline regressed: %v vs %v", got.LeaseDeadline, hbDeadline)
	}
	if got.Version != 3 {
		t.Fatalf("version after restart = %d", got.Version)
	}

	// Advance past deadline, restart again: timeout must be applied and
	// persisted; an old-worker result is rejected.
	c.advance(20 * time.Second)
	if n, err := st.Sweep(); err != nil || n != 1 {
		t.Fatalf("sweep n=%d err=%v", n, err)
	}
	if got := st.Get(id); got.State != machine.Pending {
		t.Fatalf("state after sweep = %s", got.State)
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}
	st = open()
	got = st.Get(id)
	if got.State != machine.Pending || got.Attempt != 1 || got.WorkerID != "" {
		t.Fatalf("timeout did not survive restart: %+v", got)
	}
	if _, berr := st.Complete(id, "w1", "tok", json.RawMessage(`{"stale":true}`)); berr == nil {
		t.Fatal("stale result accepted after restart")
	}

	// Reclaim and complete; restart; terminal state persists.
	task, _, berr = st.Claim("w2")
	if berr != nil {
		t.Fatal(berr)
	}
	if task.Attempt != 2 || task.LeaseToken != "tok2" {
		t.Fatalf("reclaimed = %+v", task)
	}
	task, berr = st.Complete(id, "w2", "tok2", json.RawMessage(`{"ok":true}`))
	if berr != nil || task.State != machine.Completed {
		t.Fatalf("complete: %v %+v", berr, task)
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}
	st = open()
	got = st.Get(id)
	if got.State != machine.Completed || string(got.Result) != `{"ok":true}` || got.Attempt != 2 {
		t.Fatalf("completed state regressed: %+v", got)
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestCompactionSurvivesRestart(t *testing.T) {
	c := &clock{t: time.Now()}
	dir := filepath.Join(t.TempDir(), "db")
	st, err := Open(dir, Options{LeaseTTL: time.Hour, Now: c.now, CompactEvery: 10})
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 25; i++ {
		task, berr := st.Submit(json.RawMessage(`{}`))
		if berr != nil {
			t.Fatal(berr)
		}
		if _, berr := st.Cancel(task.ID); berr != nil {
			t.Fatal(berr)
		}
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}
	st2, err := Open(dir, Options{LeaseTTL: time.Hour, Now: c.now, CompactEvery: 10})
	if err != nil {
		t.Fatal(err)
	}
	tasks, err := st2.List()
	if err != nil {
		t.Fatal(err)
	}
	if len(tasks) != 25 {
		t.Fatalf("after compaction+restart: %d tasks", len(tasks))
	}
	for _, tk := range tasks {
		if tk.State != machine.Cancelled {
			t.Fatalf("task %s = %s", tk.ID, tk.State)
		}
	}
	if err := st2.Close(); err != nil {
		t.Fatal(err)
	}
}

// TestConcurrentCancelCompleteRace hammers one task with concurrent cancel
// and complete calls. Exactly one must win: final state is one terminal
// state, and a COMPLETED result is always the committed one (never a stale
// writer's), while every rejected complete carries STALE/INVALID error codes.
func TestConcurrentCancelCompleteRace(t *testing.T) {
	c := &clock{t: time.Now()}
	st := openTestStore(t, time.Hour, c)
	task, berr := st.Submit(json.RawMessage(`{}`))
	if berr != nil {
		t.Fatal(berr)
	}
	task, _, berr = st.Claim("w1")
	if berr != nil {
		t.Fatal(berr)
	}
	token := task.LeaseToken
	id := task.ID

	const n = 200
	var wg sync.WaitGroup
	var completesAccepted, rejects int64
	var (
		mu               sync.Mutex
		completeVersions = map[int64]struct{}{}
		cancelVersions   = map[int64]struct{}{}
	)
	wg.Add(2)
	go func() {
		defer wg.Done()
		for i := 0; i < n; i++ {
			tk, e := st.Complete(id, "w1", token, json.RawMessage(`{"i":0}`))
			mu.Lock()
			if e == nil {
				completesAccepted++
				completeVersions[tk.Version] = struct{}{}
			} else {
				rejects++
			}
			mu.Unlock()
		}
	}()
	// Cancellation is idempotent: after the winning cancel, later cancels
	// still return 200 but carry the SAME version (no new event). We count
	// the distinct versions returned to prove only one cancel ever committed.
	go func() {
		defer wg.Done()
		for i := 0; i < n; i++ {
			tk, e := st.Cancel(id)
			if e != nil {
				continue
			}
			mu.Lock()
			cancelVersions[tk.Version] = struct{}{}
			mu.Unlock()
		}
	}()
	wg.Wait()

	final := st.Get(id)
	if !final.State.Terminal() {
		t.Fatalf("final state = %s", final.State)
	}
	switch final.State {
	case machine.Completed:
		if completesAccepted != 1 || len(completeVersions) != 1 {
			t.Fatalf("COMPLETED but completesAccepted=%d distinct versions=%d",
				completesAccepted, len(completeVersions))
		}
		if len(cancelVersions) != 0 {
			t.Fatalf("COMPLETED but a cancel committed a version: %v", cancelVersions)
		}
	case machine.Cancelled:
		if len(cancelVersions) != 1 {
			t.Fatalf("CANCELLED but distinct cancel versions=%d (want exactly 1)", len(cancelVersions))
		}
		if completesAccepted != 0 || len(completeVersions) != 0 {
			t.Fatalf("CANCELLED but a complete committed: %d", completesAccepted)
		}
		// The single committed cancel version must be the final version.
		if _, ok := cancelVersions[final.Version]; !ok {
			t.Fatalf("final version %d not among cancel versions %v", final.Version, cancelVersions)
		}
	}
	t.Logf("winner=%s completesAccepted=%d distinctCancelVersions=%d rejects=%d",
		final.State, completesAccepted, len(cancelVersions), rejects)
}

// TestConcurrentOldWorkersNeverWin lets attempt 1 time out, then redispatch
// as attempt 2 while displaced/forged workers hammer results. The committed
// result must always be attempt 2 owner's, never a stale writer's.
func TestConcurrentOldWorkersNeverWin(t *testing.T) {
	c := &clock{t: time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC)}
	dir := filepath.Join(t.TempDir(), "db")
	st, err := Open(dir, Options{
		LeaseTTL: 50 * time.Millisecond, Now: c.now, CompactEvery: 1_000_000,
	})
	if err != nil {
		t.Fatal(err)
	}
	task, _ := st.Submit(json.RawMessage(`{}`))
	id := task.ID

	// Attempt 1: w1 claims, then the lease times out.
	t1, token1, berr := st.Claim("w1")
	if berr != nil || t1.Attempt != 1 {
		t.Fatalf("claim1: %v %+v", berr, t1)
	}
	c.advance(60 * time.Millisecond)
	if n, err := st.Sweep(); err != nil || n != 1 {
		t.Fatalf("sweep: n=%d err=%v", n, err)
	}
	// w1 is now an old worker holding token1.

	stop := make(chan struct{})
	var wg sync.WaitGroup
	staleHammer := func(worker, token string) {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
			}
			if _, e := st.Complete(id, worker, token, json.RawMessage(`{"who":"stale"}`)); e == nil {
				t.Errorf("stale result accepted by worker=%q token=%q", worker, token)
				return
			}
		}
	}
	wg.Add(2)
	go staleHammer("w1", token1)     // genuine old-attempt owner
	go staleHammer("forged", "xxxx") // never held the lease

	// Attempt 2: the new owner claims and completes.
	t2, token2, berr := st.Claim("w2")
	if berr != nil || t2.Attempt != 2 {
		t.Fatalf("claim2: %v %+v", berr, t2)
	}
	final, berr := st.Complete(id, "w2", token2, json.RawMessage(`{"who":"cur"}`))
	if berr != nil || final.State != machine.Completed {
		t.Fatalf("current owner complete: %v %+v", berr, final)
	}
	close(stop)
	wg.Wait()

	if string(final.Result) != `{"who":"cur"}` {
		t.Fatalf("committed result came from a stale worker: %s", final.Result)
	}

	// Restart: the committed terminal state and attempt number persist.
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}
	st2, err := Open(dir, Options{LeaseTTL: 50 * time.Millisecond, Now: c.now, CompactEvery: 1_000_000})
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	got := st2.Get(id)
	if got.State != machine.Completed || got.Attempt != 2 || string(got.Result) != `{"who":"cur"}` {
		t.Fatalf("state regressed after restart: %+v", got)
	}
}

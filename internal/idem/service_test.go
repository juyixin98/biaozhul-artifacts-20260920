package idem

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"idemresp/internal/client"
	"idemresp/internal/clock"
	"idemresp/internal/txn"
)

// fakeGateway is a scripted GatewayCaller for service-level tests. The
// script is consumed in GLOBAL call order across all Execute invocations,
// so "first call fails, retry succeeds" can be scripted naturally.
type fakeGateway struct {
	mu        sync.Mutex
	calls     []client.ChargeRequest
	script    []client.Result
	idx       int
	block     chan struct{} // if non-nil, the first charge waits for close
	blocked   chan struct{} // signaled when the first charge starts blocking
	blockOnce sync.Once
}

func (f *fakeGateway) Charge(_ context.Context, attempt int, req client.ChargeRequest) client.Result {
	f.mu.Lock()
	f.calls = append(f.calls, req)
	block := f.block
	f.mu.Unlock()

	if block != nil {
		f.blockOnce.Do(func() { close(f.blocked) })
		<-block
	}

	f.mu.Lock()
	defer f.mu.Unlock()
	if f.idx < len(f.script) {
		r := f.script[f.idx]
		f.idx++
		return r
	}
	return client.Result{Outcome: client.OutcomeSuccess, HTTPStatus: 201,
		Charge: &client.Charge{ID: "ch_default", Amount: req.Amount, Currency: req.Currency}}
}

func (f *fakeGateway) callCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.calls)
}

func successResult(id string, deduped bool) client.Result {
	status := 201
	if deduped {
		status = 200
	}
	return client.Result{Outcome: client.OutcomeSuccess, HTTPStatus: status,
		Charge: &client.Charge{ID: id, Amount: 100, Currency: "USD"}}
}

type harness struct {
	svc *Service
	db  *txn.DB
	gw  *fakeGateway
	clk *clock.Fake
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	clk := clock.NewFake(time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC))
	db, err := txn.Open(t.TempDir(), clk)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	gw := &fakeGateway{}
	svc := New(Config{
		DB:               db,
		GW:               gw,
		Clk:              clk,
		Lease:            time.Hour,
		AmbiguousRetries: 1,
	})
	return &harness{svc: svc, db: db, gw: gw, clk: clk}
}

func validBody() []byte { return []byte(`{"amount":100,"currency":"USD"}`) }

func execute(h *harness, key string, body []byte, forward bool) Output {
	return h.svc.Execute(context.Background(), Input{
		Key:        key,
		Method:     "POST",
		Path:       "/v1/orders",
		Body:       body,
		ForwardKey: forward,
	})
}

func TestHappyPathCommitOnceReplaySameResponse(t *testing.T) {
	h := newHarness(t)
	key := "key-happy"

	out := execute(h, key, validBody(), true)
	if out.Status != 201 {
		t.Fatalf("first status = %d body=%s", out.Status, out.Body)
	}
	firstBody := append([]byte(nil), out.Body...)

	out2 := execute(h, key, validBody(), true)
	if out2.Status != 201 || !out2.Replayed {
		t.Fatalf("replay: status=%d replayed=%v", out2.Status, out2.Replayed)
	}
	if string(out2.Body) != string(firstBody) {
		t.Fatalf("replayed response differs:\n%s\nvs\n%s", firstBody, out2.Body)
	}
	if h.gw.callCount() != 1 {
		t.Fatalf("gateway calls = %d, want 1", h.gw.callCount())
	}
	if got := h.db.CountLedgerForKey(key); got != 1 {
		t.Fatalf("ledger side effects = %d, want 1", got)
	}
}

func TestSameKeyDifferentBodyIsConflict(t *testing.T) {
	h := newHarness(t)
	key := "key-conflict"
	if out := execute(h, key, []byte(`{"amount":100,"currency":"USD"}`), true); out.Status != 201 {
		t.Fatalf("setup status = %d", out.Status)
	}
	out := execute(h, key, []byte(`{"amount":250,"currency":"USD"}`), true)
	if out.Status != 409 || !strings.Contains(string(out.Body), "idempotency_key_conflict") {
		t.Fatalf("expected 409 conflict, got %d %s", out.Status, out.Body)
	}
	// Conflict must not add a side effect.
	if got := h.db.CountLedgerForKey(key); got != 1 {
		t.Fatalf("ledger after conflict = %d, want 1", got)
	}
}

func TestInProgressDuplicateThenReplay(t *testing.T) {
	h := newHarness(t)
	key := "key-progress"
	h.gw.block = make(chan struct{})
	h.gw.blocked = make(chan struct{})

	var first Output
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		first = execute(h, key, validBody(), true)
	}()

	<-h.gw.blocked // first request is inside the gateway call

	// A concurrent identical request must get an explicit in-progress
	// response rather than triggering a second execution.
	dup := execute(h, key, validBody(), true)
	if dup.Status != 409 || !dup.InProgress {
		t.Fatalf("duplicate: status=%d inProgress=%v body=%s", dup.Status, dup.InProgress, dup.Body)
	}
	if dup.RetryAfter <= 0 {
		t.Fatal("Retry-After hint missing")
	}

	close(h.gw.block)
	wg.Wait()
	if first.Status != 201 {
		t.Fatalf("first status = %d", first.Status)
	}

	// The duplicate retried AFTER completion replays the stored response.
	retry := execute(h, key, validBody(), true)
	if retry.Status != 201 || !retry.Replayed {
		t.Fatalf("post-completion retry: status=%d replayed=%v", retry.Status, retry.Replayed)
	}
	if h.gw.callCount() != 1 {
		t.Fatalf("gateway calls = %d, want 1", h.gw.callCount())
	}
	if got := h.db.CountLedgerForKey(key); got != 1 {
		t.Fatalf("ledger = %d, want 1", got)
	}
}

func TestConcurrentRequestsExactlyOneSideEffect(t *testing.T) {
	h := newHarness(t)
	key := "key-concurrent"
	h.gw.block = make(chan struct{})
	h.gw.blocked = make(chan struct{})

	const n = 24
	results := make(chan Output, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			results <- execute(h, key, validBody(), true)
		}()
	}

	<-h.gw.blocked
	// Give the other goroutines time to hit the processing claim.
	time.Sleep(20 * time.Millisecond)
	close(h.gw.block)
	wg.Wait()
	close(results)

	created, inProgress := 0, 0
	for o := range results {
		switch {
		case o.Status == 201 && !o.Replayed:
			created++
		case o.Status == 409 && o.InProgress:
			inProgress++
		default:
			t.Fatalf("unexpected concurrent outcome: %d %s", o.Status, o.Body)
		}
	}
	if created != 1 {
		t.Fatalf("created executions = %d, want 1", created)
	}
	if inProgress != n-1 {
		t.Fatalf("in-progress responses = %d, want %d", inProgress, n-1)
	}
	if h.gw.callCount() != 1 {
		t.Fatalf("gateway calls = %d, want 1", h.gw.callCount())
	}
	if got := h.db.CountLedgerForKey(key); got != 1 {
		t.Fatalf("ledger = %d, want 1", got)
	}

	// Every client retries after completion: all see the same replay.
	for i := 0; i < n; i++ {
		o := execute(h, key, validBody(), true)
		if o.Status != 201 || !o.Replayed {
			t.Fatalf("retry %d: status=%d replayed=%v", i, o.Status, o.Replayed)
		}
	}
	if h.gw.callCount() != 1 {
		t.Fatalf("gateway calls after retries = %d, want 1", h.gw.callCount())
	}
}

func TestDefiniteFailureThenRetryCommitsOnce(t *testing.T) {
	h := newHarness(t)
	key := "key-failed"
	h.gw.script = []client.Result{
		{Outcome: client.OutcomeFailed, HTTPStatus: 500,
			Err: errString("gateway 500")},
	}

	out := execute(h, key, validBody(), true)
	if out.Status != 502 {
		t.Fatalf("failed status = %d body=%s", out.Status, out.Body)
	}
	if got := h.db.CountLedgerForKey(key); got != 0 {
		t.Fatalf("ledger after failure = %d, want 0", got)
	}

	// Failed keys are retryable immediately; second attempt succeeds.
	out = execute(h, key, validBody(), true)
	if out.Status != 201 {
		t.Fatalf("retry status = %d body=%s", out.Status, out.Body)
	}
	if got := h.db.CountLedgerForKey(key); got != 1 {
		t.Fatalf("ledger after retry = %d, want 1", got)
	}
	if h.gw.callCount() != 2 {
		t.Fatalf("gateway calls = %d, want 2", h.gw.callCount())
	}
}

func TestAmbiguousWithForwardedKeySafelyRetries(t *testing.T) {
	h := newHarness(t)
	key := "key-ambig-keyed"
	h.gw.script = []client.Result{
		{Outcome: client.OutcomeAmbiguous, Err: errString("EOF")},
		successResult("ch_1", true), // gateway deduped the lost call
	}

	out := execute(h, key, validBody(), true)
	if out.Status != 201 {
		t.Fatalf("status = %d body=%s", out.Status, out.Body)
	}
	if h.gw.callCount() != 2 {
		t.Fatalf("gateway calls = %d, want 2", h.gw.callCount())
	}
	if !strings.Contains(string(out.Body), `"deduped": true`) {
		t.Fatalf("expected deduped response, got %s", out.Body)
	}
	if got := h.db.CountLedgerForKey(key); got != 1 {
		t.Fatalf("ledger = %d, want 1", got)
	}

	// Both calls must carry the same end-to-end key.
	for i, c := range h.gw.calls {
		if c.IdempotencyKey != key {
			t.Fatalf("call %d key = %q, want %q", i, c.IdempotencyKey, key)
		}
	}
}

func TestAmbiguousWithoutKeyDoesNotCommitOrRetry(t *testing.T) {
	h := newHarness(t)
	key := "key-ambig-keyless"
	h.gw.script = []client.Result{
		{Outcome: client.OutcomeAmbiguous, Err: errString("reset")},
	}

	out := execute(h, key, validBody(), false)
	if out.Status != 503 || !strings.Contains(string(out.Body), "ambiguous_outcome") {
		t.Fatalf("ambiguous keyless: status=%d body=%s", out.Status, out.Body)
	}
	if h.gw.callCount() != 1 {
		t.Fatalf("gateway calls = %d, want 1 (no blind retry)", h.gw.callCount())
	}
	if got := h.db.CountLedgerForKey(key); got != 0 {
		t.Fatalf("ledger = %d, want 0", got)
	}

	// Retry within the lease: still processing, still no re-execution.
	out2 := execute(h, key, validBody(), false)
	if !out2.InProgress {
		t.Fatalf("expected in-progress, got %d", out2.Status)
	}
	if h.gw.callCount() != 1 {
		t.Fatalf("gateway calls = %d, still want 1", h.gw.callCount())
	}

	// After lease expiry the claim may be reclaimed (crash recovery
	// semantics). With NO end-to-end key that re-runs the external call —
	// this is the documented boundary, not a guarantee.
	h.clk.Add(time.Hour + time.Second)
	h.gw.script = nil
	out3 := execute(h, key, validBody(), false)
	if out3.Status != 201 {
		t.Fatalf("post-lease retry status = %d body=%s", out3.Status, out3.Body)
	}
	if got := h.db.CountLedgerForKey(key); got != 1 {
		t.Fatalf("local ledger = %d, want 1", got)
	}
}

func TestValidationFailureDoesNotCommitSideEffect(t *testing.T) {
	h := newHarness(t)
	key := "key-bad-amount"
	out := execute(h, key, []byte(`{"amount":-5,"currency":"USD"}`), true)
	if out.Status != 400 {
		t.Fatalf("status = %d body=%s", out.Status, out.Body)
	}
	if h.gw.callCount() != 0 {
		t.Fatalf("gateway calls = %d, want 0", h.gw.callCount())
	}
	if got := h.db.CountLedgerForKey(key); got != 0 {
		t.Fatalf("ledger = %d, want 0", got)
	}
}

func TestCrashHooksFireAroundCommit(t *testing.T) {
	h := newHarness(t)
	var stages []string
	h.svc.cfg.Crash = func(stage string) { stages = append(stages, stage) }

	key := "key-crash"
	in := Input{Key: key, Method: "POST", Path: "/v1/orders", Body: validBody(),
		ForwardKey: true, CrashPoint: CrashBeforeCommit}
	if out := h.svc.Execute(context.Background(), in); out.Status != 201 {
		t.Fatalf("before-commit hook run status = %d", out.Status)
	}
	if got := h.db.CountLedgerForKey(key); got != 1 {
		t.Fatalf("ledger = %d, want 1", got)
	}

	key2 := "key-crash2"
	in2 := Input{Key: key2, Method: "POST", Path: "/v1/orders", Body: validBody(),
		ForwardKey: true, CrashPoint: CrashAfterCommit}
	if out := h.svc.Execute(context.Background(), in2); out.Status != 201 {
		t.Fatalf("after-commit hook run status = %d", out.Status)
	}
	if len(stages) != 2 || stages[0] != CrashBeforeCommit || stages[1] != CrashAfterCommit {
		t.Fatalf("crash stages = %v", stages)
	}
}

type errString string

func (e errString) Error() string { return string(e) }

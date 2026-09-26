package idem

import (
	"context"
	"strings"
	"testing"
	"time"

	"idemresp/internal/client"
	"idemresp/internal/digest"
	"idemresp/internal/txn"
)

func TestNewAppliesDefaults(t *testing.T) {
	db, err := txn.Open(t.TempDir(), nil)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer func() { _ = db.Close() }()

	s := New(Config{DB: db, GW: &fakeGateway{}})
	if s.cfg.Lease != 30*time.Second {
		t.Fatalf("default lease = %v", s.cfg.Lease)
	}
	if s.cfg.AmbiguousRetries != 1 {
		t.Fatalf("default retries = %d", s.cfg.AmbiguousRetries)
	}
	if s.cfg.Logf == nil || s.cfg.Clk == nil {
		t.Fatal("defaults for Logf/Clk not set")
	}
	// Logf with no logger must not panic.
	s.cfg.Logf("hello %s", "world")
}

func TestInvalidJSONAtDigestStage(t *testing.T) {
	h := newHarness(t)
	out := h.svc.Execute(context.Background(), Input{
		Key: "key-bad-digest", Method: "POST", Path: "/v1/orders", Body: []byte("nope"),
	})
	if out.Status != 400 {
		t.Fatalf("status = %d body=%s", out.Status, out.Body)
	}
}

func TestMissingCurrencyRejected(t *testing.T) {
	h := newHarness(t)
	out := execute(h, "key-no-currency", []byte(`{"amount":10}`), true)
	if out.Status != 400 {
		t.Fatalf("status = %d body=%s", out.Status, out.Body)
	}
	if h.gw.callCount() != 0 {
		t.Fatalf("gateway calls = %d, want 0", h.gw.callCount())
	}
}

func TestPreCommitContextCancelAbortsCommit(t *testing.T) {
	h := newHarness(t)
	key := "key-cancel-pre"
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()
	out := h.svc.Execute(ctx, Input{
		Key: key, Method: "POST", Path: "/v1/orders", Body: validBody(),
		ForwardKey: true, PreCommitDelay: time.Second,
	})
	if out.Status != 503 {
		t.Fatalf("status = %d body=%s", out.Status, out.Body)
	}
	if got := h.db.CountLedgerForKey(key); got != 0 {
		t.Fatalf("ledger = %d, want 0 after pre-commit abort", got)
	}
}

func TestPostCommitContextCancelStillReturnsResult(t *testing.T) {
	h := newHarness(t)
	key := "key-cancel-post"
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()
	out := h.svc.Execute(ctx, Input{
		Key: key, Method: "POST", Path: "/v1/orders", Body: validBody(),
		ForwardKey: true, PostCommitDelay: time.Second,
	})
	if out.Status != 201 {
		t.Fatalf("status = %d body=%s", out.Status, out.Body)
	}
	if got := h.db.CountLedgerForKey(key); got != 1 {
		t.Fatalf("ledger = %d, want 1 (commit already durable)", got)
	}
}

func TestCommitFailureMapped500(t *testing.T) {
	h := newHarness(t)
	key := "key-commit-fail"
	// The crash hook runs immediately before Commit: if it completes the
	// key out from under the request, the service's own Commit fails
	// because the key is no longer processing.
	h.svc.cfg.Crash = func(_ string) {
		err := h.db.Commit(key, 201, []byte(`{"stolen":true}`), nil, nil)
		if err != nil {
			t.Errorf("setup commit failed: %v", err)
		}
	}
	out := h.svc.Execute(context.Background(), Input{
		Key: key, Method: "POST", Path: "/v1/orders", Body: validBody(),
		ForwardKey: true, CrashPoint: CrashBeforeCommit,
	})
	if out.Status != 500 || !strings.Contains(string(out.Body), "commit_failed") {
		t.Fatalf("status = %d body=%s", out.Status, out.Body)
	}
}

func TestReplayStoredZeroStatusDefaultsToOK(t *testing.T) {
	h := newHarness(t)
	key := "key-zero-status"
	if _, err := h.db.Acquire(key, "POST", "/v1/orders", mustHash(key), time.Hour); err != nil {
		t.Fatalf("acquire: %v", err)
	}
	if err := h.db.Commit(key, 0, []byte(`{"x":1}`), nil, nil); err != nil {
		t.Fatalf("commit: %v", err)
	}
	out := execute(h, key, validBodyForHash(key), true)
	if out.Status != 200 || !out.Replayed {
		t.Fatalf("status = %d replayed=%v", out.Status, out.Replayed)
	}
}

// mustHash and validBodyForHash build a body whose digest matches a
// directly-acquired row for the replay test.
func mustHash(key string) string {
	body := validBodyForHash(key)
	h, err := digest.Of("POST", "/v1/orders", body)
	if err != nil {
		panic(err)
	}
	return h
}

func validBodyForHash(_ string) []byte { return validBody() }

func TestAmbiguousKeylessWithinLeaseAndImmediateFail(t *testing.T) {
	// Covers: failed definite error path already; here ensure an
	// ambiguous keyless result followed by same-body retry stays blocked.
	h := newHarness(t)
	key := "key-ambig-blocked"
	h.gw.script = []client.Result{
		{Outcome: client.OutcomeAmbiguous, Err: errString("reset")},
	}
	out := execute(h, key, validBody(), false)
	if out.Status != 503 {
		t.Fatalf("status = %d", out.Status)
	}
	out2 := execute(h, key, validBody(), false)
	if out2.Status != 409 || !out2.InProgress {
		t.Fatalf("retry during processing: %d inProgress=%v", out2.Status, out2.InProgress)
	}
}

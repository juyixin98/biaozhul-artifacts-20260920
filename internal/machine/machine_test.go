package machine

import (
	"encoding/json"
	"testing"
	"time"
)

// fixedClock provides a controllable deterministic clock.
func testMachine() (*Machine, *time.Time) {
	now := time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC)
	m := New(Config{
		LeaseTTL: 10 * time.Second,
		NewID:    func(seq int64) string { return "t1" },
		NewToken: func(_ int64, _ string, attempt int) string { return "tok" },
	})
	return m, &now
}

func at(t time.Time, extra time.Duration) time.Time { return t.Add(extra) }

func TestSubmitAndClaimLifecycle(t *testing.T) {
	m, now := testMachine()
	task, evs, err := m.Step(Cmd{Kind: SubmitCmd, Now: *now, Payload: json.RawMessage(`{"k":1}`)})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	if len(evs) != 1 || evs[0].Seq != 1 {
		t.Fatalf("submit events = %+v", evs)
	}
	if task.State != Pending {
		t.Fatalf("state = %s", task.State)
	}

	task, _, err = m.Step(Cmd{Kind: ClaimCmd, Now: *now, WorkerID: "w1"})
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if task.State != Running || task.Attempt != 1 || task.WorkerID != "w1" || task.LeaseToken != "tok" {
		t.Fatalf("claimed task = %+v", task)
	}
	if !task.LeaseDeadline.Equal(now.Add(10 * time.Second)) {
		t.Fatalf("deadline = %v", task.LeaseDeadline)
	}

	// Second claim with nothing pending.
	if _, _, err := m.Step(Cmd{Kind: ClaimCmd, Now: *now, WorkerID: "w2"}); err == nil || err.Code != CodeNoTask {
		t.Fatalf("second claim err = %v, want NO_TASK_AVAILABLE", err)
	}
}

func TestCompleteRequiresCurrentLease(t *testing.T) {
	m, now := testMachine()
	_, _, _ = m.Step(Cmd{Kind: SubmitCmd, Now: *now})
	_, _, _ = m.Step(Cmd{Kind: ClaimCmd, Now: *now, WorkerID: "w1"})

	// Wrong worker.
	_, _, err := m.Step(Cmd{Kind: CompleteCmd, ID: "t1", Now: *now, WorkerID: "w2",
		LeaseToken: "tok", Payload: json.RawMessage(`{"r":1}`)})
	if err == nil || err.Code != CodeStaleLease {
		t.Fatalf("wrong worker complete err = %v", err)
	}
	// Wrong token.
	_, _, err = m.Step(Cmd{Kind: CompleteCmd, ID: "t1", Now: *now, WorkerID: "w1",
		LeaseToken: "forged"})
	if err == nil || err.Code != CodeStaleLease {
		t.Fatalf("forged token complete err = %v", err)
	}
	// Correct lease -> completed with result.
	task, _, err := m.Step(Cmd{Kind: CompleteCmd, ID: "t1", Now: *now, WorkerID: "w1",
		LeaseToken: "tok", Payload: json.RawMessage(`{"r":42}`)})
	if err != nil {
		t.Fatalf("complete: %v", err)
	}
	if task.State != Completed || string(task.Result) != `{"r":42}` || task.LeaseToken != "" {
		t.Fatalf("completed task = %+v", task)
	}
	// Second complete is rejected: not running.
	_, _, err = m.Step(Cmd{Kind: CompleteCmd, ID: "t1", Now: *now, WorkerID: "w1",
		LeaseToken: "tok", Payload: json.RawMessage(`{"r":43}`)})
	if err == nil || err.Code != CodeInvalidState {
		t.Fatalf("double complete err = %v", err)
	}
}

func TestHeartbeatExtendsLease(t *testing.T) {
	m, now := testMachine()
	_, _, _ = m.Step(Cmd{Kind: SubmitCmd, Now: *now})
	_, _, _ = m.Step(Cmd{Kind: ClaimCmd, Now: *now, WorkerID: "w1"})

	later := at(*now, 9*time.Second)
	task, _, err := m.Step(Cmd{Kind: HeartbeatCmd, ID: "t1", Now: later,
		WorkerID: "w1", LeaseToken: "tok"})
	if err != nil {
		t.Fatalf("heartbeat: %v", err)
	}
	if !task.LeaseDeadline.Equal(later.Add(10 * time.Second)) {
		t.Fatalf("deadline after hb = %v, want %v", task.LeaseDeadline, later.Add(10*time.Second))
	}

	// At +19s the original lease (d=+10s) would be long expired, but the
	// heartbeat at +9s renewed the deadline to +19s. Exactly at +19s the
	// lease expires; at +18s it must still be RUNNING.
	at18 := at(*now, 18*time.Second)
	m.ExpireDue(at18)
	if got := m.Get("t1"); got.State != Running {
		t.Fatalf("after expiry pass at 18s state = %s, want RUNNING (renewed)", got.State)
	}
	at19 := at(*now, 19*time.Second)
	m.ExpireDue(at19)
	if got := m.Get("t1"); got.State != Pending {
		t.Fatalf("after expiry pass at 19s state = %s, want PENDING", got.State)
	}
}

func TestTimeoutRequeuesAndOldLeaseDies(t *testing.T) {
	m, now := testMachine()
	_, _, _ = m.Step(Cmd{Kind: SubmitCmd, Now: *now})
	claimed, _, _ := m.Step(Cmd{Kind: ClaimCmd, Now: *now, WorkerID: "w1"})
	if claimed.Attempt != 1 {
		t.Fatalf("attempt = %d", claimed.Attempt)
	}

	// Exactly at the deadline the lease expires (<= now).
	events := m.ExpireDue(now.Add(10 * time.Second))
	if len(events) != 1 {
		t.Fatalf("expiry events = %d", len(events))
	}
	got := m.Get("t1")
	if got.State != Pending || got.WorkerID != "" || got.LeaseToken != "" {
		t.Fatalf("after timeout task = %+v", got)
	}

	// Old worker's result is rejected even before re-claim.
	_, _, err := m.Apply(Cmd{Kind: CompleteCmd, ID: "t1", Now: now.Add(10 * time.Second),
		WorkerID: "w1", LeaseToken: "tok", Payload: json.RawMessage(`{}`)})
	if err == nil || err.Code != CodeInvalidState {
		t.Fatalf("stale result after timeout err = %v", err)
	}

	// Re-claim starts attempt 2 with a fresh token.
	reclaimed, _, _ := m.Apply(Cmd{Kind: ClaimCmd, Now: now.Add(11 * time.Second), WorkerID: "w2"})
	if reclaimed.Attempt != 2 || reclaimed.WorkerID != "w2" {
		t.Fatalf("reclaimed = %+v", reclaimed)
	}
	// w1 using the attempt-1 token must be rejected on attempt 2.
	_, _, err = m.Apply(Cmd{Kind: CompleteCmd, ID: "t1", Now: now.Add(12 * time.Second),
		WorkerID: "w1", LeaseToken: "tok", Payload: json.RawMessage(`{}`)})
	if err == nil || err.Code != CodeStaleLease {
		t.Fatalf("old-attempt token err = %v", err)
	}
}

func TestCancelPendingAndRunning(t *testing.T) {
	m, now := testMachine()
	_, _, _ = m.Step(Cmd{Kind: SubmitCmd, Now: *now})

	// Cancel before claim.
	task, _, err := m.Step(Cmd{Kind: CancelCmd, ID: "t1", Now: *now})
	if err != nil || task.State != Cancelled {
		t.Fatalf("cancel pending: %v %+v", err, task)
	}
	// Idempotent cancel.
	task, evs, err := m.Step(Cmd{Kind: CancelCmd, ID: "t1", Now: *now})
	if err != nil || task.State != Cancelled || len(evs) != 0 {
		t.Fatalf("idempotent cancel: %v evs=%d", err, len(evs))
	}

	// A second task: cancel while RUNNING kills the lease.
	_, _, _ = m.Step(Cmd{Kind: SubmitCmd, ID: "t2", Now: *now})
	_, _, _ = m.Step(Cmd{Kind: ClaimCmd, ID: "t2", Now: *now, WorkerID: "w1"})
	task, _, err = m.Step(Cmd{Kind: CancelCmd, ID: "t2", Now: *now})
	if err != nil || task.State != Cancelled || task.LeaseToken != "" {
		t.Fatalf("cancel running: %v %+v", err, task)
	}
	// Claim must skip the cancelled task.
	if _, _, err := m.Step(Cmd{Kind: ClaimCmd, Now: *now, WorkerID: "w9"}); err == nil {
		t.Fatalf("claim unexpectedly returned cancelled task")
	}
}

func TestCancelAfterCompleteRejected(t *testing.T) {
	m, now := testMachine()
	_, _, _ = m.Step(Cmd{Kind: SubmitCmd, Now: *now})
	_, _, _ = m.Step(Cmd{Kind: ClaimCmd, Now: *now, WorkerID: "w1"})
	_, _, _ = m.Step(Cmd{Kind: CompleteCmd, ID: "t1", Now: *now,
		WorkerID: "w1", LeaseToken: "tok", Payload: json.RawMessage(`{}`)})
	_, _, err := m.Step(Cmd{Kind: CancelCmd, ID: "t1", Now: *now})
	if err == nil || err.Code != CodeInvalidState {
		t.Fatalf("cancel after complete err = %v", err)
	}
}

func TestRetrySemantics(t *testing.T) {
	m, now := testMachine()
	_, _, _ = m.Step(Cmd{Kind: SubmitCmd, Now: *now})
	_, _, _ = m.Step(Cmd{Kind: ClaimCmd, Now: *now, WorkerID: "w1"})

	// A different worker cannot force someone else's task back to pending.
	_, _, err := m.Step(Cmd{Kind: RetryCmd, ID: "t1", Now: *now, WorkerID: "w2", LeaseToken: "tok"})
	if err == nil || err.Code != CodeStaleLease {
		t.Fatalf("foreign retry err = %v", err)
	}

	// Owner gives it back.
	task, _, err := m.Step(Cmd{Kind: RetryCmd, ID: "t1", Now: *now, WorkerID: "w1", LeaseToken: "tok"})
	if err != nil || task.State != Pending || task.LeaseToken != "" {
		t.Fatalf("owner retry: %v %+v", err, task)
	}
	// Retry when already pending = idempotent, no event.
	_, evs, err := m.Step(Cmd{Kind: RetryCmd, ID: "t1", Now: *now})
	if err != nil || len(evs) != 0 {
		t.Fatalf("idempotent retry: %v evs=%d", err, len(evs))
	}

	// Retry of terminal tasks is rejected.
	_, _, _ = m.Step(Cmd{Kind: CancelCmd, ID: "t1", Now: *now})
	if _, _, err := m.Step(Cmd{Kind: RetryCmd, ID: "t1", Now: *now}); err == nil {
		t.Fatalf("retry cancelled unexpectedly succeeded")
	}
}

func TestSubmitValidation(t *testing.T) {
	m, now := testMachine()
	if _, _, err := m.Step(Cmd{Kind: SubmitCmd, Now: *now, Payload: json.RawMessage(`{bad`)}); err == nil {
		t.Fatalf("invalid JSON accepted")
	}
	_, _, _ = m.Step(Cmd{Kind: SubmitCmd, ID: "dup", Now: *now})
	if _, _, err := m.Step(Cmd{Kind: SubmitCmd, ID: "dup", Now: *now}); err == nil {
		t.Fatalf("duplicate id accepted")
	}
}

func TestRestoreAndSnapshot(t *testing.T) {
	m, now := testMachine()
	_, evs, _ := m.Step(Cmd{Kind: SubmitCmd, ID: "t1", Now: *now})
	_, evs2, _ := m.Step(Cmd{Kind: ClaimCmd, ID: "t1", Now: *now, WorkerID: "w1"})
	all := append(evs, evs2...)

	m2 := New(Config{LeaseTTL: 10 * time.Second})
	for _, e := range all {
		m2.Restore(e)
	}
	got := m2.Get("t1")
	if got.State != Running || got.Attempt != 1 || got.Version != 2 {
		t.Fatalf("restored = %+v", got)
	}
	if m2.Seq() != 2 {
		t.Fatalf("restored seq = %d", m2.Seq())
	}
}

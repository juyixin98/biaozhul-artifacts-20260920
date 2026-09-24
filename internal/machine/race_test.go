package machine

import (
	"encoding/json"
	"reflect"
	"testing"
	"time"
)

// prepareRunning builds a fresh machine with t1 RUNNING under w1, attempt 1,
// deterministic token "tok" and lease deadline t0+10s.
func prepareRunning(t *testing.T) (*Machine, time.Time) {
	t.Helper()
	m, now := testMachine()
	if _, _, err := m.Step(Cmd{Kind: SubmitCmd, ID: "t1", Now: *now,
		Payload: json.RawMessage(`{}`)}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := m.Step(Cmd{Kind: ClaimCmd, ID: "t1", Now: *now, WorkerID: "w1"}); err != nil {
		t.Fatal(err)
	}
	// Make the attempt-2 token distinguishable.
	m.cfg.NewToken = func(_ int64, _ string, attempt int) string {
		if attempt >= 2 {
			return "tok2"
		}
		return "tok"
	}
	return m, *now
}

func permutations(in []string) [][]string {
	var out [][]string
	var rec func([]string, int)
	rec = func(a []string, k int) {
		if k == len(a) {
			out = append(out, append([]string(nil), a...))
			return
		}
		for i := k; i < len(a); i++ {
			a[k], a[i] = a[i], a[k]
			rec(a, k+1)
			a[k], a[i] = a[i], a[k]
		}
	}
	rec(append([]string(nil), in...), 0)
	return out
}

// forcedAction applies one atomic event directly, without the lazy-expiry
// wrapper: "timeout" is therefore an explicit, positioned linearization
// event, exactly as a sweep (or the first command past the deadline) would be.
func forcedAction(t *testing.T, m *Machine, now time.Time, action string) (ok bool, code ErrorCode) {
	t.Helper()
	apply := func(c Cmd) (bool, ErrorCode) {
		_, _, err := m.Apply(c)
		if err != nil {
			return false, err.Code
		}
		return true, ""
	}
	switch action {
	case "cancel":
		return apply(Cmd{Kind: CancelCmd, ID: "t1", Now: now})
	case "complete":
		return apply(Cmd{Kind: CompleteCmd, ID: "t1", Now: now,
			WorkerID: "w1", LeaseToken: "tok", Payload: json.RawMessage(`{"r":1}`)})
	case "timeout":
		_ = m.ExpireDue(now)
		return true, ""
	default:
		t.Fatalf("unknown action %q", action)
		return false, ""
	}
}

// TestExhaustiveCancelTimeoutComplete enumerates every ordering (3! = 6) of
// the three racing events cancel / complete / lease-timeout and asserts:
//
//  1. the winner is determined solely by who linearizes first:
//     complete first => COMPLETED, result kept; otherwise CANCELLED;
//  2. the current-attempt result is accepted in exactly that one ordering
//     and rejected (STALE_LEASE/INVALID_STATE) in the other five;
//  3. the final state is terminal, unique, and identical on repeated runs;
//  4. the same outcome arises through Step (lazy expiry at the deadline) as
//     through an explicit timeout event — i.e. it does not matter whether the
//     timeout is observed by a sweep or by the next request.
func TestExhaustiveCancelTimeoutComplete(t *testing.T) {
	actions := []string{"cancel", "complete", "timeout"}
	for _, perm := range permutations(actions) {
		name := perm[0] + ">" + perm[1] + ">" + perm[2]
		t.Run(name, func(t *testing.T) {
			// --- model A: explicit positioned timeout (Apply/ExpireDue) ---
			m, now := prepareRunning(t)
			deadline := now.Add(10 * time.Second)
			completeOK := false
			var completeCode ErrorCode
			for _, a := range perm {
				ok, code := forcedAction(t, m, deadline, a)
				if a == "complete" {
					completeOK, completeCode = ok, code
				}
			}
			final := m.Get("t1")

			// Determinism: same permutation on a fresh machine => identical.
			m2, _ := prepareRunning(t)
			for _, a := range perm {
				_, _ = forcedAction(t, m2, deadline, a)
			}
			final2 := m2.Get("t1")
			if !reflect.DeepEqual(final, final2) {
				t.Fatalf("non-deterministic final state:\n%+v\n%+v", final, final2)
			}

			// Winner = whoever is first.
			wantState := Cancelled
			if perm[0] == "complete" {
				wantState = Completed
			}
			if final.State != wantState {
				t.Fatalf("state = %s, want %s (perm %v)", final.State, wantState, perm)
			}
			if !final.State.Terminal() {
				t.Fatalf("final state %s is not terminal", final.State)
			}
			if completeOK != (perm[0] == "complete") {
				t.Fatalf("complete accepted=%v code=%q, want accepted=%v",
					completeOK, completeCode, perm[0] == "complete")
			}
			if perm[0] == "complete" {
				if string(final.Result) != `{"r":1}` {
					t.Fatalf("result lost: %s", final.Result)
				}
			} else if len(final.Result) != 0 {
				t.Fatalf("rejected completion left a result: %s", final.Result)
			}

			// --- model B: realistic Step timeline with lazy expiry ---
			// No sweep occurs at the timeout slot; the first request after
			// the deadline must expire the lease itself inside Step. The
			// resulting terminal state and the complete accept/reject verdict
			// must match the explicit-event model A.
			m4, now4 := prepareRunning(t)
			d4 := now4.Add(10 * time.Second)
			timeoutSlot := indexOf(perm, "timeout")
			stepCompleteOK := false
			for i, a := range perm {
				if a == "timeout" {
					continue // lazy: no explicit sweep
				}
				var ts time.Time
				if i < timeoutSlot {
					ts = d4.Add(-1 * time.Second)
				} else {
					ts = d4.Add(1 * time.Second)
				}
				var ok bool
				if a == "cancel" {
					_, _, e := m4.Step(Cmd{Kind: CancelCmd, ID: "t1", Now: ts})
					ok = e == nil
				} else {
					_, _, e := m4.Step(Cmd{Kind: CompleteCmd, ID: "t1", Now: ts,
						WorkerID: "w1", LeaseToken: "tok", Payload: json.RawMessage(`{"r":1}`)})
					ok = e == nil
				}
				if a == "complete" {
					stepCompleteOK = ok
				}
			}
			finalB := m4.Get("t1")
			if finalB.State != wantState {
				t.Fatalf("lazy-expiry model state = %s, want %s", finalB.State, wantState)
			}
			if stepCompleteOK != completeOK {
				t.Fatalf("lazy-expiry model complete accepted=%v, want %v",
					stepCompleteOK, completeOK)
			}
			if string(finalB.Result) != string(final.Result) {
				t.Fatalf("lazy-expiry model result = %s, want %s", finalB.Result, final.Result)
			}
		})
	}
}

// TestSweepVariantEquivalent explicitly sweeps at the timeout slot. The
// outcome must match lazy expiry (the two ways a timeout can linearize are
// indistinguishable to clients).
func TestSweepVariantEquivalent(t *testing.T) {
	actions := []string{"cancel", "complete", "timeout"}
	for _, perm := range permutations(actions) {
		m, now := prepareRunning(t)
		d := now.Add(10 * time.Second)
		timeoutSlot := indexOf(perm, "timeout")
		for i, a := range perm {
			ts := d.Add(time.Duration(i-timeoutSlot) * time.Second)
			if a == "timeout" {
				_ = m.ExpireDue(ts)
				continue
			}
			if a == "cancel" {
				_, _, _ = m.Step(Cmd{Kind: CancelCmd, ID: "t1", Now: ts})
			} else {
				_, _, _ = m.Step(Cmd{Kind: CompleteCmd, ID: "t1", Now: ts,
					WorkerID: "w1", LeaseToken: "tok", Payload: json.RawMessage(`{"r":1}`)})
			}
		}
		got := m.Get("t1")
		want := Cancelled
		if perm[0] == "complete" {
			want = Completed
		}
		if got.State != want {
			t.Fatalf("perm %v: sweep model state = %s, want %s", perm, got.State, want)
		}
	}
}

// TestTimeoutThenReclaimOldWorkerRejected: after a timeout the task is
// redispatched (attempt 2, new token); every result/heartbeat carrying the
// attempt-1 token must be refused, while the new lease owner can complete.
func TestTimeoutThenReclaimOldWorkerRejected(t *testing.T) {
	m, now := prepareRunning(t)
	d := now.Add(10 * time.Second)

	_ = m.ExpireDue(d)
	if got := m.Get("t1"); got.Attempt != 1 || got.State != Pending {
		t.Fatalf("post-timeout = %+v", got)
	}
	t2, _, err := m.Apply(Cmd{Kind: ClaimCmd, Now: d.Add(time.Second), WorkerID: "w2"})
	if err != nil || t2.Attempt != 2 || t2.LeaseToken != "tok2" {
		t.Fatalf("reclaim: %v %+v", err, t2)
	}

	// Old worker w1 with the old token: complete rejected.
	if _, _, e := m.Apply(Cmd{Kind: CompleteCmd, ID: "t1", Now: d.Add(2 * time.Second),
		WorkerID: "w1", LeaseToken: "tok", Payload: json.RawMessage(`{"old":true}`)}); e == nil || e.Code != CodeStaleLease {
		t.Fatalf("old-worker complete after reclaim err = %v", e)
	}
	// Old worker heartbeat rejected as well.
	if _, _, e := m.Apply(Cmd{Kind: HeartbeatCmd, ID: "t1", Now: d.Add(2 * time.Second),
		WorkerID: "w1", LeaseToken: "tok"}); e == nil || e.Code != CodeStaleLease {
		t.Fatalf("old-worker heartbeat after reclaim err = %v", e)
	}
	// Even w1 guessing the new token string but using its own worker id fails.
	if _, _, e := m.Apply(Cmd{Kind: CompleteCmd, ID: "t1", Now: d.Add(2 * time.Second),
		WorkerID: "w1", LeaseToken: "tok2", Payload: json.RawMessage(`{}`)}); e == nil || e.Code != CodeStaleLease {
		t.Fatalf("w1 with new token err = %v", e)
	}
	// Current owner w2 completes successfully.
	final, _, e := m.Apply(Cmd{Kind: CompleteCmd, ID: "t1", Now: d.Add(3 * time.Second),
		WorkerID: "w2", LeaseToken: "tok2", Payload: json.RawMessage(`{"new":true}`)})
	if e != nil || final.State != Completed || string(final.Result) != `{"new":true}` {
		t.Fatalf("new owner complete: %v %+v", e, final)
	}
}

// TestLongerInterleavings adds a second cancel/complete/timeout-style event
// (heartbeat renewal) to cover 4! short sequences around a renewed lease.
func TestLongerInterleavings(t *testing.T) {
	actions := []string{"cancel", "complete", "timeout", "heartbeat"}
	for _, perm := range permutations(actions) {
		m, now := prepareRunning(t)
		d := now.Add(10 * time.Second)
		hbExtended := false
		completeOK := false
		for _, a := range perm {
			switch a {
			case "cancel":
				_, _, _ = m.Apply(Cmd{Kind: CancelCmd, ID: "t1", Now: d})
			case "complete":
				_, _, e := m.Apply(Cmd{Kind: CompleteCmd, ID: "t1", Now: d,
					WorkerID: "w1", LeaseToken: "tok", Payload: json.RawMessage(`{"r":1}`)})
				completeOK = e == nil
			case "heartbeat":
				// A heartbeat at d-1 extends the deadline to d-1+10 = d+9.
				if _, _, e := m.Apply(Cmd{Kind: HeartbeatCmd, ID: "t1", Now: d.Add(-1 * time.Second),
					WorkerID: "w1", LeaseToken: "tok"}); e == nil {
					hbExtended = true
				}
			case "timeout":
				// If the heartbeat already landed, expiry at d must be a no-op.
				_ = m.ExpireDue(d)
			}
		}
		got := m.Get("t1")

		// Reference outcome from the permutation order:
		first := perm[0]
		switch {
		case first == "complete":
			if got.State != Completed || !completeOK {
				t.Fatalf("perm %v: state=%s completeOK=%v, want COMPLETED/accepted", perm, got.State, completeOK)
			}
		case first == "cancel":
			if got.State != Cancelled {
				t.Fatalf("perm %v: state=%s want CANCELLED", perm, got.State)
			}
		case first == "heartbeat":
			// Lease extended; timeout at d is a no-op. Winner between the
			// remaining cancel/complete is whoever comes first among them.
			hbExtended = true
			rest := perm[1:]
			winner := rest[0]
			if winner == "timeout" { // timeout ineffective; look past it
				winner = rest[1]
			}
			if winner == "complete" {
				if got.State != Completed || !completeOK {
					t.Fatalf("perm %v: state=%s completeOK=%v want COMPLETED", perm, got.State, completeOK)
				}
			} else {
				if got.State != Cancelled {
					t.Fatalf("perm %v: state=%s want CANCELLED", perm, got.State)
				}
			}
		case first == "timeout":
			// Timeout at d wins only if the heartbeat has not happened yet.
			// It did not (timeout first), so task goes PENDING; cancel later
			// always cancels. Complete must be rejected in every such perm.
			if completeOK {
				t.Fatalf("perm %v: stale complete unexpectedly accepted", perm)
			}
			if got.State != Cancelled {
				t.Fatalf("perm %v: state=%s want CANCELLED", perm, got.State)
			}
		}
		if hbExtended && first == "heartbeat" && got.LeaseToken != "" && got.State == Completed {
			// completed tasks clear the lease — this branch can't hold; kept
			// as documentation of intent.
		}
	}
}

func indexOf(xs []string, want string) int {
	for i, x := range xs {
		if x == want {
			return i
		}
	}
	return -1
}

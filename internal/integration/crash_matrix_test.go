package integration

import (
	"net/http"
	"testing"
	"time"
)

func TestHappyPathCommit(t *testing.T) {
	cl := newCluster(t)

	code, resp := postJSON(t, cl.c.url()+"/txn", map[string]any{
		"txid":   "T1",
		"writes": cl.commitWrites("T1"),
	})
	if code != http.StatusOK || resp["status"] != "COMMITTED" {
		t.Fatalf("submit = %d %v, want 200 COMMITTED", code, resp)
	}
	if got := kv(cl, cl.p1, "k1"); got != "v1-T1" {
		t.Fatalf("p1/k1 = %q", got)
	}
	if got := kv(cl, cl.p2, "k3"); got != "v3-T1" {
		t.Fatalf("p2/k3 = %q", got)
	}
	for _, p := range []*node{cl.p1, cl.p2} {
		code, _ := postJSON(t, p.url()+"/recover", map[string]any{})
		if code != 200 {
			t.Fatalf("%s recover = %d", p.name, code)
		}
	}
}

func TestVoteNoAbortsEntireTxn(t *testing.T) {
	cl := newCluster(t)

	// p2 votes no by preparing a key that a live holder already locked; the
	// whole txn must abort everywhere, including p1 which prepares valid keys.
	code, resp := postJSON(t, cl.p2.url()+"/prepare", map[string]any{
		"txid":   "HOLDER",
		"writes": []map[string]string{{"key": "k3", "value": "held"}},
	})
	if code != 200 || resp["vote"] != "yes" {
		t.Fatalf("holder prepare = %d %v", code, resp)
	}

	code, resp = postJSON(t, cl.c.url()+"/txn", map[string]any{
		"txid": "T1",
		"writes": []map[string]string{
			{"participant": "p1", "key": "k1", "value": "v1"},
			{"participant": "p2", "key": "k3", "value": "v3"},
		},
	})
	if code != http.StatusOK || resp["status"] != "ABORTED" {
		t.Fatalf("submit = %d %v, want 200 ABORTED", code, resp)
	}
	if got := kv(cl, cl.p1, "k1"); got != "" {
		t.Fatalf("p1/k1 = %q, want empty (aborted txn must not apply)", got)
	}
	code, st := getJSON(t, cl.p1.url()+"/txn/T1")
	if code != 200 || st["status"] != "ABORTED" {
		t.Fatalf("p1 txn T1 = %d %v, want ABORTED", code, st)
	}
}

// crashCase pins every fsync boundary. For each point we assert the atomic
// outcome after restart + recovery: never some participants committed and
// others rolled back.
type crashCase struct {
	name    string
	armedOn *node
	point   string
	wantAll string // "COMMIT" or "ABORT": the single global outcome
}

func TestCrashAtEveryDurableBoundary(t *testing.T) {
	cases := []crashCase{
		{name: "C1 begin-logged", armedOn: nil /*set per run*/, point: "C1", wantAll: "ABORT"},
		{name: "C2 decision-logged commit", point: "C2", wantAll: "COMMIT"},
		{name: "C3 done-logged", point: "C3", wantAll: "COMMIT"},
		{name: "P1 prepare-logged before vote", point: "P1", wantAll: "ABORT"},
		{name: "P2 commit-logged before ack", point: "P2", wantAll: "COMMIT"},
		{name: "P3 abort-logged before ack", point: "P3", wantAll: "ABORT"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) { runCrashCase(t, tc) })
	}
}

func runCrashCase(t *testing.T, tc crashCase) {
	cl := newCluster(t)

	// P3 sits on the abort path: force a no-vote from p2 via lock conflict.
	// All other cases run the normal commit-path writes.
	writes := cl.commitWrites("X")
	armed := cl.c
	if tc.point == "P1" || tc.point == "P2" {
		armed = cl.p1
	}
	if tc.point == "P3" {
		armed = cl.p2
		// Arrange global ABORT with p2 holding a durable PREPARE for X:
		// p1 holds k1 (votes no on X), while p2 has prepared k3 for X
		// directly (the same state it would be in after a yes vote). The
		// coordinator's abort of X then reaches p2's P3 point.
		code, resp := postJSON(t, cl.p1.url()+"/prepare", map[string]any{
			"txid":   "HOLDER",
			"writes": []map[string]string{{"key": "k1", "value": "held"}},
		})
		if code != 200 || resp["vote"] != "yes" {
			t.Fatalf("holder prepare = %d %v", code, resp)
		}
		code, resp = postJSON(t, cl.p2.url()+"/prepare", map[string]any{
			"txid":   "X",
			"writes": []map[string]string{{"key": "k3", "value": "v3-X"}},
		})
		if code != 200 || resp["vote"] != "yes" {
			t.Fatalf("p2 pre-prepare X = %d %v", code, resp)
		}
	}

	// Arm and submit; the armed node exits 42 mid-request.
	armed.restart(tc.point)
	if tc.point == "P3" {
		// p2 only crashes once phase-2 /abort reaches it, which happens after
		// the coordinator collects phase-1 votes (p2 votes no on the lock
		// conflict). submit returns quickly with ABORT_PENDING because the
		// connection died; wait explicitly for p2 to crash.
		go func() {
			_, _ = postExpectError(t, cl.c.url()+"/txn", map[string]any{"txid": "X", "writes": writes})
		}()
		waitForExit(t, armed)
	} else {
		_, _ = postExpectError(t, cl.c.url()+"/txn", map[string]any{"txid": "X", "writes": writes})
		waitForExit(t, armed)
	}

	// P1 case: p1 died before voting, so the coordinator's submit call is
	// still waiting on phase 1. Give the coordinator its abort decision by
	// restarting p1 and letting phase 1 fail + phase 2 abort run.
	switch tc.point {
	case "P1":
		cl.p1.restart("")
		// Coordinator phase1 already failed with unreachable; it logged ABORT
		// and is retrying phase-2 delivery. Restart clean, trigger recovery.
		waitForTxnStatus(t, cl.c, "X", "ABORT_COMPLETE", 15*time.Second)
	case "P2":
		cl.p1.restart("")
		waitForTxnStatus(t, cl.c, "X", "COMMIT_COMPLETE", 15*time.Second)
	case "P3":
		cl.p2.restart("")
		waitForTxnStatus(t, cl.c, "X", "ABORT_COMPLETE", 15*time.Second)
	case "C1", "C2", "C3":
		cl.c.restart("")
		want := "COMMIT_COMPLETE"
		if tc.point == "C1" {
			want = "ABORT_COMPLETE"
		}
		waitForTxnStatus(t, cl.c, "X", want, 15*time.Second)
	}

	// Explicit sweep in case the background loop timing needs a nudge.
	postJSON(t, cl.c.url()+"/recover", map[string]any{})

	assertUniformOutcome(t, cl, tc.wantAll)
}

func assertUniformOutcome(t *testing.T, cl *cluster, want string) {
	t.Helper()

	if want == "COMMIT" {
		if got := kv(cl, cl.p1, "k1"); got != "v1-X" {
			t.Errorf("p1/k1 = %q, want v1-X (global COMMIT)", got)
		}
		if got := kv(cl, cl.p1, "k2"); got != "v2-X" {
			t.Errorf("p1/k2 = %q, want v2-X", got)
		}
		if got := kv(cl, cl.p2, "k3"); got != "v3-X" {
			t.Errorf("p2/k3 = %q, want v3-X", got)
		}
	} else {
		for key, p := range map[string]*node{"k1": cl.p1, "k2": cl.p1, "k3": cl.p2} {
			if got := kv(cl, p, key); got != "" {
				t.Errorf("%s/%s = %q, want empty (global ABORT)", p.name, key, got)
			}
		}
	}

	// No participant may remain in the opposite terminal state.
	for _, p := range []*node{cl.p1, cl.p2} {
		code, st := getJSON(t, p.url()+"/txn/X")
		if code == 404 {
			// Unknown = never prepared; only legitimate on the ABORT path for
			// participants that voted no/never prepared.
			if want == "COMMIT" {
				t.Errorf("%s has no txn X under global COMMIT", p.name)
			}
			continue
		}
		state, _ := st["status"].(string)
		if want == "COMMIT" && state != "COMMITTED" {
			t.Errorf("%s txn X = %s, want COMMITTED", p.name, state)
		}
		if want == "ABORT" && state != "ABORTED" {
			// A no-vote participant may legitimately have ABORTED status.
			t.Errorf("%s txn X = %s, want ABORTED", p.name, state)
		}
		if st["blocked"] == true {
			t.Errorf("%s txn X still blocked after recovery completed", p.name)
		}
	}

	// Locks held by transaction X must all be gone after it terminates.
	// (Test fixtures such as P3's HOLDER are intentionally left prepared.)
	for _, p := range []*node{cl.p1, cl.p2} {
		code, st := getJSON(t, p.url()+"/txn/X")
		if code == 200 && st["blocked"] == true {
			t.Fatalf("%s txn X still blocked", p.name)
		}
		_, st = getJSON(t, p.url()+"/kv/"+mapKey(p))
		if locked, _ := st["locked"].(bool); locked {
			if holder, _ := st["locked_by"].(string); holder == "X" {
				t.Errorf("%s still holds X's lock after recovery", p.name)
			}
		}
	}
}

func mapKey(p *node) string {
	if p.name == "p2" {
		return "k3"
	}
	return "k1"
}

func waitForTxnStatus(t *testing.T, n *node, txid, want string, timeout time.Duration) {
	t.Helper()
	waitFor(t, n.url()+"/txn/"+txid, func(m map[string]any) bool {
		return m["status"] == want
	}, timeout)
}

func waitForExit(t *testing.T, n *node) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if !n.alive() {
			n.reap()
			n.stopped = true
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("node %s did not exit at crash point\nlogs:\n%s", n.name, n.logBuf.String())
}

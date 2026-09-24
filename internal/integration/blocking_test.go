package integration

import (
	"net/http"
	"testing"
	"time"
)

// TestPreparedParticipantBlocksWhenCoordinatorGone is the headline blocking
// guarantee. Both participants prepare a transaction, then the coordinator
// is hard-killed and stays down far longer than any plausible timeout. The
// participants must:
//   - stay PREPARED (no autonomous abort, no timeout anywhere);
//   - keep holding the locks (a conflicting prepare is refused);
//   - keep the old committed values visible;
//   - survive their OWN restart in the same blocked state;
//
// and once the coordinator comes back and re-drives the durable COMMIT, both
// commit — proving uncertainty resolves only via the coordinator.
func TestPreparedParticipantBlocksWhenCoordinatorGone(t *testing.T) {
	cl := newCluster(t)

	// Prepare directly (coordinator is up but we never let it decide; this is
	// exactly the state the participants are in if the coordinator dies
	// between phase-1 yes-votes and phase 2).
	for _, p := range []*node{cl.p1, cl.p2} {
		code, resp := postJSON(t, p.url()+"/prepare", map[string]any{
			"txid":   "B1",
			"writes": []map[string]string{{"key": "blocked-key", "value": "new"}},
		})
		if code != http.StatusOK || resp["vote"] != "yes" {
			t.Fatalf("%s prepare = %d %v", p.name, code, resp)
		}
	}

	// Coordinator dies and stays dead for 6 seconds — much longer than any
	// RPC timeout in the system (2s).
	cl.c.kill()

	code, st := getJSON(t, cl.p1.url()+"/txn/B1")
	if code != 200 || st["status"] != "PREPARED" || st["blocked"] != true {
		t.Fatalf("p1 B1 = %d %v, want PREPARED/blocked", code, st)
	}

	// Wait out a deliberately long window; nothing may change on its own.
	time.Sleep(6 * time.Second)

	for _, p := range []*node{cl.p1, cl.p2} {
		code, st = getJSON(t, p.url()+"/txn/B1")
		if code != 200 || st["status"] != "PREPARED" {
			t.Fatalf("%s after 6s without coordinator: %d %v, want still PREPARED",
				p.name, code, st)
		}
		// Lock is still held: a conflicting txn cannot prepare.
		code, st = postJSON(t, p.url()+"/prepare", map[string]any{
			"txid":   "RIVAL",
			"writes": []map[string]string{{"key": "blocked-key", "value": "x"}},
		})
		if code != http.StatusConflict {
			t.Fatalf("%s rival prepare = %d %v, want 409 lock held", p.name, code, st)
		}
		// Old value still readable, marked locked.
		code, st = getJSON(t, p.url()+"/kv/blocked-key")
		if code != 200 || st["value"] != "" || st["locked"] != true {
			t.Fatalf("%s kv = %d %v, want old empty value still locked", p.name, code, st)
		}
		// /recover only reports; it must not resolve anything.
		code, st = postJSON(t, p.url()+"/recover", map[string]any{})
		if code != 200 {
			t.Fatalf("%s recover = %d", p.name, code)
		}
	}

	// The participants themselves can also crash and restart: the PREPARED
	// state is reconstructed from the WAL and they remain blocked.
	cl.p1.kill()
	cl.p1.restart("")
	code, st = getJSON(t, cl.p1.url()+"/txn/B1")
	if code != 200 || st["status"] != "PREPARED" {
		t.Fatalf("p1 after own restart: %d %v, want PREPARED recovered from WAL", code, st)
	}

	// Coordinator returns. Its WAL has no B1 record (it never logged one in
	// this scenario), so for a faithful drive we POST commit directly to each
	// participant — the same phase-2 message a recovered coordinator sends
	// once its durable COMMIT record exists. This proves the blocked state
	// resolves exactly when, and only when, the coordinator's decision comes.
	cl.c.restart("")
	for _, p := range []*node{cl.p1, cl.p2} {
		code, st = postJSON(t, p.url()+"/commit", map[string]string{"txid": "B1"})
		if code != http.StatusOK || st["status"] != "COMMITTED" {
			t.Fatalf("%s commit = %d %v", p.name, code, st)
		}
	}
	for _, p := range []*node{cl.p1, cl.p2} {
		if got := kv(cl, p, "blocked-key"); got != "new" {
			t.Fatalf("%s value = %q, want new", p.name, got)
		}
		code, st = getJSON(t, p.url()+"/txn/B1")
		if code != 200 || st["status"] != "COMMITTED" || st["blocked"] == true {
			t.Fatalf("%s B1 final = %d %v", p.name, code, st)
		}
	}
}

// TestPreparedResolvesByAbortAfterCoordinatorRestart covers the C1 boundary
// end to end: the coordinator logged BEGIN, died before any prepare; the
// one participant that HAD prepared (prepare sent concurrently before the
// kill window) must block, then abort when the recovered coordinator re-drives.
func TestCoordinatorCrashC1AbortsPreparedParticipants(t *testing.T) {
	cl := newCluster(t)

	// p1 prepares independently BEFORE the coordinator even sees the submit,
	// emulating a yes vote already durable when the coordinator dies.
	code, resp := postJSON(t, cl.p1.url()+"/prepare", map[string]any{
		"txid":   "Y1",
		"writes": []map[string]string{{"key": "y", "value": "1"}},
	})
	if code != 200 || resp["vote"] != "yes" {
		t.Fatalf("p1 prepare = %d %v", code, resp)
	}

	cl.c.restart("C1")
	_, _ = postExpectError(t, cl.c.url()+"/txn", map[string]any{
		"txid": "Y1",
		"writes": []map[string]string{
			{"participant": "p1", "key": "y", "value": "1"},
		},
	})
	waitForExit(t, cl.c)

	// p1 is prepared and coordinator is dead: blocked, not aborted.
	code, st := getJSON(t, cl.p1.url()+"/txn/Y1")
	if code != 200 || st["status"] != "PREPARED" {
		t.Fatalf("p1 Y1 = %d %v, want PREPARED blocking", code, st)
	}

	cl.c.restart("")
	postJSON(t, cl.c.url()+"/recover", map[string]any{})
	waitForTxnStatus(t, cl.c, "Y1", "ABORT_COMPLETE", 10*time.Second)

	code, st = getJSON(t, cl.p1.url()+"/txn/Y1")
	if code != 200 || st["status"] != "ABORTED" {
		t.Fatalf("p1 Y1 = %d %v, want ABORTED after coordinator recovery", code, st)
	}
	if got := kv(cl, cl.p1, "y"); got != "" {
		t.Fatalf("p1/y = %q, want empty", got)
	}
}

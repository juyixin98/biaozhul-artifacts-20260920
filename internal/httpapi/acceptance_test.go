package httpapi_test

import (
	"net/http"
	"testing"
)

// TestAcceptanceLifecycle runs the required acceptance scenario end to end:
//
//  1. writes before migration (old primary, route v1)
//  2. SNAPSHOT phase: more writes + target disconnect/reconnect
//  3. snapshot-complete, including a duplicated control message (idempotent)
//  4. CATCHUP phase: writes, target disconnect (write not delivered),
//     reconnect, explicit catchup flush, duplicate catchup message
//  5. switch refused while disconnected; reconnect; switch succeeds;
//     duplicate switch message replays the same result
//  6. after cut-over: old primary (stale route v1) writes are rejected,
//     new primary confirms at route v2, old primary never confirms again
//  7. all confirmed writes present, seq dense, audit healthy (no dual primary)
func TestAcceptanceLifecycle(t *testing.T) {
	ts := newTestServer(t)

	// Step 1: pre-migration writes on node-a (seq 1,2).
	mustWrite(ts, 1, "pre-1")
	mustWrite(ts, 1, "pre-2")

	// Idempotent data-plane retry: same key returns the same seq, no new write.
	w1 := writeAt(ts, 1, "idem-1", "client-key-9")
	w1.requireOK()
	if asInt(w1.dataMap(), "seq") != 3 {
		t.Fatalf("first idem write seq = %v, want 3", w1.dataMap()["seq"])
	}
	w1rep := writeAt(ts, 1, "idem-1", "client-key-9")
	w1rep.requireOK()
	if asInt(w1rep.dataMap(), "seq") != 3 || !w1rep.replay() {
		t.Fatalf("idempotent retry did not replay seq 3: %+v", w1rep.body)
	}

	// Step 2: start migration with a control idempotency key.
	start := controlKeyed(ts, "start", "ctrl-start-1")
	start.requireOK()
	if start.dataMap()["target_id"] != "node-b" || start.dataMap()["phase"] != "SNAPSHOT" {
		t.Fatalf("bad start response: %+v", start.body)
	}
	// Duplicate START control message: same key -> replay, phase untouched.
	startDup := controlKeyed(ts, "start", "ctrl-start-1")
	startDup.requireOK()
	if !startDup.replay() {
		t.Fatalf("duplicate START with same key was not a replay: %+v", startDup.body)
	}
	// Bare duplicate START (no key, wrong phase) must be rejected.
	if r := control(ts, "start", map[string]any{"target_id": "node-b"}); r.status != http.StatusConflict {
		t.Fatalf("duplicate START in SNAPSHOT: status=%d body=%s, want 409", r.status, r.raw)
	}

	// SNAPSHOT-phase writes keep confirming on old primary (seq 4,5).
	mustWrite(ts, 1, "snap-write-1")
	mustWrite(ts, 1, "snap-write-2")

	// Target disconnects mid-snapshot; snapshot-complete must fail while down.
	if r := ts.call("POST", "/nodes/node-b/disconnect", map[string]any{}, nil); r.status != http.StatusOK {
		t.Fatalf("disconnect node-b: %d %s", r.status, r.raw)
	}
	if r := control(ts, "snapshot-complete", nil); r.status != http.StatusConflict {
		t.Fatalf("snapshot-complete while target down: status=%d, want 409", r.status)
	}
	// Old primary still accepts writes during the partition (seq 6).
	mustWrite(ts, 1, "snap-write-during-partition")
	// Reconnect; snapshot completes and bulk-loads seq 1..3 (start_seq=3).
	if r := ts.call("POST", "/nodes/node-b/reconnect", map[string]any{}, nil); r.status != http.StatusOK {
		t.Fatalf("reconnect node-b: %d %s", r.status, r.raw)
	}
	snap := controlKeyed(ts, "snapshot-complete", "ctrl-snap-1")
	snap.requireOK()
	if snap.dataMap()["phase"] != "CATCHUP" || asInt(snap.dataMap(), "loaded_write_count") != 3 {
		t.Fatalf("bad snapshot-complete: %+v", snap.body)
	}
	// Duplicated snapshot-complete control message: idempotent replay.
	snapDup := controlKeyed(ts, "snapshot-complete", "ctrl-snap-1")
	snapDup.requireOK()
	if !snapDup.replay() || snapDup.dataMap()["phase"] != "CATCHUP" {
		t.Fatalf("duplicate snapshot-complete not a replay: %+v", snapDup.body)
	}
	// Same key reused for a different action -> 409 conflict.
	if r := controlKeyed(ts, "catchup", "ctrl-snap-1"); r.status != http.StatusConflict {
		t.Fatalf("cross-action key reuse: status=%d, want 409", r.status)
	}

	// Step 4: CATCHUP. Write while both up: streamed immediately (seq 7).
	mustWrite(ts, 1, "catchup-write-1")

	// Target disconnects: primary confirms seq 8 but target misses it.
	if r := ts.call("POST", "/nodes/node-b/disconnect", map[string]any{}, nil); r.status != http.StatusOK {
		t.Fatalf("disconnect: %d", r.status)
	}
	missed := mustWrite(ts, 1, "catchup-write-while-down")
	if asInt(missed, "seq") != 8 {
		t.Fatalf("seq = %v, want 8", missed["seq"])
	}
	if dlv, _ := missed["delivered_to"].([]any); len(dlv) != 1 || dlv[0] != "node-a" {
		t.Fatalf("write during target disconnect leaked to target: %v", missed["delivered_to"])
	}
	// Switch must be refused: target lagging AND disconnected.
	if r := control(ts, "switch", nil); r.status != http.StatusConflict {
		t.Fatalf("switch while target down/lagging: status=%d, want 409", r.status)
	}
	if r := ts.call("POST", "/nodes/node-b/reconnect", map[string]any{}, nil); r.status != http.StatusOK {
		t.Fatalf("reconnect: %d", r.status)
	}
	// Still lagging immediately after reconnect: switch refused (lag_remaining).
	if r := control(ts, "switch", nil); r.status != http.StatusConflict {
		t.Fatalf("switch while lagging: status=%d, want 409", r.status)
	}
	// Explicit catchup flushes seq 8 to the target.
	fl := controlKeyed(ts, "catchup", "ctrl-catchup-1")
	fl.requireOK()
	if asInt(fl.dataMap(), "flushed_writes") != 1 || fl.dataMap()["caught_up"] != true {
		t.Fatalf("bad catchup response: %+v", fl.body)
	}
	// Duplicated catchup control message: replay reports 0 new flushes via cache
	// (same cached result), and stays idempotent.
	flDup := controlKeyed(ts, "catchup", "ctrl-catchup-1")
	flDup.requireOK()
	if !flDup.replay() {
		t.Fatalf("duplicate catchup not a replay: %+v", flDup.body)
	}

	// Step 5: switch succeeds at zero lag (atomic cut-over).
	sw := controlKeyed(ts, "switch", "ctrl-switch-1")
	sw.requireOK()
	d := sw.dataMap()
	if d["old_primary_id"] != "node-a" || d["new_primary_id"] != "node-b" ||
		asInt(d, "switch_seq") != 8 || asInt(d, "new_route_ver") != 2 || d["phase"] != "SWITCHED" {
		t.Fatalf("bad switch response: %+v", d)
	}
	// Duplicated SWITCH control message: replays the identical result, no state change.
	swDup := controlKeyed(ts, "switch", "ctrl-switch-1")
	swDup.requireOK()
	if !swDup.replay() || asInt(swDup.dataMap(), "switch_seq") != 8 {
		t.Fatalf("duplicate switch replay mismatch: %+v", swDup.body)
	}

	// Step 6: post cut-over routing behaviour.
	// Old client still on route v1 -> rejected as stale, write NOT confirmed.
	stale := writeAt(ts, 1, "stale-after-switch", "")
	if stale.status != http.StatusConflict {
		t.Fatalf("stale-route write after switch: status=%d, want 409", stale.status)
	}
	if stale.body["error_code"] != "stale_route" {
		t.Fatalf("stale write error_code=%v, want stale_route", stale.body["error_code"])
	}
	// Old primary disconnected to prove the new primary is the sole confirmer:
	// route v2 writes still succeed while node-a is down.
	if r := ts.call("POST", "/nodes/node-a/disconnect", map[string]any{}, nil); r.status != http.StatusOK {
		t.Fatalf("disconnect old primary: %d", r.status)
	}
	newW := mustWrite(ts, 2, "after-switch-new-primary")
	if newW["primary"] != "node-b" || asInt(newW, "route_ver") != 2 || asInt(newW, "seq") != 9 {
		t.Fatalf("post-switch write wrong attribution: %+v", newW)
	}
	// While new primary is the only primary, a v1 write during old-primary down
	// must fail the route check (not somehow confirm on node-a).
	stale2 := writeAt(ts, 1, "stale-while-old-down", "")
	if stale2.status != http.StatusConflict {
		t.Fatalf("stale v1 write with old primary down: status=%d, want 409", stale2.status)
	}
	if r := ts.call("POST", "/nodes/node-a/reconnect", map[string]any{}, nil); r.status != http.StatusOK {
		t.Fatalf("reconnect old primary: %d", r.status)
	}
	// Old primary back up but route v1 still rejected: no dual confirmation.
	stale3 := writeAt(ts, 1, "stale-old-primary-alive", "")
	if stale3.status != http.StatusConflict {
		t.Fatalf("old primary alive but stale v1 write accepted: %d", stale3.status)
	}
	mustWrite(ts, 2, "after-switch-new-primary-2") // seq 10

	// Step 7: verify all confirmed writes present + no dual primary.
	aud := auditShard(ts)
	checks := map[string]int{
		"confirmed_count":                    asInt(aud, "confirmed_count"),
		"missing_confirmed_writes":           asInt(aud, "missing_confirmed_writes"),
		"sequence_gaps":                      asInt(aud, "sequence_gaps"),
		"multi_confirmer_writes":             asInt(aud, "multi_confirmer_writes"),
		"old_primary_confirms_after_switch":  asInt(aud, "old_primary_confirms_after_switch"),
		"new_primary_confirms_before_switch": asInt(aud, "new_primary_confirms_before_switch"),
		"stale_route_confirms":               asInt(aud, "stale_route_confirms"),
	}
	if checks["confirmed_count"] != 10 {
		t.Errorf("confirmed_count=%d want 10 (stale writes must not confirm)", checks["confirmed_count"])
	}
	for name, v := range checks {
		if name == "confirmed_count" {
			continue
		}
		if v != 0 {
			t.Errorf("audit %s=%d want 0 (full audit: %+v)", name, v, aud)
		}
	}
	if aud["healthy"] != true {
		t.Errorf("audit not healthy: %+v", aud)
	}

	// Independently inspect the write log: seq 1..8 confirmed by node-a,
	// seq 9..10 by node-b; boundary seq=8 exactly.
	wr := ts.call("GET", "/shards/orders/writes", nil, nil)
	wr.requireOK()
	list, _ := wr.dataMap()["writes"].([]any)
	if len(list) != 10 {
		t.Fatalf("write log length=%d want 10", len(list))
	}
	for i, item := range list {
		w := item.(map[string]any)
		if asInt(w, "seq") != i+1 {
			t.Errorf("write[%d] seq=%v want %d", i, w["seq"], i+1)
		}
		wantPrimary := "node-a"
		if i+1 > 8 {
			wantPrimary = "node-b"
		}
		if w["primary"] != wantPrimary {
			t.Errorf("write seq=%d primary=%v want %s", i+1, w["primary"], wantPrimary)
		}
	}
}

// TestPrimaryDownRejectsWrites verifies writes against a disconnected primary
// fail with 503 and are counted as rejects (no phantom confirmation).
func TestPrimaryDownRejectsWrites(t *testing.T) {
	ts := newTestServer(t)
	mustWrite(ts, 1, "before-down")
	if r := ts.call("POST", "/nodes/node-a/disconnect", map[string]any{}, nil); r.status != 200 {
		t.Fatalf("disconnect: %d", r.status)
	}
	r := writeAt(ts, 1, "while-down", "")
	if r.status != http.StatusServiceUnavailable || r.body["error_code"] != "primary_unavailable" {
		t.Fatalf("write to down primary: status=%d body=%s", r.status, r.raw)
	}
	if r := ts.call("POST", "/nodes/node-a/reconnect", map[string]any{}, nil); r.status != 200 {
		t.Fatalf("reconnect: %d", r.status)
	}
	w := mustWrite(ts, 1, "after-reconnect")
	if asInt(w, "seq") != 2 {
		t.Fatalf("seq after reconnect = %v want 2 (failed write must not consume seq)", w["seq"])
	}
}

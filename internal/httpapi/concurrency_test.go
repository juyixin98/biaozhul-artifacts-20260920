package httpapi_test

import (
	"fmt"
	"net/http"
	"sync"
	"testing"
	"time"
)

// TestConcurrentNoDualPrimary fires writes from a "v1 client" (old primary
// route) and a "v2 client" concurrently with the migration switch, and asserts
// that no write is ever confirmed by two primaries and the boundary is clean:
// every confirmed seq <= switch_seq is node-a@v1, every later seq is node-b@v2.
func TestConcurrentNoDualPrimary(t *testing.T) {
	ts := newTestServer(t)

	// Some pre-migration history.
	for i := 0; i < 5; i++ {
		mustWrite(ts, 1, fmt.Sprintf("pre-%d", i))
	}
	control(ts, "start", map[string]any{"target_id": "node-b"}).requireOK()
	mustWrite(ts, 1, "during-snapshot")
	control(ts, "snapshot-complete", nil).requireOK()
	for i := 0; i < 5; i++ {
		mustWrite(ts, 1, fmt.Sprintf("catchup-streamed-%d", i))
	}

	var wg sync.WaitGroup
	stop := make(chan struct{})
	var rejectedV1, acceptedV1, acceptedV2 int
	var mu sync.Mutex

	// Old-route clients keep trying v1 until the switch (and a little after).
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			default:
			}
			r := writeAt(ts, 1, fmt.Sprintf("v1-conc-%d", i), "")
			mu.Lock()
			if r.status == http.StatusOK {
				acceptedV1++
			} else {
				rejectedV1++
			}
			mu.Unlock()
		}
	}()

	// New-route clients attempt v2; they only succeed after the switch.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			default:
			}
			r := writeAt(ts, 2, fmt.Sprintf("v2-conc-%d", i), "")
			mu.Lock()
			if r.status == http.StatusOK {
				acceptedV2++
			}
			mu.Unlock()
		}
	}()

	// Let contention build, then perform the single atomic switch.
	time.Sleep(50 * time.Millisecond)
	sw := control(ts, "switch", nil)
	if sw.status != http.StatusOK {
		t.Fatalf("switch under concurrency: %d %s", sw.status, sw.raw)
	}
	switchSeq := asInt(sw.dataMap(), "switch_seq")
	time.Sleep(50 * time.Millisecond)
	close(stop)
	wg.Wait()

	if acceptedV1 == 0 || acceptedV2 == 0 || rejectedV1 == 0 {
		t.Fatalf("expected writers on both sides plus stale rejections: v1 accepted=%d rejected=%d, v2 accepted=%d",
			acceptedV1, rejectedV1, acceptedV2)
	}

	// Hard invariant: inspect every confirmed write against the switch boundary.
	aud := auditShard(ts)
	if aud["healthy"] != true {
		t.Fatalf("audit unhealthy under concurrency: %+v", aud)
	}
	if asInt(aud, "old_primary_confirms_after_switch") != 0 {
		t.Errorf("old primary confirmed writes after switch: %+v", aud)
	}
	if asInt(aud, "new_primary_confirms_before_switch") != 0 {
		t.Errorf("new primary confirmed writes before switch: %+v", aud)
	}
	if asInt(aud, "missing_confirmed_writes") != 0 || asInt(aud, "sequence_gaps") != 0 {
		t.Errorf("seq gaps under concurrency: %+v", aud)
	}

	wr := ts.call("GET", "/shards/orders/writes", nil, nil)
	wr.requireOK()
	list, _ := wr.dataMap()["writes"].([]any)
	for _, item := range list {
		w := item.(map[string]any)
		seq := asInt(w, "seq")
		primary, _ := w["primary"].(string)
		rv := asInt(w, "route_ver")
		switch {
		case seq <= switchSeq && primary != "node-a":
			t.Errorf("seq=%d before/at boundary confirmed by %s, want node-a", seq, primary)
		case seq <= switchSeq && rv != 1:
			t.Errorf("seq=%d before/at boundary route_ver=%d want 1", seq, rv)
		case seq > switchSeq && primary != "node-b":
			t.Errorf("seq=%d after boundary confirmed by %s, want node-b", seq, primary)
		case seq > switchSeq && rv != 2:
			t.Errorf("seq=%d after boundary route_ver=%d want 2", seq, rv)
		}
	}
}

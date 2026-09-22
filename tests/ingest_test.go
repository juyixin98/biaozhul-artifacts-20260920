package tests

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"testing"
	"time"
)

func baseEvent(id string, t time.Time) map[string]any {
	return map[string]any{
		"source_event_id": id,
		"db_user":         "alice",
		"occurred_at":     rfc(t),
		"action_category": "select",
		"schema_name":     "app",
		"table_name":      "orders",
		"row_count":       1,
		"client_ip":       "10.0.0.1",
		"sql_text":        "SELECT 1",
	}
}

// A conflict (same event id, different content) must reject the ENTIRE batch:
// no event from the batch may persist, and the stored count is unchanged.
func TestBatchConflictRollsBackEverything(t *testing.T) {
	h := Setup(t, "UTC")
	t0 := time.Date(2026, 3, 1, 10, 0, 0, 0, time.UTC)

	good := []map[string]any{
		baseEvent("e1", t0),
		baseEvent("e2", t0.Add(time.Second)),
	}
	h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": good})
	if got := h.EventCount(); got != 2 {
		t.Fatalf("count after first batch = %d, want 2", got)
	}

	// e3/e4 are brand new; e1 comes back with different content. The whole
	// batch must roll back — e3/e4 must NOT exist afterwards.
	bad := []map[string]any{
		baseEvent("e3", t0.Add(2*time.Second)),
		baseEvent("e4", t0.Add(3*time.Second)),
	}
	changed := baseEvent("e1", t0)
	changed["row_count"] = 999
	bad = append(bad, changed)

	rec, body := h.IngestEvents("src", bad)
	if rec.StatusCode != http.StatusConflict {
		t.Fatalf("status = %d, want 409: %v", rec.StatusCode, body)
	}
	if got := h.EventCount(); got != 2 {
		t.Fatalf("count after conflicting batch = %d, want 2 (full rollback)", got)
	}
}

// An exact replay is accepted as duplicates and never re-counted, and it must
// not trigger detection a second time.
func TestExactReplayIsIdempotent(t *testing.T) {
	h := Setup(t, "UTC")
	t0 := time.Date(2026, 3, 1, 10, 0, 0, 0, time.UTC)
	evs := []map[string]any{baseEvent("p1", t0)}

	res1 := h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": evs})
	if res1["inserted"].(float64) != 1 || res1["duplicates"].(float64) != 0 {
		t.Fatalf("first batch result = %v", res1)
	}
	res2 := h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": evs})
	if res2["inserted"].(float64) != 0 || res2["duplicates"].(float64) != 1 {
		t.Fatalf("replay result = %v", res2)
	}
	if got := h.EventCount(); got != 1 {
		t.Fatalf("count after replay = %d, want 1", got)
	}
}

// Concurrent retries of the same new batch must count every event exactly once.
func TestConcurrentRetriesNoDoubleCount(t *testing.T) {
	h := Setup(t, "UTC")
	t0 := time.Date(2026, 3, 1, 9, 0, 0, 0, time.UTC)
	var evs []map[string]any
	const n = 40
	for i := 0; i < n; i++ {
		evs = append(evs, baseEvent(fmt.Sprintf("c%03d", i), t0.Add(time.Duration(i)*time.Second)))
	}

	const goroutines = 12
	var wg sync.WaitGroup
	statuses := make(chan int, goroutines)
	for g := 0; g < goroutines; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			rec, _ := h.IngestEvents("src", evs)
			statuses <- rec.StatusCode
		}()
	}
	wg.Wait()
	close(statuses)
	for s := range statuses {
		if s != http.StatusOK {
			t.Fatalf("concurrent ingest status = %d, want 200", s)
		}
	}
	if got := h.EventCount(); got != n {
		t.Fatalf("count after concurrent retries = %d, want exactly %d", got, n)
	}
}

// Over-size batches and batches with duplicated ids inside the payload are 400.
func TestBatchLimits(t *testing.T) {
	h := Setup(t, "UTC")
	t0 := time.Date(2026, 3, 1, 8, 0, 0, 0, time.UTC)

	mk := func(n int, dup bool) []map[string]any {
		out := make([]map[string]any, n)
		for i := range out {
			id := fmt.Sprintf("z%05d", i)
			if dup && i == n-1 {
				id = "z00000"
			}
			out[i] = baseEvent(id, t0.Add(time.Duration(i)))
		}
		return out
	}

	rec, _ := h.IngestEvents("src", mk(2001, false))
	if rec.StatusCode != http.StatusBadRequest {
		t.Fatalf("2001 events: status = %d, want 400", rec.StatusCode)
	}
	rec, _ = h.IngestEvents("src", mk(50, true))
	if rec.StatusCode != http.StatusBadRequest {
		t.Fatalf("dup id in batch: status = %d, want 400", rec.StatusCode)
	}
	if got := h.EventCount(); got != 0 {
		t.Fatalf("count after rejected batches = %d, want 0", got)
	}
}

// The batch response serializes as documented.
func TestBatchResponseShape(t *testing.T) {
	h := Setup(t, "UTC")
	evs := []map[string]any{baseEvent("s1", time.Now().UTC())}
	body := h.MustStatus(http.MethodPost, "/v1/events:batch", h.Keys.Collector, http.StatusOK,
		map[string]any{"source": "src", "events": evs})
	for _, k := range []string{"batch_id", "received", "inserted", "duplicates", "alerts_created"} {
		if _, ok := body[k]; !ok {
			t.Fatalf("response missing %q: %s", k, mustJSON(body))
		}
	}
}

func mustJSON(v any) string { b, _ := json.Marshal(v); return string(b) }

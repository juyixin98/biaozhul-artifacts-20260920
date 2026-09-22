package tests

import (
	"fmt"
	"net/http"
	"testing"
	"time"
)

// TestBatchRollbackOnConflict: a batch containing one event whose id already
// exists with DIFFERENT content must roll back entirely — the valid events
// in the same batch must not persist either.
func TestBatchRollbackOnConflict(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("rollback")
	tk := env.seedOrg(t, slug, "UTC")
	orgID := env.orgID(t, slug)

	first := []batchEvent{{
		EventID: "dup-1", DbUser: "u1",
		OccurredAt: "2026-09-22T10:00:00Z", Action: "select",
		Table: "a", RowCount: 1,
	}}
	path, body := batchPayload(slug, "src1", first)
	st, out := env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, 200, out)

	// Second batch: same id, different row_count (content conflict) plus a
	// brand-new valid event. Both must be absent afterwards.
	second := []batchEvent{
		{EventID: "dup-1", DbUser: "u1",
			OccurredAt: "2026-09-22T10:00:00Z", Action: "select",
			Table: "a", RowCount: 999}, // changed
		{EventID: "fresh-1", DbUser: "u1",
			OccurredAt: "2026-09-22T10:01:00Z", Action: "insert",
			Table: "b", RowCount: 2},
	}
	path, body = batchPayload(slug, "src1", second)
	st, out = env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, http.StatusConflict, out)
	if out["code"] != "id_conflict" {
		t.Fatalf("code=%v, want id_conflict", out["code"])
	}
	if n := env.eventCount(t, orgID); n != 1 {
		t.Fatalf("events after conflict = %d, want 1 (batch must roll back wholly)", n)
	}
}

// TestBatchDedupRetransmit: identical retransmission (same source+id, same
// content) is counted once; concurrent duplicate batches must not double
// count either.
func TestBatchDedupRetransmit(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("dedup")
	tk := env.seedOrg(t, slug, "UTC")
	orgID := env.orgID(t, slug)

	evs := mkEvents("dedup", "u1", time.Date(2026, 9, 22, 10, 0, 0, 0, time.UTC), 50, 1)
	path, body := batchPayload(slug, "src1", evs)

	st, out := env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, 200, out)
	if got := int(out["received"].(float64)); got != 50 {
		t.Fatalf("received=%d want 50", got)
	}

	// Sequential retransmission.
	st, out = env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, 200, out)
	if got := int(out["duplicates"].(float64)); got != 50 {
		t.Fatalf("duplicates=%d want 50", got)
	}
	if got := int(out["received"].(float64)); got != 0 {
		t.Fatalf("received on replay=%d want 0", got)
	}
	if n := env.eventCount(t, orgID); n != 50 {
		t.Fatalf("events after replay = %d, want 50", n)
	}

	// Exact duplicate inside a single batch must collapse, not 409.
	dupBatch := append(append([]batchEvent{}, evs[:2]...), evs[0])
	path, body = batchPayload(slug, "src1", dupBatch)
	st, out = env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, 200, out)
	if n := env.eventCount(t, orgID); n != 50 {
		t.Fatalf("events after in-batch dup = %d, want 50", n)
	}
}

// TestConcurrentRetransmitNoDoubleCount fires the same new batch from
// several goroutines at once. Exactly one request inserts the events; the
// others must observe them as duplicates.
func TestConcurrentRetransmitNoDoubleCount(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("concur")
	tk := env.seedOrg(t, slug, "UTC")
	orgID := env.orgID(t, slug)

	evs := mkEvents("cc", "u1", time.Date(2026, 9, 22, 11, 0, 0, 0, time.UTC), 100, 1)
	path, body := batchPayload(slug, "src1", evs)
	raw := mustJSON(body)

	const N = 8
	type res struct{ status, received, dups int }
	results := make(chan res, N)
	errs := make(chan error, N)
	for i := 0; i < N; i++ {
		go func() {
			req := newJSONPost(env.srv.URL+path, raw, tk.Admin)
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				errs <- err
				return
			}
			defer resp.Body.Close()
			out := decodeBody(resp)
			results <- res{
				status:   resp.StatusCode,
				received: intOr(out["received"]),
				dups:     intOr(out["duplicates"]),
			}
		}()
	}
	var inserts, dupBatches, other int
	for i := 0; i < N; i++ {
		select {
		case err := <-errs:
			t.Fatalf("concurrent request: %v", err)
		case r := <-results:
			if r.status != http.StatusOK {
				other++
				continue
			}
			if r.received == 100 {
				inserts++
			}
			if r.dups == 100 {
				dupBatches++
			}
		}
	}
	if inserts != 1 {
		t.Fatalf("exactly one batch should insert 100, got %d insert-batches (others=%d)", inserts, other)
	}
	if inserts+dupBatches != N {
		t.Fatalf("insert=%d dup=%d do not sum to %d (other=%d)", inserts, dupBatches, N, other)
	}
	if n := env.eventCount(t, orgID); n != 100 {
		t.Fatalf("events after concurrent retransmit = %d, want 100", n)
	}
}

// TestBatchLimitsAndValidation verifies the 2000 cap and validation.
func TestBatchLimitsAndValidation(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("limits")
	tk := env.seedOrg(t, slug, "UTC")

	tooMany := make([]batchEvent, 2001)
	for i := range tooMany {
		tooMany[i] = batchEvent{
			EventID: fmt.Sprintf("big-%d", i), DbUser: "u",
			OccurredAt: "2026-09-22T10:00:00Z", Action: "select",
			Table: "t", RowCount: 0,
		}
	}
	path, body := batchPayload(slug, "s", tooMany)
	st, out := env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, http.StatusUnprocessableEntity, out)

	bad := []batchEvent{{
		EventID: "x", DbUser: "u",
		OccurredAt: "2026-09-22T10:00:00Z", Action: "DROP TABLE",
	}}
	path, body = batchPayload(slug, "s", bad)
	st, out = env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, http.StatusUnprocessableEntity, out)
}

func batchPayload(org string, source string, evs []batchEvent) (string, map[string]any) {
	return fmt.Sprintf("/api/v1/orgs/%s/events:batch", org), map[string]any{
		"source": source,
		"events": evs,
	}
}

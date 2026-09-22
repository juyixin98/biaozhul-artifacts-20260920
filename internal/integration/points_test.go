package integration

import (
	"fmt"
	"net/http"
	"sync"
	"testing"

	"geoterritory/internal/models"
	"geoterritory/internal/store"
)

// Idempotency by external id; changed coordinates require the expected
// version; stale versions conflict; good rows survive bad rows.
func TestBatchIdempotencyAndVersioning(t *testing.T) {
	ext := "pt-ver-" + uniq()

	insert := map[string]any{"points": []map[string]any{
		{"external_id": ext, "lat": 1.0, "lng": 2.0},
	}}
	st, body := doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA, insert)
	if st != http.StatusOK || body["failed"].(float64) != 0 {
		t.Fatalf("insert: %d %v", st, body)
	}
	row := body["results"].([]any)[0].(map[string]any)
	if row["version"].(float64) != 1 || row["created"] != true {
		t.Fatalf("first insert version=%v created=%v", row["version"], row["created"])
	}

	// Same coordinates again: idempotent, version stays 1, no update flag.
	st, body = doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA, insert)
	if st != http.StatusOK {
		t.Fatalf("repeat: %d", st)
	}
	row = body["results"].([]any)[0].(map[string]any)
	if row["status"] != store.RowOK || row["version"].(float64) != 1 {
		t.Fatalf("same-coordinate write must be idempotent at v1: %v", row)
	}

	// Changed coordinates without expected_version -> conflict.
	movedNoVer := map[string]any{"points": []map[string]any{
		{"external_id": ext, "lat": 3.0, "lng": 4.0},
	}}
	st, body = doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA, movedNoVer)
	if st != http.StatusOK {
		t.Fatalf("move no version http=%d", st)
	}
	row = body["results"].([]any)[0].(map[string]any)
	if row["status"] != store.RowErrVersion || row["version"].(float64) != 1 {
		t.Fatalf("move without version must conflict and report current v1: %v", row)
	}

	// Stale expected version -> conflict, reports current seq (still 1).
	stale := 0
	movedStale := map[string]any{"points": []map[string]any{
		{"external_id": ext, "lat": 3.0, "lng": 4.0, "expected_version": stale},
	}}
	st, body = doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA, movedStale)
	row = body["results"].([]any)[0].(map[string]any)
	if row["status"] != store.RowErrVersion {
		t.Fatalf("expected_version=0 against existing point must conflict: %v", row)
	}

	// Correct expected version 1 -> update to v2.
	movedOK := map[string]any{"points": []map[string]any{
		{"external_id": ext, "lat": 3.0, "lng": 4.0, "expected_version": 1},
	}}
	st, body = doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA, movedOK)
	row = body["results"].([]any)[0].(map[string]any)
	if row["status"] != store.RowOK || row["version"].(float64) != 2 || row["updated"] != true {
		t.Fatalf("optimistic update should advance to v2: %v", row)
	}

	// A subsequent stale seq 1 move conflicts and the coordinates were NOT
	// overwritten.
	st, body = doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA, map[string]any{"points": []map[string]any{
		{"external_id": ext, "lat": 88, "lng": 88, "expected_version": 1},
	}})
	row = body["results"].([]any)[0].(map[string]any)
	if row["status"] != store.RowErrVersion || row["version"].(float64) != 2 {
		t.Fatalf("stale seq must conflict and report v2: %v", row)
	}
	st, got := doJSON(t, http.MethodGet, "/v1/points/"+ext, env.keyA, nil)
	p := got["point"].(map[string]any)
	if p["lat"].(float64) != 3.0 || p["lng"].(float64) != 4.0 {
		t.Fatalf("rejected stale move must not change coordinates: %v", p)
	}
}

// One batch mixes valid and invalid rows; valid rows are still committed.
func TestBatchPartialSuccess(t *testing.T) {
	suffix := uniq()
	body := map[string]any{"points": []map[string]any{
		{"external_id": "good-" + suffix, "lat": 0, "lng": 0},
		{"external_id": "", "lat": 0, "lng": 0},                     // missing id
		{"external_id": "badcoord-" + suffix, "lat": 200, "lng": 0}, // range
		{"external_id": "good2-" + suffix, "lat": 1, "lng": 1},      // fine
	}}
	st, res := doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA, body)
	if st != http.StatusOK {
		t.Fatalf("http=%d %v", st, res)
	}
	if res["success"].(float64) != 2 || res["failed"].(float64) != 2 {
		t.Fatalf("want 2 success / 2 failed, got %v", res)
	}
	for _, r := range res["results"].([]any) {
		rm := r.(map[string]any)
		if rm["external_id"] == "good-"+suffix && rm["status"] != store.RowOK {
			t.Fatalf("good row must be OK: %v", rm)
		}
	}
	// The good row is durable even though siblings failed.
	st, got := doJSON(t, http.MethodGet, "/v1/points/good-"+suffix, env.keyA, nil)
	if st != http.StatusOK {
		t.Fatalf("good row missing after partial batch: %d %v", st, got)
	}
}

// Batch size cap enforced.
func TestBatchLimit1000(t *testing.T) {
	pts := make([]map[string]any, 0, 1001)
	for i := 0; i < 1001; i++ {
		pts = append(pts, map[string]any{
			"external_id": fmt.Sprintf("big-%s-%d", uniq(), i), "lat": 0, "lng": 0,
		})
	}
	st, body := doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA,
		map[string]any{"points": pts})
	if st != http.StatusBadRequest {
		t.Fatalf("want 400 for >1000 rows, got %d %v", st, body)
	}
}

// Concurrent updates to the SAME point at the same base version: exactly one
// can win at v2; every loser gets VERSION_CONFLICT; no update is silently
// lost (the winner's coordinates survive and are verifiable).
func TestConcurrentSamePointNoLostUpdate(t *testing.T) {
	ext := "race-" + uniq()
	if st, body := doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA, map[string]any{
		"points": []map[string]any{{"external_id": ext, "lat": 0, "lng": 0}},
	}); st != http.StatusOK {
		t.Fatalf("seed: %v", body)
	}

	const goroutines = 16
	var wg sync.WaitGroup
	var wins, conflicts int64
	var mu sync.Mutex
	winningLat := -1.0
	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			lat := 10.0 + float64(i)
			_, body := doJSON(t, http.MethodPost, "/v1/points/batch", env.keyA, map[string]any{
				"points": []map[string]any{{
					"external_id": ext, "lat": lat, "lng": 5.0, "expected_version": 1,
				}},
			})
			row := body["results"].([]any)[0].(map[string]any)
			mu.Lock()
			defer mu.Unlock()
			if row["status"] == store.RowOK {
				wins++
				winningLat = lat
			} else if row["status"] == store.RowErrVersion {
				conflicts++
			}
		}(i)
	}
	wg.Wait()
	if wins != 1 {
		t.Fatalf("exactly one concurrent mover must win, got wins=%d conflicts=%d", wins, conflicts)
	}
	if conflicts != goroutines-1 {
		t.Fatalf("the other %d movers must conflict, got %d", goroutines-1, conflicts)
	}
	_, got := doJSON(t, http.MethodGet, "/v1/points/"+ext, env.keyA, nil)
	p := got["point"].(map[string]any)
	if p["version"].(float64) != 2 || p["lat"].(float64) != winningLat {
		t.Fatalf("stored point must be the single winner v2 at lat %v, got %v", winningLat, p)
	}

	// Direct store-level sanity: update_seq is 2.
	var point models.Point
	if err := env.db.Where("org_id = ? AND external_id = ?", orgIDByKey(t, env.keyA), ext).
		First(&point).Error; err != nil {
		t.Fatal(err)
	}
	if point.UpdateSeq != 2 {
		t.Fatalf("update_seq=%d want 2", point.UpdateSeq)
	}
}

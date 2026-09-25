package trmerge

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
)

func newTestServer(t *testing.T) (*httptest.Server, *Store) {
	t.Helper()
	dir := t.TempDir()
	st, err := NewStore(filepath.Join(dir, "cache"))
	if err != nil {
		t.Fatal(err)
	}
	srv, err := NewServer(st, filepath.Join(dir, "work"))
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)
	return ts, st
}

func doJSON(t *testing.T, method, url string, body any) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, _ := http.NewRequest(method, url, rdr)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestAPIEndToEndEventsMode(t *testing.T) {
	ts, _ := newTestServer(t)

	// create
	code, body := doJSON(t, "POST", ts.URL+"/v1/runs", map[string]any{
		"mode": "events",
		"shards": []map[string]any{
			{"shard_id": "s1", "test_ids": []string{"t1", "t2"}},
		},
	})
	if code != http.StatusCreated {
		t.Fatalf("create code=%d body=%v", code, body)
	}
	runID := body["run_id"].(string)

	// events
	code, body = doJSON(t, "POST", ts.URL+"/v1/runs/"+runID+"/events", map[string]any{
		"events": []map[string]any{
			{"type": "attempt_started", "shard_id": "s1", "test_id": "t1", "attempt_id": "a1", "attempt_no": 1},
			{"type": "attempt_result", "shard_id": "s1", "test_id": "t1", "attempt_id": "a1", "attempt_no": 1, "result": "passed"},
			// t2 missing entirely
			{"type": "shard_finished", "shard_id": "s1", "outcome": "completed"},
		},
	})
	if code != http.StatusOK {
		t.Fatalf("events code=%d body=%v", code, body)
	}

	// finalize
	code, _ = doJSON(t, "POST", ts.URL+"/v1/runs/"+runID+"/finalize", map[string]any{})
	if code != http.StatusOK {
		t.Fatalf("finalize code=%d", code)
	}

	// summary
	code, body = doJSON(t, "GET", ts.URL+"/v1/runs/"+runID+"/summary", nil)
	if code != http.StatusOK {
		t.Fatalf("summary code=%d", code)
	}
	if body["status"] != "failed" {
		t.Fatalf("expected failed due to missing t2, got %v (%v)", body["status"], body["counts"])
	}
	counts := body["counts"].(map[string]any)
	if counts["passed"].(float64) != 1 || counts["incomplete"].(float64) != 1 {
		t.Fatalf("counts = %v", counts)
	}
}

func TestAPIRejectUnknownFieldsAndBadResult(t *testing.T) {
	ts, _ := newTestServer(t)
	code, body := doJSON(t, "POST", ts.URL+"/v1/runs", map[string]any{
		"mode":   "events",
		"bogus":  1,
		"shards": []any{},
	})
	if code != http.StatusBadRequest {
		t.Fatalf("unknown field should be rejected, code=%d body=%v", code, body)
	}
}

func TestAPIExecuteModeRejectsEmptyCommands(t *testing.T) {
	ts, _ := newTestServer(t)
	code, body := doJSON(t, "POST", ts.URL+"/v1/runs", map[string]any{
		"mode":   "execute",
		"shards": []any{},
	})
	if code != http.StatusBadRequest {
		t.Fatalf("execute without commands should 400, code=%d body=%v", code, body)
	}
}

func TestAPIReplayShuffleRepeatable(t *testing.T) {
	ts, _ := newTestServer(t)
	code, body := doJSON(t, "POST", ts.URL+"/v1/replay", map[string]any{
		"shards": []map[string]any{{"shard_id": "s1", "test_ids": []string{"t1"}}},
		"events": []map[string]any{
			{"type": "attempt_started", "shard_id": "s1", "test_id": "t1", "attempt_id": "a1", "attempt_no": 1},
			{"type": "attempt_result", "shard_id": "s1", "test_id": "t1", "attempt_id": "a1", "attempt_no": 1, "result": "passed"},
			{"type": "shard_finished", "shard_id": "s1", "outcome": "completed"},
			{"type": "run_finalized"},
		},
		"shuffle": true,
		"seed":    42,
		"repeats": 5,
	})
	if code != http.StatusOK {
		t.Fatalf("replay code=%d body=%v", code, body)
	}
	if body["identical"] != true {
		t.Fatalf("replay summaries not identical: %v", body["identical"])
	}
	sums := body["summaries"].([]any)
	first := sums[0].(map[string]any)
	if first["status"] != "completed" {
		t.Fatalf("replay status = %v", first["status"])
	}
}

func TestAPI404UnknownRun(t *testing.T) {
	ts, _ := newTestServer(t)
	code, _ := doJSON(t, "GET", ts.URL+"/v1/runs/nope/summary", nil)
	if code != http.StatusNotFound {
		t.Fatalf("code=%d want 404", code)
	}
}

func TestAPILateResultAfterFinalize(t *testing.T) {
	ts, _ := newTestServer(t)
	_, body := doJSON(t, "POST", ts.URL+"/v1/runs", map[string]any{
		"shards": []map[string]any{{"shard_id": "s1", "test_ids": []string{"t1"}}},
	})
	runID := body["run_id"].(string)

	doJSON(t, "POST", ts.URL+"/v1/runs/"+runID+"/events", map[string]any{
		"events": []map[string]any{
			{"type": "attempt_started", "shard_id": "s1", "test_id": "t1", "attempt_id": "a1", "attempt_no": 1},
			{"type": "shard_finished", "shard_id": "s1", "outcome": "crashed"},
		},
	})
	doJSON(t, "POST", ts.URL+"/v1/runs/"+runID+"/finalize", map[string]any{})

	// late pass after finalize
	code, body := doJSON(t, "POST", ts.URL+"/v1/runs/"+runID+"/events", map[string]any{
		"events": []map[string]any{
			{"type": "attempt_result", "shard_id": "s1", "test_id": "t1", "attempt_id": "a1", "attempt_no": 1, "result": "passed"},
		},
	})
	if code != http.StatusOK {
		t.Fatalf("code=%d", code)
	}
	res := body["results"].([]any)[0].(map[string]any)
	if res["late"] != true {
		t.Fatalf("late result not marked: %v", res)
	}
	_, sum := doJSON(t, "GET", ts.URL+"/v1/runs/"+runID+"/summary", nil)
	if sum["status"] != "failed" {
		t.Fatalf("late pass must not flip run to completed: %v", sum["status"])
	}
}

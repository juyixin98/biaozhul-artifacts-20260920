package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"rsrv/sched"
)

func testServer() (*Server, *httptest.Server) {
	sch := sched.New(sched.WithClock(sched.NewFakeClock(time.Date(2026, 9, 23, 0, 0, 0, 0, time.UTC))))
	s := New(sch)
	return s, httptest.NewServer(s.Handler())
}

func do(t *testing.T, ts *httptest.Server, method, path string, body any) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, _ := http.NewRequest(method, ts.URL+path, rdr)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode %s %s: %v", method, path, err)
	}
	return resp.StatusCode, out
}

func TestHTTPEndToEnd(t *testing.T) {
	_, ts := testServer()
	defer ts.Close()

	// health
	if code, body := do(t, ts, "GET", "/healthz", nil); code != 200 || body["status"] != "ok" {
		t.Fatalf("health: %d %v", code, body)
	}

	// add resource
	code, body := do(t, ts, "POST", "/v1/resources", map[string]any{
		"id": "room-a", "capacity": map[string]int64{"seats": 2, "mics": 1},
	})
	if code != http.StatusCreated {
		t.Fatalf("create resource: %d %v", code, body)
	}

	// duplicate -> 409
	if code, _ := do(t, ts, "POST", "/v1/resources", map[string]any{
		"id": "room-a", "capacity": map[string]int64{"seats": 2},
	}); code != http.StatusConflict {
		t.Fatalf("duplicate resource code = %d, want 409", code)
	}

	// list
	if code, body := do(t, ts, "GET", "/v1/resources", nil); code != 200 {
		t.Fatalf("list: %d %v", code, body)
	}
	if code, _ := do(t, ts, "GET", "/v1/resources/missing", nil); code != http.StatusNotFound {
		t.Fatalf("missing resource code = %d, want 404", code)
	}

	// commit a fixed reservation
	code, body = do(t, ts, "POST", "/v1/reservations:batch", map[string]any{
		"id": "b1",
		"items": []map[string]any{{
			"id": "i1", "resource_id": "room-a", "kind": "fixed",
			"demand":   map[string]int64{"seats": 2, "mics": 1},
			"interval": map[string]string{"start": "2026-09-23T10:00:00Z", "end": "2026-09-23T12:00:00Z"},
		}},
	})
	if code != http.StatusCreated {
		t.Fatalf("batch b1: %d %v", code, body)
	}

	// earliest query: a 2h job at 08:00 occupies [8,10), adjacent to the
	// existing [10,12), so 08:00 is feasible.
	code, body = do(t, ts, "POST", "/v1/earliest", map[string]any{
		"resource_id":  "room-a",
		"window_start": "2026-09-23T08:00:00Z",
		"window_end":   "2026-09-23T18:00:00Z",
		"duration":     "2h",
		"demand":       map[string]int64{"seats": 1, "mics": 1},
	})
	if code != 200 {
		t.Fatalf("earliest: %d %v", code, body)
	}
	if body["feasible"] != true || body["start"] != "2026-09-23T08:00:00Z" {
		t.Fatalf("earliest answer: %v (want adjacent 08:00)", body)
	}

	// A 3h job starting at 08:00 would occupy [8,11) and overlap [10,12);
	// the earliest feasible start is 12:00 (adjacent after).
	code, body = do(t, ts, "POST", "/v1/earliest", map[string]any{
		"resource_id":  "room-a",
		"window_start": "2026-09-23T08:00:00Z",
		"window_end":   "2026-09-23T18:00:00Z",
		"duration":     "3h",
		"demand":       map[string]int64{"seats": 1, "mics": 1},
	})
	if code != 200 || body["start"] != "2026-09-23T12:00:00Z" {
		t.Fatalf("3h earliest answer: code=%d %v (want 12:00)", code, body)
	}

	// batch that partially conflicts -> 409, nothing lands.
	code, body = do(t, ts, "POST", "/v1/reservations:batch", map[string]any{
		"id": "b2",
		"items": []map[string]any{
			{
				"id": "ok", "resource_id": "room-a", "kind": "fixed",
				"demand":   map[string]int64{"seats": 1},
				"interval": map[string]string{"start": "2026-09-23T08:00:00Z", "end": "2026-09-23T09:00:00Z"},
			},
			{
				"id": "bad", "resource_id": "room-a", "kind": "fixed",
				"demand":   map[string]int64{"seats": 1},
				"interval": map[string]string{"start": "2026-09-23T11:00:00Z", "end": "2026-09-23T13:00:00Z"},
			},
		},
	})
	if code != http.StatusConflict {
		t.Fatalf("partial conflict code = %d, want 409; body=%v", code, body)
	}
	b, _ := json.Marshal(body)
	if !strings.Contains(string(b), "capacity_exceeded") {
		t.Fatalf("409 body must name capacity_exceeded: %s", b)
	}

	// Atomicity: still exactly one reservation.
	code, body = do(t, ts, "GET", "/v1/reservations?resource_id=room-a", nil)
	if code != 200 {
		t.Fatalf("list reservations: %d", code)
	}
	rs, _ := body["reservations"].([]any)
	if len(rs) != 1 {
		t.Fatalf("after rejected batch there must still be exactly 1 reservation, got %d: %v", len(rs), body)
	}

	// zero capacity resource rejects a positive demand batch
	code, _ = do(t, ts, "POST", "/v1/resources", map[string]any{
		"id": "full", "capacity": map[string]int64{"seats": 0},
	})
	if code != http.StatusCreated {
		t.Fatalf("create zero-cap resource: %d", code)
	}
	code, body = do(t, ts, "POST", "/v1/reservations:batch", map[string]any{
		"id": "b3",
		"items": []map[string]any{{
			"id": "z", "resource_id": "full", "kind": "fixed",
			"demand":   map[string]int64{"seats": 1},
			"interval": map[string]string{"start": "2026-09-23T08:00:00Z", "end": "2026-09-23T09:00:00Z"},
		}},
	})
	if code != http.StatusConflict {
		t.Fatalf("zero-capacity batch code = %d want 409: %v", code, body)
	}

	// events endpoint shows structured history incl. the rejection
	code, body = do(t, ts, "GET", "/v1/events", nil)
	if code != 200 {
		t.Fatalf("events: %d", code)
	}
	evs, _ := body["events"].([]any)
	if len(evs) == 0 {
		t.Fatal("event history empty")
	}
	blob, _ := json.Marshal(evs)
	if !strings.Contains(string(blob), "batch_rejected") {
		t.Fatalf("events missing batch_rejected: %s", blob)
	}

	// malformed JSON -> 400
	req, _ := http.NewRequest("POST", ts.URL+"/v1/resources", strings.NewReader("{not json"))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("bad json code = %d, want 400", resp.StatusCode)
	}
}

func TestHTTPEarliestItemBatch(t *testing.T) {
	_, ts := testServer()
	defer ts.Close()
	do(t, ts, "POST", "/v1/resources", map[string]any{
		"id": "R", "capacity": map[string]int64{"cpu": 1},
	})
	code, body := do(t, ts, "POST", "/v1/reservations:batch", map[string]any{
		"id": "b",
		"items": []map[string]any{{
			"id": "e", "resource_id": "R", "kind": "earliest",
			"demand":       map[string]int64{"cpu": 1},
			"duration":     "90m",
			"window_start": "2026-09-23T00:00:00Z",
			"window_end":   "2026-09-23T12:00:00Z",
		}},
	})
	if code != http.StatusCreated {
		t.Fatalf("earliest batch: %d %v", code, body)
	}
	planned, _ := body["planned"].([]any)
	if len(planned) != 1 {
		t.Fatalf("planned = %v", body)
	}
	p := planned[0].(map[string]any)
	if p["start"] != "2026-09-23T00:00:00Z" || p["end"] != "2026-09-23T01:30:00Z" {
		t.Fatalf("earliest placement = %v", p)
	}
	if p["reservation_id"] == "" || p["reservation_id"] == nil {
		t.Fatal("reservation id missing in planned item")
	}
}

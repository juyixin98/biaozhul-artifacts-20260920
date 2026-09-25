package api_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"alertfsm/internal/api"
	"alertfsm/internal/engine"
	"alertfsm/internal/model"
	"alertfsm/internal/store"
)

type client struct {
	t   *testing.T
	srv *httptest.Server
}

func newClient(t *testing.T) *client {
	t.Helper()
	st, err := store.New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	st.SetClockFresh(1_000_000)
	eng := engine.New(st)
	srv := httptest.NewServer(api.NewServer(eng, st).Handler())
	t.Cleanup(srv.Close)
	return &client{t: t, srv: srv}
}

func (c *client) do(method, path string, body any) (int, map[string]any) {
	c.t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, err := http.NewRequest(method, c.srv.URL+path, rdr)
	if err != nil {
		c.t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		c.t.Fatal(err)
	}
	defer resp.Body.Close()
	out := map[string]any{}
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestHTTPFullWorkflow(t *testing.T) {
	c := newClient(t)

	// Health.
	if code, body := c.do("GET", "/health", nil); code != 200 || body["status"] != "ok" {
		t.Fatalf("health: %d %v", code, body)
	}

	// Create rule, using human-readable duration strings.
	rule := map[string]any{
		"id":           "cpu-high",
		"metric":       "cpu.usage",
		"threshold":    80,
		"direction":    "above",
		"pending_for":  "60s",
		"recovery_for": "30s",
		"no_data_for":  "120s",
	}
	if code, body := c.do("POST", "/api/v1/rules", rule); code != 201 {
		t.Fatalf("create rule: %d %v", code, body)
	}
	if code, body := c.do("POST", "/api/v1/rules", rule); code != 400 {
		t.Fatalf("duplicate rule should fail: %d %v", code, body)
	}
	if code, body := c.do("POST", "/api/v1/rules", map[string]any{"metric": "x", "direction": "sideways"}); code != 400 {
		t.Fatalf("invalid rule should fail: %d %v", code, body)
	}

	// Ingest healthy data.
	ing := map[string]any{"samples": []map[string]any{
		{"metric": "cpu.usage", "ts_ms": 1_000_000, "value": 50},
	}}
	if code, body := c.do("POST", "/api/v1/ingest", ing); code != 202 {
		t.Fatalf("ingest: %d %v", code, body)
	}

	// Breach.
	ing = map[string]any{"samples": []map[string]any{
		{"metric": "cpu.usage", "ts_ms": 1_010_000, "value": 95},
	}}
	c.do("POST", "/api/v1/ingest", ing)
	if code, body := c.do("GET", "/api/v1/rules/cpu-high/state", nil); code != 200 || body["status"] != model.StatusPending {
		t.Fatalf("state after breach: %d %v", code, body)
	}

	// Duplicate ingestion: reported as duplicate, state unchanged.
	if _, body := c.do("POST", "/api/v1/ingest", ing); true {
		dups, _ := body["duplicate"].([]any)
		if len(dups) != 1 {
			t.Fatalf("want 1 duplicate, got %v", body)
		}
	}

	// Tick past pending deadline -> firing event.
	if code, body := c.do("POST", "/api/v1/admin/tick", map[string]any{"to_ms": 1_070_000}); code != 200 {
		t.Fatalf("tick: %d %v", code, body)
	}
	if code, body := c.do("GET", "/api/v1/rules/cpu-high/state", nil); code != 200 || body["status"] != model.StatusAlerting {
		t.Fatalf("state after pending: %d %v", code, body)
	}
	if code, body := c.do("GET", "/api/v1/events?rule_id=cpu-high", nil); code != 200 {
		t.Fatalf("events: %d", code)
	} else {
		evs, _ := body["events"].([]any)
		if len(evs) != 1 || evs[0].(map[string]any)["type"] != model.EventFiring {
			t.Fatalf("want single firing event, got %v", body)
		}
	}

	// Recover and resolve.
	c.do("POST", "/api/v1/ingest", map[string]any{"samples": []map[string]any{
		{"metric": "cpu.usage", "ts_ms": 1_080_000, "value": 10},
	}})
	c.do("POST", "/api/v1/admin/tick", map[string]any{"by": "30s"})
	if code, body := c.do("GET", "/api/v1/rules/cpu-high/state", nil); body["status"] != model.StatusOK {
		t.Fatalf("state after recovery: %d %v", code, body)
	}
	if _, body := c.do("GET", "/api/v1/events?rule_id=cpu-high", nil); true {
		evs, _ := body["events"].([]any)
		if len(evs) != 2 || evs[1].(map[string]any)["type"] != model.EventResolved {
			t.Fatalf("want resolved event, got %v", body)
		}
	}

	// Samples are queryable.
	if code, body := c.do("GET", "/api/v1/samples?metric=cpu.usage", nil); code != 200 {
		t.Fatalf("samples: %d", code)
	} else if ss, _ := body["samples"].([]any); len(ss) != 3 {
		t.Fatalf("want 3 stored samples, got %v", body)
	}

	// Config update resets state with a reset event.
	rule["threshold"] = 99
	if code, body := c.do("PUT", "/api/v1/rules/cpu-high", rule); code != 200 {
		t.Fatalf("update: %d %v", code, body)
	}
	if code, body := c.do("GET", "/api/v1/rules/cpu-high/state", nil); body["status"] != model.StatusOK {
		t.Fatalf("state after update: %d %v", code, body)
	}
	if _, body := c.do("GET", "/api/v1/events?rule_id=cpu-high&limit=1", nil); true {
		evs, _ := body["events"].([]any)
		if len(evs) != 1 || evs[0].(map[string]any)["type"] != model.EventReset {
			t.Fatalf("want reset event, got %v", body)
		}
	}

	// Delete.
	if code, _ := c.do("DELETE", "/api/v1/rules/cpu-high", nil); code != 204 {
		t.Fatalf("delete: %d", code)
	}
	if code, _ := c.do("GET", "/api/v1/rules/cpu-high/state", nil); code != 404 {
		t.Fatalf("state after delete: %d", code)
	}
}

func TestHTTPNoDataAndBackwardsTick(t *testing.T) {
	c := newClient(t)
	c.do("POST", "/api/v1/rules", map[string]any{
		"id": "nd", "metric": "m", "threshold": 1, "direction": "above",
		"pending_for": "1s", "recovery_for": "1s", "no_data_for": "10s",
	})
	c.do("POST", "/api/v1/ingest", map[string]any{"samples": []map[string]any{
		{"metric": "m", "ts_ms": 1_000_000, "value": 0},
	}})
	// Silence beyond no-data deadline.
	if code, body := c.do("POST", "/api/v1/admin/tick", map[string]any{"to_ms": 1_010_000}); code != 200 {
		t.Fatalf("tick: %d %v", code, body)
	}
	if code, body := c.do("GET", "/api/v1/rules/nd/state", nil); body["status"] != model.StatusNoData {
		t.Fatalf("want no_data, got %d %v", code, body)
	}
	// Clock cannot move backwards.
	if code, _ := c.do("POST", "/api/v1/admin/tick", map[string]any{"to_ms": 1_000_000}); code != 400 {
		t.Fatalf("backwards tick should be 400, got %d", code)
	}
	// Late (out-of-order) sample is stored but ignored by the FSM.
	if code, body := c.do("POST", "/api/v1/ingest", map[string]any{"samples": []map[string]any{
		{"metric": "m", "ts_ms": 1_005_000, "value": 99},
	}}); code != 202 {
		t.Fatalf("late ingest: %d %v", code, body)
	} else if late, _ := body["late"].([]any); len(late) != 1 {
		t.Fatalf("want late classification, got %v", body)
	}
	if code, body := c.do("GET", "/api/v1/rules/nd/state", nil); body["status"] != model.StatusNoData {
		t.Fatalf("late sample changed FSM: %d %v", code, body)
	}
}

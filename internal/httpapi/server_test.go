package httpapi_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/example/hysteresis-alerter/internal/engine"
	"github.com/example/hysteresis-alerter/internal/httpapi"
)

type envelope struct {
	OK    bool            `json:"ok"`
	Data  json.RawMessage `json:"data"`
	Error *struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

func newSrv() (*httptest.Server, *engine.Engine) {
	e := engine.NewEngine()
	return httptest.NewServer(httpapi.New(e)), e
}

func do(t *testing.T, s *httptest.Server, method, path string, body interface{}) (envelope, int) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(raw)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, err := http.NewRequest(method, s.URL+path, rdr)
	if err != nil {
		t.Fatal(err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var env envelope
	if err := json.NewDecoder(resp.Body).Decode(&env); err != nil {
		t.Fatalf("decode %s %s: %v", method, path, err)
	}
	return env, resp.StatusCode
}

func mustOK(t *testing.T, env envelope, status int, wantStatus int) {
	t.Helper()
	if status != wantStatus {
		msg := ""
		if env.Error != nil {
			msg = env.Error.Message
		}
		t.Fatalf("status=%d want %d: %s", status, wantStatus, msg)
	}
	if env.Error != nil && wantStatus < 300 {
		t.Fatalf("status %d but error body: %+v", wantStatus, env.Error)
	}
}

func TestHTTPEndToEnd(t *testing.T) {
	srv, e := newSrv()
	defer srv.Close()

	// Health and clock.
	env, status := do(t, srv, http.MethodGet, "/health", nil)
	mustOK(t, env, status, http.StatusOK)
	env, _ = do(t, srv, http.MethodGet, "/clock", nil)
	var clock struct {
		Now string `json:"now"`
	}
	_ = json.Unmarshal(env.Data, &clock)
	if clock.Now == "" {
		t.Fatal("clock now missing")
	}

	// Create rule.
	env, status = do(t, srv, http.MethodPost, "/rules", map[string]interface{}{
		"id": "cpu1", "metric": "cpu", "operator": ">=",
		"threshold": 80, "trigger_for": "60s", "recover_for": "90s",
		"no_data_for": "2m", "recovery_threshold": 70,
	})
	mustOK(t, env, status, http.StatusCreated)

	// Duplicate create -> 400.
	_, status = do(t, srv, http.MethodPost, "/rules", map[string]interface{}{
		"id": "cpu1", "metric": "cpu", "operator": ">=",
		"threshold": 80, "no_data_for": "2m",
	})
	if status != http.StatusBadRequest {
		t.Fatalf("dup create status=%d want 400", status)
	}

	// Invalid rule -> 400.
	_, status = do(t, srv, http.MethodPost, "/rules", map[string]interface{}{
		"id": "bad", "metric": "cpu", "operator": "==",
		"threshold": 80, "no_data_for": "2m",
	})
	if status != http.StatusBadRequest {
		t.Fatalf("bad operator status=%d want 400", status)
	}

	// GET missing rule -> 404.
	_, status = do(t, srv, http.MethodGet, "/rules/nope", nil)
	if status != http.StatusNotFound {
		t.Fatalf("missing rule status=%d want 404", status)
	}

	// Ingest samples using Unix-seconds timestamps and drive a firing.
	t0 := time.Date(2026, 2, 1, 12, 0, 0, 0, time.UTC)
	ingest := func(offset time.Duration, v float64) {
		env, st := do(t, srv, http.MethodPost, "/samples", map[string]interface{}{
			"samples": []map[string]interface{}{{
				"metric": "cpu", "ts": t0.Add(offset).Unix(), "value": v,
			}},
		})
		if st != http.StatusOK {
			t.Fatalf("ingest offset=%s v=%v status=%d err=%+v", offset, v, st, env.Error)
		}
		mustOK(t, env, st, http.StatusOK)
	}
	ingest(0, 90)
	ingest(30*time.Second, 91)
	ingest(60*time.Second, 92) // fires

	if got := ruleState(t, srv, "cpu1"); got != "firing" {
		t.Fatalf("state=%s want firing", got)
	}

	// Events endpoint shows exactly one firing notification.
	evs := listEvents(t, srv, "")
	if len(evs) != 1 || evs[0]["type"] != "firing" {
		t.Fatalf("events=%v want one firing", evs)
	}

	// Duplicate sample: same metric+ts -> duplicate=true, no new events.
	env, status = do(t, srv, http.MethodPost, "/samples", map[string]interface{}{
		"samples": []map[string]interface{}{{
			"metric": "cpu", "ts": t0.Add(60 * time.Second).Unix(), "value": 92,
		}},
	})
	mustOK(t, env, status, http.StatusOK)
	var ingestOut struct {
		Results []struct {
			Duplicate bool                     `json:"duplicate"`
			Late      bool                     `json:"late"`
			Events    []map[string]interface{} `json:"events"`
		} `json:"results"`
	}
	_ = json.Unmarshal(env.Data, &ingestOut)
	if !ingestOut.Results[0].Duplicate || ingestOut.Results[0].Late || len(ingestOut.Results[0].Events) != 0 {
		t.Fatalf("repeat sample behavior wrong: %+v", ingestOut.Results[0])
	}

	// Late sample -> late=true, zero events.
	env, _ = do(t, srv, http.MethodPost, "/samples", map[string]interface{}{
		"samples": []map[string]interface{}{{
			"metric": "cpu", "ts": t0.Add(-time.Hour).Unix(), "value": 999,
		}},
	})
	_ = json.Unmarshal(env.Data, &ingestOut)
	if !ingestOut.Results[0].Late || len(ingestOut.Results[0].Events) != 0 {
		t.Fatalf("late sample behavior wrong: %+v", ingestOut.Results[0])
	}

	// Tick past no-data? No — first resolve via cold streak, then test tick.
	ingest(90*time.Second, 40) // recovering
	// warm 75 suspends countdown
	ingest(120*time.Second, 75)
	// cold restart; 90s needed
	ingest(130*time.Second, 40)
	ingest(160*time.Second, 41)
	ingest(220*time.Second, 42) // resolved
	if got := ruleState(t, srv, "cpu1"); got != "inactive" {
		t.Fatalf("state=%s want inactive", got)
	}
	evs = listEvents(t, srv, "")
	if len(evs) != 2 || evs[1]["type"] != "resolved" {
		t.Fatalf("events=%v want firing,resolved", evs)
	}

	// Long silence -> nodata via tick.
	env, status = do(t, srv, http.MethodPost, "/clock/tick", map[string]string{"duration": "3m"})
	mustOK(t, env, status, http.StatusOK)
	if got := ruleState(t, srv, "cpu1"); got != "nodata" {
		t.Fatalf("state=%s want nodata", got)
	}
	// Another tick must not duplicate the notification.
	_, _ = do(t, srv, http.MethodPost, "/clock/tick", map[string]string{"duration": "1h"})
	evs = listEvents(t, srv, "")
	var nodataCount int
	for _, ev := range evs {
		if ev["type"] == "nodata" {
			nodataCount++
		}
	}
	if nodataCount != 1 {
		t.Fatalf("nodata notifications=%d want 1", nodataCount)
	}

	// Backward tick -> 400.
	_, status = do(t, srv, http.MethodPost, "/clock/tick-to", map[string]int64{
		"to": t0.Unix(),
	})
	if status != http.StatusBadRequest {
		t.Fatalf("backward tick status=%d want 400", status)
	}

	// Resume with healthy sample -> data_resumed.
	_, status = do(t, srv, http.MethodPost, "/samples", map[string]interface{}{
		"samples": []map[string]interface{}{{
			"metric": "cpu", "ts": e.Now().Add(time.Minute).Unix(), "value": 30,
		}},
	})
	mustOK(t, env, status, http.StatusOK)
	evs = listEvents(t, srv, "")
	if evs[len(evs)-1]["type"] != "data_resumed" {
		t.Fatalf("last event=%v want data_resumed", evs[len(evs)-1])
	}

	// Update rule resets state and adds an audit event (hidden from the
	// notifications feed, visible with all=true).
	_, status = do(t, srv, http.MethodPut, "/rules/cpu1", map[string]interface{}{
		"metric": "cpu", "operator": ">", "threshold": 95,
		"trigger_for": "30s", "recover_for": "30s", "no_data_for": "2m",
	})
	mustOK(t, env, status, http.StatusOK)
	if got := ruleState(t, srv, "cpu1"); got != "inactive" {
		t.Fatalf("after PUT state=%s want inactive", got)
	}
	allEvs := listEvents(t, srv, "?all=true")
	foundReset := false
	for _, ev := range allEvs {
		if ev["type"] == "rule_reset" {
			foundReset = true
		}
	}
	if !foundReset {
		t.Fatal("rule_reset audit event missing in all-events feed")
	}
	notifs := listEvents(t, srv, "")
	for _, ev := range notifs {
		if ev["type"] == "rule_reset" {
			t.Fatal("rule_reset leaked into notifications feed")
		}
	}

	// Query samples.
	env, status = do(t, srv, http.MethodGet, "/samples/cpu?limit=5", nil)
	mustOK(t, env, status, http.StatusOK)
	var qout struct {
		Samples []map[string]interface{} `json:"samples"`
	}
	_ = json.Unmarshal(env.Data, &qout)
	if len(qout.Samples) != 5 {
		t.Fatalf("sample limit returned %d want 5", len(qout.Samples))
	}

	// Delete rule.
	_, status = do(t, srv, http.MethodDelete, "/rules/cpu1", nil)
	mustOK(t, env, status, http.StatusOK)
	_, status = do(t, srv, http.MethodGet, "/rules/cpu1", nil)
	if status != http.StatusNotFound {
		t.Fatalf("deleted rule GET status=%d want 404", status)
	}
}

func ruleState(t *testing.T, s *httptest.Server, id string) string {
	t.Helper()
	env, status := do(t, s, http.MethodGet, "/rules/"+id, nil)
	mustOK(t, env, status, http.StatusOK)
	var out struct {
		State struct {
			State string `json:"state"`
		} `json:"state"`
	}
	if err := json.Unmarshal(env.Data, &out); err != nil {
		t.Fatal(err)
	}
	return out.State.State
}

func listEvents(t *testing.T, s *httptest.Server, query string) []map[string]interface{} {
	t.Helper()
	path := "/events" + query
	env, status := do(t, s, http.MethodGet, path, nil)
	mustOK(t, env, status, http.StatusOK)
	var out struct {
		Events []map[string]interface{} `json:"events"`
	}
	if err := json.Unmarshal(env.Data, &out); err != nil {
		t.Fatal(err)
	}
	return out.Events
}

func TestHTTPAdditionalEndpoints(t *testing.T) {
	srv, _ := newSrv()
	defer srv.Close()

	// Empty list endpoints.
	env, status := do(t, srv, http.MethodGet, "/rules", nil)
	mustOK(t, env, status, http.StatusOK)
	var rules []json.RawMessage
	_ = json.Unmarshal(env.Data, &rules)
	if len(rules) != 0 {
		t.Fatalf("want empty rules list, got %d", len(rules))
	}
	env, _ = do(t, srv, http.MethodGet, "/states", nil)
	var states []json.RawMessage
	_ = json.Unmarshal(env.Data, &states)
	if len(states) != 0 {
		t.Fatalf("want empty states, got %d", len(states))
	}

	// Create via RFC3339 timestamps later.
	env, status = do(t, srv, http.MethodPost, "/rules", map[string]interface{}{
		"id": "r1", "metric": "m", "operator": ">", "threshold": 10,
		"trigger_for": "10s", "recover_for": "10s", "no_data_for": "1m",
	})
	mustOK(t, env, status, http.StatusCreated)

	// tick-to forward.
	env, status = do(t, srv, http.MethodPost, "/clock/tick-to", map[string]string{
		"to": "2026-01-01T00:00:30Z",
	})
	mustOK(t, env, status, http.StatusOK)
	// tick-to missing field.
	_, status = do(t, srv, http.MethodPost, "/clock/tick-to", map[string]string{})
	if status != http.StatusBadRequest {
		t.Fatalf("empty tick-to status=%d want 400", status)
	}
	// tick invalid duration.
	_, status = do(t, srv, http.MethodPost, "/clock/tick", map[string]string{"duration": "0s"})
	if status != http.StatusBadRequest {
		t.Fatalf("zero duration status=%d want 400", status)
	}
	_, status = do(t, srv, http.MethodPost, "/clock/tick", map[string]string{"duration": "junk"})
	if status != http.StatusBadRequest {
		t.Fatalf("junk duration status=%d want 400", status)
	}

	// PUT missing rule -> 404.
	_, status = do(t, srv, http.MethodPut, "/rules/ghost", map[string]interface{}{
		"metric": "m", "operator": ">", "threshold": 1, "no_data_for": "1m",
	})
	if status != http.StatusNotFound {
		t.Fatalf("PUT missing status=%d want 404", status)
	}
	// DELETE missing -> 404.
	_, status = do(t, srv, http.MethodDelete, "/rules/ghost", nil)
	if status != http.StatusNotFound {
		t.Fatalf("DELETE missing status=%d want 404", status)
	}

	// Samples with RFC3339 timestamp; query using from/to RFC3339 + bad time.
	env, status = do(t, srv, http.MethodPost, "/samples", map[string]interface{}{
		"samples": []map[string]interface{}{{
			"metric": "m", "ts": "2026-01-01T00:01:00Z", "value": 5,
		}},
	})
	mustOK(t, env, status, http.StatusOK)
	env, status = do(t, srv, http.MethodGet,
		"/samples/m?from=2026-01-01T00:00:00Z&to=2026-01-01T00:02:00Z", nil)
	mustOK(t, env, status, http.StatusOK)
	var q struct {
		Samples []map[string]interface{} `json:"samples"`
	}
	_ = json.Unmarshal(env.Data, &q)
	if len(q.Samples) != 1 {
		t.Fatalf("range samples=%d want 1", len(q.Samples))
	}
	// Bad query time -> 400.
	_, status = do(t, srv, http.MethodGet, "/samples/m?from=notatime", nil)
	if status != http.StatusBadRequest {
		t.Fatalf("bad from status=%d want 400", status)
	}
	// Bad limit -> 400.
	_, status = do(t, srv, http.MethodGet, "/samples/m?limit=x", nil)
	if status != http.StatusBadRequest {
		t.Fatalf("bad limit status=%d want 400", status)
	}

	// events with bad since -> 400; valid since returns a list.
	_, status = do(t, srv, http.MethodGet, "/events?since=zzz", nil)
	if status != http.StatusBadRequest {
		t.Fatalf("bad since status=%d want 400", status)
	}
	env, status = do(t, srv, http.MethodGet, "/events?since=2026-01-01T00:00:00Z", nil)
	mustOK(t, env, status, http.StatusOK)

	// Missing metric field in sample -> 400.
	_, status = do(t, srv, http.MethodPost, "/samples", map[string]interface{}{
		"samples": []map[string]interface{}{{"ts": 1, "value": 2}},
	})
	if status != http.StatusBadRequest {
		t.Fatalf("missing metric status=%d want 400", status)
	}
}

func TestHTTPBadBodies(t *testing.T) {
	srv, _ := newSrv()
	defer srv.Close()
	resp, err := http.Post(srv.URL+"/samples", "application/json", bytes.NewReader([]byte("{not json")))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d want 400", resp.StatusCode)
	}

	// Empty samples array -> 400.
	env, status := do(t, srv, http.MethodPost, "/samples", map[string]interface{}{
		"samples": []interface{}{},
	})
	if status != http.StatusBadRequest {
		t.Fatalf("status=%d want 400 (env=%s)", status, env.Error)
	}

	// Tick with no body defaults? It requires a body -> 400 on empty body.
	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/clock/tick", bytes.NewReader(nil))
	resp2, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp2.Body.Close()
	if resp2.StatusCode != http.StatusBadRequest {
		t.Fatalf("empty tick status=%d want 400", resp2.StatusCode)
	}
}

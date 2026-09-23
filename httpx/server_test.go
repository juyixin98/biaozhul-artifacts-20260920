package httpx

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newTestServer(t *testing.T) (*httptest.Server, func()) {
	t.Helper()
	mux := http.NewServeMux()
	srv := New()
	srv.Routes(mux)
	ts := httptest.NewServer(mux)
	return ts, ts.Close
}

func post(t *testing.T, ts *httptest.Server, path, body string) (int, map[string]interface{}) {
	t.Helper()
	resp, err := http.Post(ts.URL+path, "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]interface{}
	dec := json.NewDecoder(resp.Body)
	_ = dec.Decode(&out)
	return resp.StatusCode, out
}

func get(t *testing.T, ts *httptest.Server, path string) (int, map[string]interface{}) {
	t.Helper()
	resp, err := http.Get(ts.URL + path)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]interface{}
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestCreateAdvanceProposeSnapshot(t *testing.T) {
	ts, closeFn := newTestServer(t)
	defer closeFn()

	code, out := post(t, ts, "/sessions", `{"nodes":3,"seed":99}`)
	if code != 201 {
		t.Fatalf("create status %d body %v", code, out)
	}
	id := out["id"].(string)

	code, out = post(t, ts, "/sessions/"+id+"/advance", `{"ms":250}`)
	if code != 200 {
		t.Fatalf("advance status %d", code)
	}
	if out["leader"] == nil || out["leader"].(float64) < 1 {
		t.Fatalf("no leader after advance: %v", out["leader"])
	}
	leader := int(out["leader"].(float64))

	code, out = post(t, ts, "/sessions/"+id+"/propose", `{"command":"SET k v1"}`)
	if code != 200 || out["accepted"] != true {
		t.Fatalf("propose failed: %d %v", code, out)
	}
	if int(out["node"].(float64)) != leader {
		t.Fatalf("proposal routed to %d, want leader %d", int(out["node"].(float64)), leader)
	}

	code, out = post(t, ts, "/sessions/"+id+"/advance", `{"ms":80}`)
	nodes := out["nodes"].([]interface{})
	committed := 0
	for _, v := range nodes {
		n := v.(map[string]interface{})
		if n["commitIndex"].(float64) >= 2 {
			committed++
		}
	}
	if committed < 2 {
		t.Fatalf("want majority commit>=2, got %d", committed)
	}
}

func TestBadCreateRejected(t *testing.T) {
	ts, closeFn := newTestServer(t)
	defer closeFn()
	code, _ := post(t, ts, "/sessions", `{"nodes":7}`)
	if code != 400 {
		t.Fatalf("want 400 for 7 nodes, got %d", code)
	}
	code, _ = post(t, ts, "/sessions", `{"variant":"nope"}`)
	if code != 400 {
		t.Fatalf("want 400 for bad variant, got %d", code)
	}
}

func TestCounterexampleRunEndpoint(t *testing.T) {
	ts, closeFn := newTestServer(t)
	defer closeFn()

	code, out := get(t, ts, "/scenarios/counterexamples")
	if code != 200 {
		t.Fatalf("list CE: %d", code)
	}
	ces := out["counterexamples"].([]interface{})
	if len(ces) < 2 {
		t.Fatalf("want >=2 counterexamples, got %d", len(ces))
	}
	ce0 := ces[0].(map[string]interface{})

	raw, _ := json.Marshal(ce0)
	resp, err := http.Post(ts.URL+"/scenarios/run", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var run map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&run); err != nil {
		t.Fatal(err)
	}
	if run["ok"] != false {
		t.Fatalf("buggy variant unexpectedly OK: %v", run["ok"])
	}

	// Same trace switched to standard must pass.
	ce0["variant"] = "standard"
	raw, _ = json.Marshal(ce0)
	resp2, err := http.Post(ts.URL+"/scenarios/run", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	var run2 map[string]interface{}
	_ = json.NewDecoder(resp2.Body).Decode(&run2)
	if run2["ok"] != true {
		t.Fatalf("standard variant violated: %v", run2["violations"])
	}
}

func TestEnumerateEndpoint(t *testing.T) {
	ts, closeFn := newTestServer(t)
	defer closeFn()
	code, out := post(t, ts, "/enumerate", `{"variant":"standard","depth":2,"fuzz":50,"maxCounterexamples":3}`)
	if code != 200 {
		t.Fatalf("enumerate: %d", code)
	}
	if n, ok := out["counterexamples"].([]interface{}); !ok || len(n) != 0 {
		t.Fatalf("standard enum had counterexamples: %v", out["counterexamples"])
	}
	if out["exhaustiveRan"].(float64) < 100 {
		t.Fatalf("enumeration suspiciously short: %v", out["exhaustiveRan"])
	}
}

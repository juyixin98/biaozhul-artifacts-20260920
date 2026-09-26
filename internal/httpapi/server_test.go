package httpapi_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"breakerhalfopen/internal/breaker"
	"breakerhalfopen/internal/httpapi"
)

type apiEnvelope struct {
	Success bool            `json:"success"`
	Data    json.RawMessage `json:"data"`
	Error   string          `json:"error"`
}

func newServer(t *testing.T) (*httptest.Server, *http.Client) {
	t.Helper()
	cfg := breaker.Config{
		WindowSize:               5,
		FailureThreshold:         3,
		OpenCoolDown:             10 * time.Second,
		MaxProbeCalls:            2,
		HalfOpenSuccessThreshold: 2,
	}
	srv := httptest.NewServer(httpapi.NewService(cfg).Handler())
	t.Cleanup(srv.Close)
	return srv, srv.Client()
}

func postJSON(t *testing.T, cl *http.Client, url string, body string) (int, apiEnvelope) {
	t.Helper()
	resp, err := cl.Post(url, "application/json", bytes.NewBufferString(body))
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	defer resp.Body.Close()
	var env apiEnvelope
	if err := json.NewDecoder(resp.Body).Decode(&env); err != nil {
		t.Fatalf("decode %s: %v", url, err)
	}
	return resp.StatusCode, env
}

func getJSON(t *testing.T, cl *http.Client, url string) (int, apiEnvelope) {
	t.Helper()
	resp, err := cl.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer resp.Body.Close()
	var env apiEnvelope
	if err := json.NewDecoder(resp.Body).Decode(&env); err != nil {
		t.Fatalf("decode %s: %v", url, err)
	}
	return resp.StatusCode, env
}

func stateOf(t *testing.T, env apiEnvelope) breaker.Snapshot {
	t.Helper()
	var data struct {
		Breaker breaker.Snapshot `json:"breaker"`
	}
	if err := json.Unmarshal(env.Data, &data); err != nil {
		t.Fatalf("unmarshal state data: %v", err)
	}
	return data.Breaker
}

// TestHTTPTripProbeRecover drives the full localhost HTTP surface with the
// virtual clock: inject failures, trip, advance cool-down and recover.
func TestHTTPTripProbeRecover(t *testing.T) {
	srv, cl := newServer(t)

	// Healthy call first.
	if code, env := postJSON(t, cl, srv.URL+"/api/call", "{}"); code != http.StatusOK || !env.Success {
		t.Fatalf("healthy call code=%d env=%+v", code, env)
	}

	// Inject failures and trip on the 3rd.
	if _, env := postJSON(t, cl, srv.URL+"/api/upstream/behavior", `{"fail":true}`); !env.Success {
		t.Fatalf("set behavior: %+v", env)
	}
	for i := 0; i < 3; i++ {
		postJSON(t, cl, srv.URL+"/api/call", "{}")
	}
	_, env := getJSON(t, cl, srv.URL+"/api/state")
	if s := stateOf(t, env); s.State != breaker.StateOpen {
		t.Fatalf("state=%s, want open", s.State)
	}

	// An OPEN call is rejected (503) without reaching the upstream.
	if code, env := postJSON(t, cl, srv.URL+"/api/call", "{}"); code != http.StatusServiceUnavailable || env.Success {
		t.Fatalf("call during OPEN code=%d success=%v, want 503 success=false", code, env.Success)
	}

	// Restore health, move virtual time past the cool-down.
	postJSON(t, cl, srv.URL+"/api/upstream/behavior", `{"dynamic":false}`)
	_, env = postJSON(t, cl, srv.URL+"/api/clock/advance", `{"duration_ms":10000}`)
	if s := stateOf(t, env); s.State != breaker.StateHalfOpen {
		t.Fatalf("state=%s, want half_open", s.State)
	}

	// Two successful probes close the breaker.
	postJSON(t, cl, srv.URL+"/api/call", "{}")
	_, env = postJSON(t, cl, srv.URL+"/api/call", "{}")
	var callData struct {
		Attempt struct {
			Result string `json:"result"`
		} `json:"attempt"`
	}
	_ = json.Unmarshal(env.Data, &callData)
	_, env = getJSON(t, cl, srv.URL+"/api/state")
	if s := stateOf(t, env); s.State != breaker.StateClosed {
		t.Fatalf("state=%s, want closed after probes", s.State)
	}
}

// TestHTTPScenarioEndpoint runs the built-in scenarios over HTTP and requires
// success=true on each structured report.
func TestHTTPScenarioEndpoint(t *testing.T) {
	srv, cl := newServer(t)

	for _, name := range []string{"late_failure", "probe_contention", "cancel_is_not_failure"} {
		code, env := postJSON(t, cl, srv.URL+"/api/scenarios/"+name+"/run", "{}")
		if code != http.StatusOK || !env.Success {
			t.Fatalf("scenario %s code=%d success=%v error=%s", name, code, env.Success, env.Error)
		}
		var rep struct {
			Pass     bool     `json:"pass"`
			Failures []string `json:"failures"`
		}
		if err := json.Unmarshal(env.Data, &rep); err != nil {
			t.Fatalf("decode report: %v", err)
		}
		if !rep.Pass {
			t.Fatalf("scenario %s failures=%v", name, rep.Failures)
		}
	}
}

// TestHTTPUnknownScenario404.
func TestHTTPUnknownScenario404(t *testing.T) {
	srv, cl := newServer(t)
	code, _ := postJSON(t, cl, srv.URL+"/api/scenarios/nope/run", "{}")
	if code != http.StatusNotFound {
		t.Fatalf("code=%d, want 404", code)
	}
}

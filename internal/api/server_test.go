package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"contractcheck/internal/clock"
	"contractcheck/internal/compat"
	"contractcheck/internal/fault"
	"contractcheck/internal/registry"
)

const oldContract = `{
	"request":  {"type":"object","required":["id"],"properties":{"id":{"type":"string"},"level":{"enum":["low","high"]}}},
	"response": {"type":"object","required":["status"],"properties":{"status":{"enum":["ok","failed"]},"eta":{"type":"integer","maximum":30}}}
}`

const newContract = `{
	"request":  {"type":"object","required":["id","level"],"properties":{"id":{"type":"string"},"level":{"enum":["low"]}}},
	"response": {"type":"object","required":["status"],"properties":{"status":{"enum":["ok","failed","pending"]},"eta":{"type":"integer","maximum":60}}}
}`

func setup(t *testing.T, faultCfg fault.Config) (*httptest.Server, *clock.Fake) {
	t.Helper()
	store := registry.NewStore()
	store.Put(registry.Contract{Service: "orders", Version: "v1",
		Request:  json.RawMessage(`{"type":"object","required":["id"],"properties":{"id":{"type":"string"},"level":{"enum":["low","high"]}}}`),
		Response: json.RawMessage(`{"type":"object","required":["status"],"properties":{"status":{"enum":["ok","failed"]},"eta":{"type":"integer","maximum":30}}}`),
	})
	store.Put(registry.Contract{Service: "orders", Version: "v2",
		Request:  json.RawMessage(`{"type":"object","required":["id","level"],"properties":{"id":{"type":"string"},"level":{"enum":["low"]}}}`),
		Response: json.RawMessage(`{"type":"object","required":["status"],"properties":{"status":{"enum":["ok","failed","pending"]},"eta":{"type":"integer","maximum":60}}}`),
	})
	reg := httptest.NewServer(store.Handler())
	t.Cleanup(reg.Close)

	clk := clock.NewFake(time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC))
	transport := fault.NewTransport(nil, clk, faultCfg)
	transport.AllowHeaderOverride = true
	srv := httptest.NewServer(NewServer(reg.URL, &http.Client{Transport: transport}, clk))
	t.Cleanup(srv.Close)
	return srv, clk
}

func post(t *testing.T, url, body string) *http.Response {
	t.Helper()
	resp, err := http.Post(url, "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	return resp
}

func TestCheckEndpointReportsBothDirections(t *testing.T) {
	srv, clk := setup(t, fault.Config{})
	resp := post(t, srv.URL+"/v1/compatibility/check", `{"service":"orders","fromVersion":"v1","toVersion":"v2"}`)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var report Report
	if err := json.NewDecoder(resp.Body).Decode(&report); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if report.Status != compat.StatusIncompatible {
		t.Fatalf("overall status = %s", report.Status)
	}
	if report.Request.Status != compat.StatusIncompatible {
		t.Fatalf("request status = %s", report.Request.Status)
	}
	if report.Response.Status != compat.StatusIncompatible {
		t.Fatalf("response status = %s", report.Response.Status)
	}
	if !report.CheckedAt.Equal(clk.Now()) {
		t.Fatalf("checkedAt = %v, want controllable clock time %v", report.CheckedAt, clk.Now())
	}
	// Request direction: new required field "level" must be reported.
	foundRequired := false
	for _, f := range report.Request.Incompatibilities {
		if strings.Contains(f.Reason, `"level"`) {
			foundRequired = true
		}
	}
	if !foundRequired {
		t.Fatalf("request findings missing new-required-field entry: %+v", report.Request.Incompatibilities)
	}
	// Response direction: widened enum "pending" must be reported at $.status.
	foundEnum := false
	for _, f := range report.Response.Incompatibilities {
		if f.Path == "$.status" && f.Counterexample == "pending" {
			foundEnum = true
		}
	}
	if !foundEnum {
		t.Fatalf("response findings missing widened-enum entry: %+v", report.Response.Incompatibilities)
	}
}

func TestCheckEndpointUnknownContract(t *testing.T) {
	srv, _ := setup(t, fault.Config{})
	resp := post(t, srv.URL+"/v1/compatibility/check", `{"service":"orders","fromVersion":"v1","toVersion":"v9"}`)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502", resp.StatusCode)
	}
}

func TestCheckEndpointSurvivesInjectedFaults(t *testing.T) {
	srv, _ := setup(t, fault.Config{ErrorRate: 1.0})
	resp := post(t, srv.URL+"/v1/compatibility/check", `{"service":"orders","fromVersion":"v1","toVersion":"v2"}`)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502 when the registry is faulted", resp.StatusCode)
	}
	var body map[string]string
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["error"] == "" {
		t.Fatal("expected structured error body")
	}
}

func TestCheckEndpointHeaderDrivenFaultInjection(t *testing.T) {
	srv, clk := setup(t, fault.Config{})
	req, err := http.NewRequest("POST", srv.URL+"/v1/compatibility/check",
		strings.NewReader(`{"service":"orders","fromVersion":"v1","toVersion":"v2"}`))
	if err != nil {
		t.Fatalf("NewRequest: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Fault-Error-Rate", "1")
	req.Header.Set("X-Fault-Latency-Ms", "40")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("POST: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502 under header-driven fault injection", resp.StatusCode)
	}
	// The injected latency must have been applied to the controllable clock
	// once per registry fetch (two fetches: old and new contract).
	want := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC).Add(40 * time.Millisecond)
	if !clk.Now().Equal(want) {
		t.Fatalf("clock = %v, want %v (fault stops after first fetch)", clk.Now(), want)
	}
}

func TestCheckInlineEndpoint(t *testing.T) {
	srv, _ := setup(t, fault.Config{})
	body := `{"old":` + oldContract + `,"new":` + newContract + `}`
	resp := post(t, srv.URL+"/v1/compatibility/checkInline", body)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var report Report
	if err := json.NewDecoder(resp.Body).Decode(&report); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if report.Status != compat.StatusIncompatible {
		t.Fatalf("status = %s", report.Status)
	}
}

func TestCheckInlineUnsupportedKeywordIsUnknown(t *testing.T) {
	srv, _ := setup(t, fault.Config{})
	body := `{
		"old": {"request": {"type":"string"}, "response": {"type":"string"}},
		"new": {"request": {"type":"string","minLength":3}, "response": {"type":"string"}}
	}`
	resp := post(t, srv.URL+"/v1/compatibility/checkInline", body)
	defer resp.Body.Close()
	var report Report
	if err := json.NewDecoder(resp.Body).Decode(&report); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if report.Request.Status != compat.StatusUnknown {
		t.Fatalf("request status = %s, want unknown", report.Request.Status)
	}
	if len(report.Request.Unknowns) != 1 || report.Request.Unknowns[0].Keyword != "minLength" {
		t.Fatalf("unknowns = %+v", report.Request.Unknowns)
	}
}

func TestBadRequests(t *testing.T) {
	srv, _ := setup(t, fault.Config{})
	cases := []struct {
		name string
		path string
		body string
	}{
		{"check: invalid JSON", "/v1/compatibility/check", `{not json`},
		{"check: missing fields", "/v1/compatibility/check", `{"service":"orders"}`},
		{"checkInline: invalid JSON", "/v1/compatibility/checkInline", `{not json`},
		{"checkInline: missing schemas", "/v1/compatibility/checkInline", `{"old":{},"new":{}}`},
		{"checkInline: invalid schema", "/v1/compatibility/checkInline",
			`{"old":{"request":{"type":42},"response":{}},"new":{"request":{},"response":{}}}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			resp := post(t, srv.URL+tc.path, tc.body)
			defer resp.Body.Close()
			if resp.StatusCode != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400", resp.StatusCode)
			}
			var body map[string]string
			if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
				t.Fatalf("decode: %v", err)
			}
			if body["error"] == "" {
				t.Fatal("expected structured error body")
			}
		})
	}
}

func TestHealthz(t *testing.T) {
	srv, _ := setup(t, fault.Config{})
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
}

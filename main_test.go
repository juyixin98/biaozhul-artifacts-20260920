package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"pi-sim/sim"
)

func postJSON(t *testing.T, h http.Handler, path string, body any) *httptest.ResponseRecorder {
	t.Helper()
	b, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	req := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(b))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func get(t *testing.T, h http.Handler, path string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
	return rec
}

func TestHealth(t *testing.T) {
	rec := get(t, router{}, "/api/health")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "ok") {
		t.Errorf("body = %s", rec.Body.String())
	}
}

func TestSimulateEndpoint(t *testing.T) {
	wl, _ := sim.Preset("basic")
	rec := postJSON(t, router{}, "/api/simulate", simRequest{
		Inheritance: true,
		Workload:    wl,
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body = %s", rec.Code, rec.Body.String())
	}
	var res sim.Result
	if err := json.Unmarshal(rec.Body.Bytes(), &res); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !res.Inheritance {
		t.Errorf("inheritance flag not echoed")
	}
	if res.Metrics["High"].BlockedTicks != 3 {
		t.Errorf("High blocked = %d, want 3", res.Metrics["High"].BlockedTicks)
	}
	if len(res.Events) == 0 {
		t.Errorf("expected decision trace events")
	}
}

func TestSimulateBadJSON(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/simulate", strings.NewReader("{nope"))
	rec := httptest.NewRecorder()
	router{}.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

func TestSimulateInvalidWorkload(t *testing.T) {
	rec := postJSON(t, router{}, "/api/simulate", simRequest{
		Inheritance: true,
		Workload:    sim.Workload{},
	})
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, want 422", rec.Code)
	}
}

func TestSimulateUnknownField(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/simulate",
		strings.NewReader(`{"inheritance":true,"workload":{"tasks":[]},"bogus":1}`))
	rec := httptest.NewRecorder()
	router{}.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 for unknown field", rec.Code)
	}
}

func TestCompareEndpoint(t *testing.T) {
	wl, _ := sim.Preset("basic")
	rec := postJSON(t, router{}, "/api/compare", wl)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body = %s", rec.Code, rec.Body.String())
	}
	var rep sim.ComparisonReport
	if err := json.Unmarshal(rec.Body.Bytes(), &rep); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if rep.TotalBlockedWithoutPIP <= rep.TotalBlockedWithPIP {
		t.Errorf("PIP did not reduce total blocking: %d -> %d",
			rep.TotalBlockedWithoutPIP, rep.TotalBlockedWithPIP)
	}
	if rep.WithoutInheritance == nil || rep.WithInheritance == nil {
		t.Errorf("both full runs must be present")
	}
}

func TestPresetEndpoints(t *testing.T) {
	h := router{}

	rec := get(t, h, "/api/presets")
	if rec.Code != http.StatusOK {
		t.Fatalf("list status = %d", rec.Code)
	}
	var presets map[string]sim.Workload
	if err := json.Unmarshal(rec.Body.Bytes(), &presets); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if _, ok := presets["basic"]; !ok {
		t.Errorf("basic preset missing")
	}

	rec = get(t, h, "/api/presets/nested")
	if rec.Code != http.StatusOK {
		t.Fatalf("get preset status = %d", rec.Code)
	}
	var wl sim.Workload
	if err := json.Unmarshal(rec.Body.Bytes(), &wl); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if wl.Name != "nested-lock-chain" {
		t.Errorf("preset name = %q", wl.Name)
	}

	rec = get(t, h, "/api/presets/does-not-exist")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}

	rec = postJSON(t, h, "/api/presets/basic/run?inheritance=1", struct{}{})
	if rec.Code != http.StatusOK {
		t.Fatalf("run with PIP status = %d body = %s", rec.Code, rec.Body.String())
	}
	var on sim.Result
	if err := json.Unmarshal(rec.Body.Bytes(), &on); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !on.Inheritance || on.Metrics["High"].BlockedTicks != 3 {
		t.Errorf("unexpected PIP run: %+v", on.Metrics["High"])
	}

	rec = postJSON(t, h, "/api/presets/basic/run?inheritance=0", struct{}{})
	if rec.Code != http.StatusOK {
		t.Fatalf("run without PIP status = %d", rec.Code)
	}
	var off sim.Result
	if err := json.Unmarshal(rec.Body.Bytes(), &off); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if off.Inheritance || off.Metrics["High"].BlockedTicks != 7 {
		t.Errorf("unexpected non-PIP run: %+v", off.Metrics["High"])
	}

	if rec := get(t, h, "/api/presets/basic/run"); rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("GET on run endpoint status = %d, want 405", rec.Code)
	}
}

func TestMethodNotAllowed(t *testing.T) {
	rec := httptest.NewRecorder()
	router{}.ServeHTTP(rec, httptest.NewRequest(http.MethodDelete, "/api/simulate", nil))
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status = %d, want 405", rec.Code)
	}
}

func TestNotFound(t *testing.T) {
	rec := get(t, router{}, "/nope")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
}

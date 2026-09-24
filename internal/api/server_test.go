package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"tztrig/internal/engine"
)

func setup(t *testing.T, start time.Time) (http.Handler, *engine.FakeClock) {
	t.Helper()
	clk := engine.NewFakeClock(start)
	eng := engine.New(clk, engine.WithCatchUpLimit(5))
	return New(eng), clk
}

func TestHealthAndInfo(t *testing.T) {
	h, _ := setup(t, time.Date(2024, 3, 1, 12, 0, 0, 0, time.UTC))
	rr := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/health", nil)
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("health code=%d", rr.Code)
	}

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/api/info", nil))
	var info map[string]any
	if err := json.Unmarshal(rr.Body.Bytes(), &info); err != nil {
		t.Fatal(err)
	}
	if info["tzdataVersion"] != "2024a" {
		t.Fatalf("info = %v", info)
	}
}

func TestScheduleLifecycle(t *testing.T) {
	start := time.Date(2024, 3, 1, 12, 0, 0, 0, time.UTC)
	clk := engine.NewFakeClock(start)
	eng := engine.New(clk, engine.WithCatchUpLimit(5))
	h := New(eng)

	// Create.
	body := `{"id":"nightly","minute":"0","hour":"2","weekday":"*","timezone":"America/New_York"}`
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodPost, "/api/schedules", strings.NewReader(body)))
	if rr.Code != http.StatusCreated {
		t.Fatalf("create code=%d body=%s", rr.Code, rr.Body.String())
	}

	// Duplicate create -> 400.
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodPost, "/api/schedules", strings.NewReader(body)))
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("dup create code=%d", rr.Code)
	}

	// Invalid zone -> 400.
	bad := `{"id":"x","minute":"0","hour":"0","weekday":"*","timezone":"Mars/Olympus"}`
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodPost, "/api/schedules", strings.NewReader(bad)))
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("invalid zone code=%d", rr.Code)
	}

	// Malformed JSON -> 400.
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodPost, "/api/schedules", strings.NewReader("{nope")))
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("bad json code=%d", rr.Code)
	}

	// Get.
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/api/schedules/nightly", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("get code=%d", rr.Code)
	}

	// Get missing -> 404.
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/api/schedules/nope", nil))
	if rr.Code != http.StatusNotFound {
		t.Fatalf("missing code=%d", rr.Code)
	}

	// Next preview: 02:00 NY on 2024-03-02 = 07:00 UTC (EST).
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/api/schedules/nightly/next?n=2", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("next code=%d body=%s", rr.Code, rr.Body.String())
	}
	var nextResp struct {
		Next []time.Time `json:"next"`
	}
	if err := json.Unmarshal(rr.Body.Bytes(), &nextResp); err != nil {
		t.Fatal(err)
	}
	if len(nextResp.Next) != 2 {
		t.Fatalf("next count=%d", len(nextResp.Next))
	}
	if want := time.Date(2024, 3, 2, 7, 0, 0, 0, time.UTC); !nextResp.Next[0].Equal(want) {
		t.Fatalf("next[0]=%s want %s", nextResp.Next[0], want)
	}

	// Pause, advance clock a day, expect no fires; resume and catch up.
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodPost, "/api/schedules/nightly/pause", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("pause code=%d", rr.Code)
	}
	clk.Set(time.Date(2024, 3, 3, 12, 0, 0, 0, time.UTC))
	eng.Advance(clk.Now())
	if n := len(eng.History(100)); n != 0 {
		t.Fatalf("paused schedule fired %d times", n)
	}

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodPost, "/api/schedules/nightly/resume", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("resume code=%d", rr.Code)
	}
	eng.Advance(clk.Now())
	fires := eng.History(100)
	if len(fires) == 0 {
		t.Fatal("resume did not catch up")
	}

	// Fires endpoint exposes history.
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/api/fires", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("fires code=%d", rr.Code)
	}

	// Delete -> 204, then get -> 404.
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodDelete, "/api/schedules/nightly", nil))
	if rr.Code != http.StatusNoContent {
		t.Fatalf("delete code=%d", rr.Code)
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/api/schedules/nightly", nil))
	if rr.Code != http.StatusNotFound {
		t.Fatalf("post-delete get code=%d", rr.Code)
	}
}

func TestListZones(t *testing.T) {
	h, _ := setup(t, time.Now())
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/api/zones?prefix=America/New", nil))
	var resp struct {
		Zones []string `json:"zones"`
	}
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, z := range resp.Zones {
		if z == "America/New_York" {
			found = true
		}
	}
	if !found {
		t.Fatalf("America/New_York not in %v", resp.Zones)
	}
}

func TestMethodNotAllowedByMux(t *testing.T) {
	h, _ := setup(t, time.Now())
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodPut, "/api/schedules", bytes.NewReader(nil)))
	// Go 1.22+ mux returns 405 for registered-but-different method.
	if rr.Code != http.StatusMethodNotAllowed {
		t.Fatalf("PUT /api/schedules code=%d", rr.Code)
	}
}

package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"hlcservice/internal/hlc"
)

type frozenClock struct{ ms int64 }

func (f frozenClock) Now() time.Time { return time.UnixMilli(f.ms) }

func testClock(t *testing.T, node string, frozenMS int64) *hlc.Clock {
	t.Helper()
	c, err := hlc.NewClock(hlc.Config{
		NodeID:              node,
		MaxDriftMS:          100,
		MaxLogical:          hlc.DefaultMaxLogical,
		OverflowWaitTimeout: time.Second,
		Physical:            frozenClock{frozenMS},
	})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func tsFrom(t *testing.T, body map[string]any) (int64, uint64, string) {
	t.Helper()
	inner, ok := body["timestamp"].(map[string]any)
	if !ok {
		t.Fatalf("missing timestamp: %v", body)
	}
	return int64(inner["physical_ms"].(float64)),
		uint64(inner["logical"].(float64)),
		inner["node_id"].(string)
}

func do(t *testing.T, h http.Handler, method, target, body string) (int, map[string]any) {
	t.Helper()
	rec := httptest.NewRecorder()
	var rdr *strings.Reader
	if body != "" {
		rdr = strings.NewReader(body)
	} else {
		rdr = strings.NewReader("")
	}
	h.ServeHTTP(rec, httptest.NewRequest(method, target, rdr))
	out := map[string]any{}
	if rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatalf("non-JSON %q: %v", rec.Body.String(), err)
		}
	}
	return rec.Code, out
}

func TestHealthIncludesPhysicalNow(t *testing.T) {
	h := New(testClock(t, "srv", 1234)).Handler()
	code, body := do(t, h, http.MethodGet, "/healthz", "")
	if code != http.StatusOK {
		t.Fatalf("status %d", code)
	}
	if body["status"] != "ok" || body["node_id"] != "srv" {
		t.Fatalf("health body %v", body)
	}
	if int64(body["physical_now_ms"].(float64)) != 1234 {
		t.Fatalf("physical_now_ms = %v", body["physical_now_ms"])
	}
}

func TestTickNowAndAliases(t *testing.T) {
	h := New(testClock(t, "srv", 1000)).Handler()

	// /v1/tick and the /tick alias advance the same clock.
	code, b1 := do(t, h, http.MethodPost, "/v1/tick", "")
	if code != http.StatusOK {
		t.Fatalf("tick %d", code)
	}
	p, l, node := tsFrom(t, b1)
	if p != 1000 || l != 1 || node != "srv" {
		t.Fatalf("first tick %v %v %v", p, l, node)
	}

	code, b2 := do(t, h, http.MethodPost, "/tick", "")
	if code != http.StatusOK {
		t.Fatalf("alias tick %d", code)
	}
	_, l2, _ := tsFrom(t, b2)
	if l2 != 2 {
		t.Fatalf("alias tick logical=%d want 2", l2)
	}

	// /v1/now and /timestamp do not advance the clock.
	code, before := do(t, h, http.MethodGet, "/v1/now", "")
	if code != http.StatusOK {
		t.Fatalf("now %d", code)
	}
	do(t, h, http.MethodGet, "/timestamp", "")
	_, after := do(t, h, http.MethodGet, "/v1/now", "")
	bp, bl, _ := tsFrom(t, before)
	ap, al, _ := tsFrom(t, after)
	if bp != ap || bl != al {
		t.Fatalf("now advanced the clock: (%d,%d) -> (%d,%d)", bp, bl, ap, al)
	}
}

func TestReceiveShapesMergeAndReject(t *testing.T) {
	h := New(testClock(t, "srv", 1000)).Handler()

	// Envelope object.
	code, body := do(t, h, http.MethodPost, "/v1/receive",
		`{"timestamp":{"physical_ms":1000,"logical":10,"node_id":"peer"}}`)
	if code != http.StatusOK {
		t.Fatalf("envelope %d %v", code, body)
	}
	if p, l, _ := tsFrom(t, body); p != 1000 || l != 11 {
		t.Fatalf("envelope merge %v %v", p, l)
	}

	// Bare timestamp object.
	_, body = do(t, h, http.MethodPost, "/v1/receive",
		`{"physical_ms":1000,"logical":20,"node_id":"peer"}`)
	if _, l, _ := tsFrom(t, body); l != 21 {
		t.Fatalf("bare merge l=%v", l)
	}

	// JSON-quoted wire string.
	_, body = do(t, h, http.MethodPost, "/v1/receive", `"hlc://peer/1000:30"`)
	if _, l, _ := tsFrom(t, body); l != 31 {
		t.Fatalf("quoted-wire merge l=%v", l)
	}

	// Raw canonical text body.
	_, body = do(t, h, http.MethodPost, "/v1/receive", `hlc://peer/1000:40`)
	if _, l, _ := tsFrom(t, body); l != 41 {
		t.Fatalf("raw-wire merge l=%v", l)
	}

	// Far-future value -> 422 future_drift.
	code, body = do(t, h, http.MethodPost, "/v1/receive",
		`{"physical_ms":100000,"logical":0,"node_id":"evil"}`)
	if code != http.StatusUnprocessableEntity || body["code"] != "future_drift" {
		t.Fatalf("drift %d %v", code, body)
	}
}

func TestReceiveBadBodies(t *testing.T) {
	h := New(testClock(t, "srv", 1000)).Handler()
	for _, tc := range []struct{ name, body string }{
		{"empty", ``},
		{"not json", `xxx`},
		{"negative physical", `{"physical_ms":-1,"logical":0,"node_id":"x"}`},
		{"missing node", `{"physical_ms":1,"logical":0}`},
		{"bad wire", `hlc://n/x:2`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			code, body := do(t, h, http.MethodPost, "/v1/receive", tc.body)
			if code != http.StatusBadRequest {
				t.Fatalf("status %d want 400 body %v", code, body)
			}
		})
	}
}

func TestStatusCounters(t *testing.T) {
	h := New(testClock(t, "srv", 1000)).Handler()
	do(t, h, http.MethodPost, "/v1/tick", "")
	do(t, h, http.MethodPost, "/v1/receive", `hlc://p/1000:5`)
	do(t, h, http.MethodPost, "/v1/receive", `{"physical_ms":999999,"logical":0,"node_id":"e"}`)

	code, st := do(t, h, http.MethodGet, "/v1/status", "")
	if code != http.StatusOK {
		t.Fatalf("status %d", code)
	}
	if st["tick_count"].(float64) != 1 {
		t.Fatalf("tick_count %v", st["tick_count"])
	}
	if st["receive_count"].(float64) != 2 {
		t.Fatalf("receive_count %v", st["receive_count"])
	}
	if st["drift_rejects"].(float64) != 1 {
		t.Fatalf("drift_rejects %v", st["drift_rejects"])
	}
	if st["max_logical"].(float64) != 4294967295 {
		t.Fatalf("max_logical %v", st["max_logical"])
	}
	if st["max_drift_ms"].(float64) != 100 {
		t.Fatalf("max_drift_ms %v", st["max_drift_ms"])
	}
	if _, ok := st["skew_ms"]; !ok {
		t.Fatal("status missing skew_ms")
	}
}

func TestMethodRouting(t *testing.T) {
	h := New(testClock(t, "srv", 1000)).Handler()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodDelete, "/v1/tick", nil))
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("DELETE /v1/tick = %d want 405", rec.Code)
	}
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/nope", nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("unknown path = %d want 404", rec.Code)
	}
}

func TestConcurrentTicksOverHTTP(t *testing.T) {
	h := New(testClock(t, "srv", 1000)).Handler()
	const n = 64
	var wg sync.WaitGroup
	errs := make(chan int, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/tick", nil))
			if rec.Code != http.StatusOK {
				errs <- rec.Code
			}
		}()
	}
	wg.Wait()
	close(errs)
	for code := range errs {
		t.Fatalf("tick status %d", code)
	}
}

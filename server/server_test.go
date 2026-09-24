package server

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"hlc-service/hlc"
)

type fakeClock struct {
	mu  sync.Mutex
	now int64
}

func (f *fakeClock) get() int64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

func (f *fakeClock) set(v int64) {
	f.mu.Lock()
	f.now = v
	f.mu.Unlock()
}

func (f *fakeClock) advance(target int64, deadline time.Time) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.now < target {
		f.now = target
	}
	return nil
}

func newTestServer(t *testing.T, node string, start int64, opts ...hlc.Option) (*Server, *fakeClock) {
	t.Helper()
	f := &fakeClock{now: start}
	opts = append([]hlc.Option{hlc.WithPhysicalFunc(f.get), hlc.WithWaitFunc(f.advance)}, opts...)
	c, err := hlc.New(node, opts...)
	if err != nil {
		t.Fatalf("hlc.New: %v", err)
	}
	return New(c), f
}

type tsBody struct {
	NodeID    string        `json:"node_id"`
	Timestamp hlc.Timestamp `json:"timestamp"`
}

func postTick(t *testing.T, srv *Server) hlc.Timestamp {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/tick", nil)
	rec := httptest.NewRecorder()
	srv.Mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("tick status %d: %s", rec.Code, rec.Body.String())
	}
	var body tsBody
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	return body.Timestamp
}

func postReceive(t *testing.T, srv *Server, payload string) (int, hlc.Timestamp) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/receive", strings.NewReader(payload))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	srv.Mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		return rec.Code, hlc.Timestamp{}
	}
	var body tsBody
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	return rec.Code, body.Timestamp
}

func TestHTTPTickStrictlyIncreasing(t *testing.T) {
	srv, _ := newTestServer(t, "http-a", 5000)
	var prev hlc.Timestamp
	for i := 0; i < 500; i++ {
		ts := postTick(t, srv)
		if i > 0 && !prev.Less(ts) {
			t.Fatalf("tick %d: %s !< %s", i, prev.Wire(), ts.Wire())
		}
		prev = ts
	}
	if prev.Physical != 5000 || prev.Logical != 500 {
		t.Fatalf("last = %s want (5000,500)", prev.Wire())
	}
}

func TestHTTPCausalChainThreeServers(t *testing.T) {
	// Acceptance over HTTP: a (wall 10000) sends -> b (wall 9000) ->
	// c (wall 9500) -> back to a; delivered timestamps strictly increase.
	a, _ := newTestServer(t, "ha", 10000)
	b, _ := newTestServer(t, "hb", 9000)
	cc, _ := newTestServer(t, "hc", 9500)

	m1 := postTick(t, a)

	status, r1 := postReceive(t, b, mustEnvelope(t, m1))
	if status != 200 {
		t.Fatal("b receive m1")
	}
	m2 := postTick(t, b)

	status, r2 := postReceive(t, cc, mustEnvelope(t, m2))
	if status != 200 || !r1.Less(r2) {
		t.Fatalf("c receive m2: status=%d, %s !< %s", status, r1.Wire(), r2.Wire())
	}
	m3 := postTick(t, cc)

	status, r3 := postReceive(t, a, mustEnvelope(t, m3))
	if status != 200 || !m3.Less(r3) {
		t.Fatalf("a receive m3: status=%d, %s !< %s", status, m3.Wire(), r3.Wire())
	}
	chain := []hlc.Timestamp{m1, r1, m2, r2, m3, r3}
	for i := 1; i < len(chain); i++ {
		if !chain[i-1].Less(chain[i]) {
			t.Fatalf("HTTP causal chain broken at %d: %s !< %s",
				i, chain[i-1].Wire(), chain[i].Wire())
		}
	}
}

func TestHTTPReceiveAcceptsAllBodyShapes(t *testing.T) {
	srv, _ := newTestServer(t, "shapes", 1000, hlc.WithMaxDrift(1_000_000))
	payloads := []string{
		`{"timestamp":{"physical_ms":2000,"logical":3,"node_id":"r"}}`,
		`{"physical_ms":2001,"logical":3,"node_id":"r"}`,
		`"hlc://r/2002:3"`,
		`hlc://r/2003:3`,
		`{"timestamp":"hlc://r/2004:3"}`,
	}
	prev := postTick(t, srv)
	for i, p := range payloads {
		status, ts := postReceive(t, srv, p)
		if status != http.StatusOK {
			t.Fatalf("payload %d (%s) status %d", i, p, status)
		}
		if !prev.Less(ts) {
			t.Fatalf("payload %d: %s !< %s", i, prev.Wire(), ts.Wire())
		}
		prev = ts
	}
}

func TestHTTPFutureDriftRejected422(t *testing.T) {
	srv, _ := newTestServer(t, "d", 1000, hlc.WithMaxDrift(100))
	body := `{"timestamp":{"physical_ms":99999999999,"logical":0,"node_id":"far"}}`
	req := httptest.NewRequest(http.MethodPost, "/v1/receive", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	srv.Mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, want 422; body %s", rec.Code, rec.Body.String())
	}
	var errBody struct {
		Code string `json:"code"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &errBody); err != nil {
		t.Fatal(err)
	}
	if errBody.Code != "future_drift" {
		t.Fatalf("code = %q, want future_drift", errBody.Code)
	}
	// Server state must remain usable.
	if ts := postTick(t, srv); ts.Physical != 1000 || ts.Logical != 1 {
		t.Fatalf("tick after reject = %s", ts.Wire())
	}
}

func TestHTTPBadBodies(t *testing.T) {
	srv, _ := newTestServer(t, "bad", 1000)
	cases := map[string]string{
		"empty":         ``,
		"not json":      `not-json`,
		"missing node":  `{"physical_ms":1000,"logical":0}`,
		"bad wire":      `hlc:///x`,
		"negative phys": `{"physical_ms":-1,"logical":0,"node_id":"r"}`,
		"non-number":    `{"physical_ms":"abc","logical":0,"node_id":"r"}`,
	}
	for name, body := range cases {
		req := httptest.NewRequest(http.MethodPost, "/v1/receive", strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		srv.Mux().ServeHTTP(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("%s: status = %d want 400: %s", name, rec.Code, rec.Body.String())
		}
	}
}

func TestHTTPOverflowReturns503(t *testing.T) {
	srv, _ := newTestServer(t, "ov", 1000, hlc.WithMaxLogical(2),
		hlc.WithWaitFunc(func(int64, time.Time) error { return hlc.ErrOverflow }))
	for i := 0; i < 2; i++ {
		postTick(t, srv)
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/tick", nil)
	rec := httptest.NewRecorder()
	srv.Mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d want 503: %s", rec.Code, rec.Body.String())
	}
}

func TestHTTPStatusAndHealth(t *testing.T) {
	srv, f := newTestServer(t, "st", 1000)
	postTick(t, srv)
	postTick(t, srv)
	f.set(1005)

	req := httptest.NewRequest(http.MethodGet, "/v1/status", nil)
	rec := httptest.NewRecorder()
	srv.Mux().ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("status %d", rec.Code)
	}
	var st hlc.Stats
	if err := json.Unmarshal(rec.Body.Bytes(), &st); err != nil {
		t.Fatal(err)
	}
	if st.TickCount != 2 || st.PhysicalNow != 1005 || st.SkewMs != -5 {
		t.Fatalf("unexpected stats: %+v", st)
	}

	req = httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec = httptest.NewRecorder()
	srv.Mux().ServeHTTP(rec, req)
	if rec.Code != 200 || !bytes.Contains(rec.Body.Bytes(), []byte(`"ok"`)) {
		t.Fatalf("health: %d %s", rec.Code, rec.Body.String())
	}
}

func TestHTTPJSONPrecisionRoundTrip(t *testing.T) {
	// Huge values ride HTTP JSON without precision loss.
	srv, _ := newTestServer(t, "prec", 1000,
		hlc.WithMaxDrift(1<<62), hlc.WithMaxLogical(1<<64-1))
	// Use logical = 2^64-2 so the receive merge (+1) lands exactly on
	// 2^64-1 without triggering overflow handling; the full-range integers
	// must ride JSON request/response without precision loss.
	huge := hlc.Timestamp{Physical: 1<<62 - 1, Logical: 1<<64 - 2, NodeID: "big"}
	raw, err := json.Marshal(map[string]any{"timestamp": huge})
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/receive", bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	srv.Mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	data, _ := io.ReadAll(rec.Body)
	var body tsBody
	if err := json.Unmarshal(data, &body); err != nil {
		t.Fatalf("response parse: %v (%s)", err, data)
	}
	got := body.Timestamp
	if got.Physical != 1<<62-1 || got.Logical != 1<<64-1 {
		t.Fatalf("precision lost: physical=%d logical=%d", got.Physical, got.Logical)
	}
}

// TestHTTPJSONUint64MaxOverflowProvesWraparoundFixed verifies the wire carries
// a max-range logical counter and the server handles it by advancing physical
// time (never emitting a wrapped-around 0 counter at the same physical part).
func TestHTTPJSONUint64MaxOverflowProvesWraparoundFixed(t *testing.T) {
	srv, _ := newTestServer(t, "wrap", 1000,
		hlc.WithMaxDrift(1<<62), hlc.WithMaxLogical(1<<64-1))
	huge := hlc.Timestamp{Physical: 1<<62 - 1, Logical: 1<<64 - 1, NodeID: "big"}
	raw, err := json.Marshal(map[string]any{"timestamp": huge})
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/receive", bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	srv.Mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	var body tsBody
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.Timestamp.Physical != 1<<62 || body.Timestamp.Logical != 0 {
		t.Fatalf("overflow handling wrong: got %s, want (%d,0)",
			body.Timestamp.Wire(), int64(1<<62))
	}
}

func TestHTTPNowPeekDoesNotAdvance(t *testing.T) {
	srv, _ := newTestServer(t, "now", 1000)
	postTick(t, srv)
	get := func() hlc.Timestamp {
		req := httptest.NewRequest(http.MethodGet, "/v1/now", nil)
		rec := httptest.NewRecorder()
		srv.Mux().ServeHTTP(rec, req)
		if rec.Code != 200 {
			t.Fatalf("now status %d", rec.Code)
		}
		var body tsBody
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatal(err)
		}
		return body.Timestamp
	}
	first := get()
	second := get()
	if first != second {
		t.Fatalf("GET /v1/now advanced the clock: %s -> %s", first.Wire(), second.Wire())
	}
}

func mustEnvelope(t *testing.T, ts hlc.Timestamp) string {
	t.Helper()
	raw, err := json.Marshal(map[string]any{"timestamp": ts})
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

package server

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"idemresp/internal/client"
	"idemresp/internal/clock"
	"idemresp/internal/gateway"
	"idemresp/internal/idem"
	"idemresp/internal/txn"
)

type env struct {
	app *httptest.Server
	gw  *httptest.Server
	db  *txn.DB
}

func newEnv(t *testing.T, allowCrash bool) *env {
	t.Helper()
	db, err := txn.Open(t.TempDir(), clock.Real{})
	if err != nil {
		t.Fatalf("db: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })

	gwSrv := gateway.NewServer(nil)
	gwTS := httptest.NewServer(gwSrv.Handler())
	t.Cleanup(gwTS.Close)

	gwClient := client.New(gwTS.URL, time.Second)
	svc := idem.New(idem.Config{
		DB: db, GW: gwClient, Clk: clock.Real{},
		Lease: 30 * time.Second, AmbiguousRetries: 1,
		Logf: func(string, ...any) {},
	})
	h := New(Deps{Service: svc, DB: db, AllowCrash: allowCrash})
	appTS := httptest.NewServer(h)
	t.Cleanup(appTS.Close)
	return &env{app: appTS, gw: gwTS, db: db}
}

func doOrder(t *testing.T, base, key, body string, hdr map[string]string) *http.Response {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, base+"/v1/orders", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if key != "" {
		req.Header.Set("Idempotency-Key", key)
	}
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("http: %v", err)
	}
	return resp
}

func TestMissingIdempotencyKey(t *testing.T) {
	e := newEnv(t, false)
	resp := doOrder(t, e.app.URL, "", `{"amount":1}`, nil)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", resp.StatusCode)
	}
}

func TestInvalidJSONRejected(t *testing.T) {
	e := newEnv(t, false)
	resp := doOrder(t, e.app.URL, "k-badjson", "not-json", nil)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", resp.StatusCode)
	}
}

func TestBodyTooLarge(t *testing.T) {
	e := newEnv(t, false)
	big := strings.Repeat("x", maxBodyBytes+10)
	resp := doOrder(t, e.app.URL, "k-big", big, nil)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want 413", resp.StatusCode)
	}
}

func TestCreateThenReadOrderAndLedger(t *testing.T) {
	e := newEnv(t, false)
	key := "k-read"
	resp := doOrder(t, e.app.URL, key, `{"amount":250,"currency":"EUR"}`, nil)
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("create: %d %s", resp.StatusCode, body)
	}
	var created struct {
		Order struct {
			ID string `json:"id"`
		} `json:"order"`
	}
	if err := json.Unmarshal(body, &created); err != nil {
		t.Fatalf("decode: %v", err)
	}

	// GET the order back.
	getResp, err := http.Get(e.app.URL + "/v1/orders?id=" + created.Order.ID)
	if err != nil {
		t.Fatalf("get order: %v", err)
	}
	defer getResp.Body.Close()
	if getResp.StatusCode != http.StatusOK {
		t.Fatalf("get order status = %d", getResp.StatusCode)
	}

	// Missing id and unknown id.
	r1 := get(t, e.app.URL+"/v1/orders")
	defer r1.Body.Close()
	if r1.StatusCode != http.StatusBadRequest {
		t.Fatalf("missing id: %d", r1.StatusCode)
	}
	r2 := get(t, e.app.URL+"/v1/orders?id=nope")
	defer r2.Body.Close()
	if r2.StatusCode != http.StatusNotFound {
		t.Fatalf("unknown id: %d", r2.StatusCode)
	}

	// Ledger filtered by key and key introspection.
	lr := get(t, e.app.URL+"/v1/ledger?key="+key)
	defer lr.Body.Close()
	if lr.StatusCode != http.StatusOK {
		t.Fatalf("ledger: %d", lr.StatusCode)
	}
	var lm struct {
		Count int `json:"count"`
	}
	_ = json.NewDecoder(lr.Body).Decode(&lm)
	if lm.Count != 1 {
		t.Fatalf("ledger count = %d, want 1", lm.Count)
	}

	kr := get(t, e.app.URL+"/v1/keys/"+key)
	defer kr.Body.Close()
	if kr.StatusCode != http.StatusOK {
		t.Fatalf("key introspection: %d", kr.StatusCode)
	}
	mr := get(t, e.app.URL+"/v1/keys/missing")
	defer mr.Body.Close()
	if mr.StatusCode != http.StatusNotFound {
		t.Fatalf("missing key: %d", mr.StatusCode)
	}
}

func get(t *testing.T, url string) *http.Response {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	return resp
}

func TestReplayHeaderAndConflict(t *testing.T) {
	e := newEnv(t, false)
	key := "k-replay-hdr"
	r1 := doOrder(t, e.app.URL, key, `{"amount":10,"currency":"USD"}`, nil)
	_, _ = io.Copy(io.Discard, r1.Body)
	r1.Body.Close()

	r2 := doOrder(t, e.app.URL, key, `{"amount":10,"currency":"USD"}`, nil)
	_, _ = io.Copy(io.Discard, r2.Body)
	r2.Body.Close()
	if r2.Header.Get("Idempotent-Replay") != "true" {
		t.Fatalf("replay header = %q", r2.Header.Get("Idempotent-Replay"))
	}
	if r2.Header.Get("Idempotency-Key") != key {
		t.Fatalf("echo key header = %q", r2.Header.Get("Idempotency-Key"))
	}

	r3 := doOrder(t, e.app.URL, key, `{"amount":11,"currency":"USD"}`, nil)
	b3, _ := io.ReadAll(r3.Body)
	r3.Body.Close()
	if r3.StatusCode != http.StatusConflict {
		t.Fatalf("conflict status = %d: %s", r3.StatusCode, b3)
	}
}

func TestHealthz(t *testing.T) {
	e := newEnv(t, false)
	resp, err := http.Get(e.app.URL + "/healthz")
	if err != nil {
		t.Fatalf("healthz: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz status = %d", resp.StatusCode)
	}
}

func TestCrashHeaderIgnoredWhenNotAllowed(t *testing.T) {
	e := newEnv(t, false)
	resp := doOrder(t, e.app.URL, "k-nocrash", `{"amount":1,"currency":"USD"}`,
		map[string]string{"X-Crash": "before-commit"})
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("crash header must be ignored without --allow-crash: %d %s", resp.StatusCode, body)
	}
}

func TestParseDurAndTruthy(t *testing.T) {
	if parseDur("not-a-duration") != 0 {
		t.Fatal("invalid duration should be 0")
	}
	if parseDur("250ms") != 250*time.Millisecond {
		t.Fatal("valid duration misparsed")
	}
	for _, v := range []string{"1", "true", "YES", " on "} {
		if !truthy(v) {
			t.Fatalf("truthy(%q) = false", v)
		}
	}
	if truthy("no") {
		t.Fatal("truthy(no) = true")
	}
}

func TestStatusRecorder(t *testing.T) {
	rec := httptest.NewRecorder()
	sr := &statusRecorder{ResponseWriter: rec, status: http.StatusOK}
	sr.WriteHeader(http.StatusTeapot)
	if sr.status != http.StatusTeapot || rec.Code != http.StatusTeapot {
		t.Fatalf("recorded=%d underlying=%d", sr.status, rec.Code)
	}
	// Flush must not panic.
	sr.Flush()

	var buf bytes.Buffer
	h := logging(testLogger(&buf), http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusAccepted)
	}))
	req := httptest.NewRequest(http.MethodGet, "/v1/orders", nil)
	h.ServeHTTP(httptest.NewRecorder(), req)
	if !strings.Contains(buf.String(), "202") {
		t.Fatalf("log missing status: %q", buf.String())
	}
}

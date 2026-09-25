package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"histmerge/internal/store"
)

func newTestServer(t *testing.T) *Server {
	t.Helper()
	st, err := store.Open("") // in-memory only
	if err != nil {
		t.Fatal(err)
	}
	return New(st)
}

func do(t *testing.T, srv *Server, method, path, body string) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	var rdr *strings.Reader
	if body != "" {
		rdr = strings.NewReader(body)
	} else {
		rdr = strings.NewReader("")
	}
	req := httptest.NewRequest(method, path, rdr)
	rec := httptest.NewRecorder()
	srv.ServeHTTP(rec, req)
	var out map[string]any
	if rec.Body.Len() > 0 {
		_ = json.Unmarshal(rec.Body.Bytes(), &out)
	}
	return rec, out
}

func TestHTTPIngestAndQuery(t *testing.T) {
	srv := newTestServer(t)

	rec, _ := do(t, srv, "POST", "/v1/histograms",
		`{"name":"h1","bounds":[1,2,"+Inf"],"counts":[3,5,9]}`)
	if rec.Code != http.StatusCreated {
		t.Fatalf("ingest: code=%d body=%s", rec.Code, rec.Body)
	}

	rec, out := do(t, srv, "GET", "/v1/quantile?name=h1&q=0.5", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("quantile: code=%d body=%s", rec.Code, rec.Body)
	}
	if out["lower"].(float64) != 1 || out["upper"].(float64) != 2 {
		t.Fatalf("quantile interval=%v..%v, want 1..2", out["lower"], out["upper"])
	}
}

func TestHTTPRejectsBadCumulativeCounts(t *testing.T) {
	srv := newTestServer(t)
	rec, out := do(t, srv, "POST", "/v1/histograms",
		`{"name":"bad","bounds":[1,"+Inf"],"counts":[9,5]}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("code=%d, want 400", rec.Code)
	}
	if !strings.Contains(out["error"].(string), "non-decreasing") {
		t.Fatalf("error=%v", out["error"])
	}
}

func TestHTTPMergeDifferentBounds(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, "POST", "/v1/histograms", `{"name":"fine","bounds":[0.5,1,"+Inf"],"counts":[2,6,10]}`)
	do(t, srv, "POST", "/v1/histograms", `{"name":"coarse","bounds":[1,"+Inf"],"counts":[4,7]}`)

	rec, out := do(t, srv, "POST", "/v1/merge", `{"names":["fine","coarse"]}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("merge: code=%d body=%s", rec.Code, rec.Body)
	}
	counts := out["counts"].([]any)
	// common bounds {1, +Inf}: fine rebins to {6,10}; merged {10,17}
	if counts[0].(float64) != 10 || counts[1].(float64) != 17 {
		t.Fatalf("merged counts=%v, want [10 17]", counts)
	}
}

func TestHTTPQuantileEmptyHistogram(t *testing.T) {
	srv := newTestServer(t)
	do(t, srv, "POST", "/v1/histograms", `{"name":"empty","bounds":[1,"+Inf"],"counts":[0,0]}`)
	rec, out := do(t, srv, "GET", "/v1/quantile?name=empty&q=0.9", "")
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("code=%d, want 422", rec.Code)
	}
	if !strings.Contains(out["error"].(string), "empty") {
		t.Fatalf("error=%v", out["error"])
	}
}

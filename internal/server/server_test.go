package server

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"httprange/internal/artifact"
	"httprange/internal/clock"
)

func testStore() *artifact.Store {
	t0 := time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)
	data := make([]byte, 1024)
	for i := range data {
		data[i] = byte(i % 251)
	}
	return artifact.NewStore().
		Put(artifact.New("sample.bin", data, "application/octet-stream", t0)).
		Put(artifact.New("empty.bin", []byte{}, "application/octet-stream", t0))
}

func newTestServer(opts ...Option) *Server {
	clk := clock.NewFake(time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC))
	return New(testStore(), clk, opts...)
}

func do(s *Server, method, target string, hdr map[string]string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, target, nil)
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	s.Routes().ServeHTTP(rec, req)
	return rec
}

func TestFullGetHeaders(t *testing.T) {
	rec := do(newTestServer(), http.MethodGet, "/artifacts/sample.bin", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d", rec.Code)
	}
	if rec.Header().Get("Accept-Ranges") != "bytes" {
		t.Error("missing Accept-Ranges")
	}
	if rec.Body.Len() != 1024 {
		t.Errorf("body %d", rec.Body.Len())
	}
	if !strings.HasPrefix(rec.Header().Get("ETag"), `"`) {
		t.Errorf("etag %q", rec.Header().Get("ETag"))
	}
}

func TestPrefixSingleRange(t *testing.T) {
	rec := do(newTestServer(), http.MethodGet, "/artifacts/sample.bin",
		map[string]string{"Range": "bytes=0-99"})
	if rec.Code != http.StatusPartialContent {
		t.Fatalf("status %d", rec.Code)
	}
	if rec.Header().Get("Content-Range") != "bytes 0-99/1024" {
		t.Errorf("CR %q", rec.Header().Get("Content-Range"))
	}
	if rec.Body.Len() != 100 {
		t.Errorf("body %d", rec.Body.Len())
	}
}

func TestSuffixRange(t *testing.T) {
	rec := do(newTestServer(), http.MethodGet, "/artifacts/sample.bin",
		map[string]string{"Range": "bytes=-200"})
	if rec.Header().Get("Content-Range") != "bytes 824-1023/1024" {
		t.Errorf("CR %q", rec.Header().Get("Content-Range"))
	}
}

func TestUnsatisfiable(t *testing.T) {
	rec := do(newTestServer(), http.MethodGet, "/artifacts/sample.bin",
		map[string]string{"Range": "bytes=2000-"})
	if rec.Code != http.StatusRequestedRangeNotSatisfiable {
		t.Fatalf("status %d", rec.Code)
	}
	if rec.Header().Get("Content-Range") != "bytes */1024" {
		t.Errorf("CR %q", rec.Header().Get("Content-Range"))
	}
}

func TestEmptyArtifactRange(t *testing.T) {
	rec := do(newTestServer(), http.MethodGet, "/artifacts/empty.bin",
		map[string]string{"Range": "bytes=0-0"})
	if rec.Code != http.StatusRequestedRangeNotSatisfiable {
		t.Fatalf("status %d", rec.Code)
	}
	if rec.Header().Get("Content-Range") != "bytes */0" {
		t.Errorf("CR %q", rec.Header().Get("Content-Range"))
	}
}

func TestMalformedRangeIgnored(t *testing.T) {
	rec := do(newTestServer(), http.MethodGet, "/artifacts/sample.bin",
		map[string]string{"Range": "bytes=abc"})
	if rec.Code != http.StatusOK || rec.Body.Len() != 1024 {
		t.Fatalf("status %d body %d", rec.Code, rec.Body.Len())
	}
}

func TestMultipartRanges(t *testing.T) {
	rec := do(newTestServer(), http.MethodGet, "/artifacts/sample.bin",
		map[string]string{"Range": "bytes=0-9,20-29"})
	if rec.Code != http.StatusPartialContent {
		t.Fatalf("status %d", rec.Code)
	}
	ct := rec.Header().Get("Content-Type")
	if !strings.HasPrefix(ct, "multipart/byteranges; boundary=") {
		t.Fatalf("CT %q", ct)
	}
	body := rec.Body.String()
	if !strings.Contains(body, "Content-Range: bytes 0-9/1024") {
		t.Error("missing first part CR")
	}
	if !strings.Contains(body, "Content-Range: bytes 20-29/1024") {
		t.Error("missing second part CR")
	}
}

func TestRangeLimitIgnored(t *testing.T) {
	s := newTestServer(WithMaxRanges(2))
	rec := do(s, http.MethodGet, "/artifacts/sample.bin",
		map[string]string{"Range": "bytes=0-1,2-3,4-5"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d, want 200 for over-limit set", rec.Code)
	}
	if rec.Header().Get("X-Range-Member-Limit") != "2" {
		t.Errorf("limit header %q", rec.Header().Get("X-Range-Member-Limit"))
	}
}

func TestIfRangeETagMismatch(t *testing.T) {
	rec := do(newTestServer(), http.MethodGet, "/artifacts/sample.bin",
		map[string]string{"Range": "bytes=0-9", "If-Range": `"deadbeef"`})
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d, want 200 on If-Range mismatch", rec.Code)
	}
}

func TestIfRangeMatchingETag(t *testing.T) {
	s := newTestServer()
	full := do(s, http.MethodGet, "/artifacts/sample.bin", nil)
	etag := full.Header().Get("ETag")
	rec := do(s, http.MethodGet, "/artifacts/sample.bin",
		map[string]string{"Range": "bytes=0-9", "If-Range": etag})
	if rec.Code != http.StatusPartialContent {
		t.Fatalf("status %d", rec.Code)
	}
}

func TestIfNoneMatch304(t *testing.T) {
	s := newTestServer()
	full := do(s, http.MethodGet, "/artifacts/sample.bin", nil)
	etag := full.Header().Get("ETag")
	rec := do(s, http.MethodGet, "/artifacts/sample.bin",
		map[string]string{"If-None-Match": etag})
	if rec.Code != http.StatusNotModified {
		t.Fatalf("status %d", rec.Code)
	}
}

func TestHeadNoBody(t *testing.T) {
	rec := do(newTestServer(), http.MethodHead, "/artifacts/sample.bin",
		map[string]string{"Range": "bytes=0-9"})
	if rec.Code != http.StatusPartialContent || rec.Body.Len() != 0 {
		t.Fatalf("status %d body %d", rec.Code, rec.Body.Len())
	}
}

func TestNotFoundAndMethod(t *testing.T) {
	if rec := do(newTestServer(), http.MethodGet, "/artifacts/missing", nil); rec.Code != 404 {
		t.Errorf("missing artifact status %d", rec.Code)
	}
	rec := do(newTestServer(), http.MethodPost, "/artifacts/sample.bin", nil)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("POST status %d", rec.Code)
	}
}

func TestGzipSelectionAndDeterminism(t *testing.T) {
	b1, err := encodeGzip([]byte("deterministic payload"))
	if err != nil {
		t.Fatal(err)
	}
	b2, err := encodeGzip([]byte("deterministic payload"))
	if err != nil {
		t.Fatal(err)
	}
	if string(b1) != string(b2) {
		t.Fatal("gzip encoding is not deterministic")
	}
	s := newTestServer(WithGzip())
	rec := do(s, http.MethodGet, "/artifacts/sample.bin",
		map[string]string{"Accept-Encoding": "gzip"})
	if rec.Header().Get("Content-Encoding") != "gzip" {
		t.Fatalf("not gzip: %q", rec.Header().Get("Content-Encoding"))
	}
	rec2 := do(s, http.MethodGet, "/artifacts/sample.bin",
		map[string]string{"Accept-Encoding": "identity"})
	if rec2.Header().Get("Content-Encoding") != "" {
		t.Fatalf("identity must not encode, got %q", rec2.Header().Get("Content-Encoding"))
	}
	rec3 := do(s, http.MethodGet, "/artifacts/sample.bin",
		map[string]string{"Accept-Encoding": "gzip;q=0"})
	if rec3.Header().Get("Content-Encoding") != "" {
		t.Fatalf("gzip;q=0 must not encode, got %q", rec3.Header().Get("Content-Encoding"))
	}
}

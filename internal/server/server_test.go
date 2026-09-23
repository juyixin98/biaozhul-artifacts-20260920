package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newTestSrv() http.Handler { return New(Config{MaxBody: 4096, MaxInputBytes: 1 << 20}) }

func TestHealthz(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	newTestSrv().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d", rec.Code)
	}
}

func TestParseContentLength(t *testing.T) {
	raw := []byte("POST /a HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello")
	req := httptest.NewRequest(http.MethodPost, "/parse", bytes.NewReader(raw))
	rec := httptest.NewRecorder()
	newTestSrv().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var out struct {
		Count    int `json:"count"`
		Messages []struct {
			Method     string `json:"method"`
			Target     string `json:"target"`
			BodyBase64 string `json:"body_base64"`
			BodyLength int64  `json:"body_length"`
			HeaderEnd  int    `json:"header_end"`
		} `json:"messages"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if out.Count != 1 || out.Messages[0].BodyLength != 5 ||
		out.Messages[0].BodyBase64 != "aGVsbG8=" || out.Messages[0].HeaderEnd != len(raw)-5 {
		t.Fatalf("unexpected: %+v", out)
	}
}

func TestParsePipeline(t *testing.T) {
	raw := []byte("GET /1 HTTP/1.1\r\nHost: a\r\n\r\nGET /2 HTTP/1.1\r\nHost: b\r\n\r\n")
	req := httptest.NewRequest(http.MethodPost, "/parse?pipeline=1", bytes.NewReader(raw))
	rec := httptest.NewRecorder()
	newTestSrv().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var out struct {
		Count int `json:"count"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if out.Count != 2 {
		t.Fatalf("count=%d", out.Count)
	}
}

func TestParseRejectCLAndChunked(t *testing.T) {
	raw := []byte("POST / HTTP/1.1\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\nx\r\n0\r\n\r\n")
	req := httptest.NewRequest(http.MethodPost, "/parse", bytes.NewReader(raw))
	rec := httptest.NewRecorder()
	newTestSrv().ServeHTTP(rec, req)
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d", rec.Code)
	}
	var eb errBody
	if err := json.Unmarshal(rec.Body.Bytes(), &eb); err != nil {
		t.Fatal(err)
	}
	if eb.Code != "content_length_and_transfer_encoding" {
		t.Fatalf("code=%s", eb.Code)
	}
	// offset points at the Transfer-Encoding line
	want := bytes.Index(raw, []byte("Transfer-Encoding"))
	if eb.Offset != want {
		t.Fatalf("offset=%d want %d", eb.Offset, want)
	}
}

func TestParseFoldedHeaderOffset(t *testing.T) {
	raw := []byte("POST / HTTP/1.1\r\nContent-Length: 2\r\n\tX: y\r\n\r\nab")
	req := httptest.NewRequest(http.MethodPost, "/parse", bytes.NewReader(raw))
	rec := httptest.NewRecorder()
	newTestSrv().ServeHTTP(rec, req)
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d", rec.Code)
	}
	var eb errBody
	_ = json.Unmarshal(rec.Body.Bytes(), &eb)
	if eb.Code != "folded_header" {
		t.Fatalf("code=%s", eb.Code)
	}
	want := bytes.Index(raw, []byte("\r\n\tX")) + 2
	if eb.Offset != want {
		t.Fatalf("offset=%d want %d", eb.Offset, want)
	}
}

func TestParseTruncatedChunk(t *testing.T) {
	raw := []byte("POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhe")
	req := httptest.NewRequest(http.MethodPost, "/parse", bytes.NewReader(raw))
	rec := httptest.NewRecorder()
	newTestSrv().ServeHTTP(rec, req)
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d", rec.Code)
	}
	var eb errBody
	_ = json.Unmarshal(rec.Body.Bytes(), &eb)
	if eb.Code != "truncated" || eb.Offset != len(raw) {
		t.Fatalf("got code=%s offset=%d", eb.Code, eb.Offset)
	}
}

func TestMethodNotAllowed(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/parse", nil)
	rec := httptest.NewRecorder()
	newTestSrv().ServeHTTP(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status=%d", rec.Code)
	}
}

func TestInputTooLarge(t *testing.T) {
	big := strings.Repeat("x", 5000)
	req := httptest.NewRequest(http.MethodPost, "/parse", strings.NewReader(big))
	rec := httptest.NewRecorder()
	srv := New(Config{MaxBody: 4096, MaxInputBytes: 100})
	srv.ServeHTTP(rec, req)
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status=%d", rec.Code)
	}
}

func TestVerifySplitsOK(t *testing.T) {
	raw := []byte("POST /a HTTP/1.1\r\nContent-Length: 11\r\n\r\nhello world")
	req := httptest.NewRequest(http.MethodPost, "/verify-splits", bytes.NewReader(raw))
	rec := httptest.NewRecorder()
	newTestSrv().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var rep struct {
		OK          bool `json:"ok"`
		CutsChecked int  `json:"cuts_checked"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &rep); err != nil {
		t.Fatal(err)
	}
	if !rep.OK || rep.CutsChecked != len(raw)+1 {
		t.Fatalf("report=%+v", rep)
	}
}

func TestVerifySplitsAmbiguousStillConsistent(t *testing.T) {
	// Even ambiguous/illegal streams must be reported identically at every cut.
	raw := []byte("POST / HTTP/1.1\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\nx\r\n0\r\n\r\n")
	req := httptest.NewRequest(http.MethodPost, "/verify-splits", bytes.NewReader(raw))
	rec := httptest.NewRecorder()
	newTestSrv().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var rep struct {
		OK bool `json:"ok"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &rep); err != nil {
		t.Fatal(err)
	}
	if !rep.OK {
		t.Fatal("illegal stream should still produce segmentation-consistent errors")
	}
}

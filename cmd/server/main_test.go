package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newTestServer() *server {
	return &server{maxBody: 10 << 20, maxCarrier: 1 << 20, maxVerifySize: 4096}
}

func doRaw(t *testing.T, h http.HandlerFunc, body []byte) (int, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/parse", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	h(rec, req)
	var out map[string]any
	if rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatalf("invalid JSON (%d): %s", rec.Code, rec.Body.String())
		}
	}
	return rec.Code, out
}

func TestParseEndpointOK(t *testing.T) {
	raw := "POST /a HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello"
	code, out := doRaw(t, newTestServer().handleParse, []byte(raw))
	if code != http.StatusOK {
		t.Fatalf("status %d", code)
	}
	if out["ok"] != true {
		t.Fatalf("expected ok=true, got %v", out)
	}
	if out["request_count"].(float64) != 1 {
		t.Fatalf("request_count = %v", out["request_count"])
	}
}

func TestParseEndpointErrorOffset(t *testing.T) {
	// Folded header: offset must be start of the continuation line.
	raw := "GET / HTTP/1.1\r\nX-A: v\r\n X-B: y\r\n\r\n"
	_, out := doRaw(t, newTestServer().handleParse, []byte(raw))
	if out["ok"] != false {
		t.Fatalf("expected ok=false")
	}
	e := out["error"].(map[string]any)
	if e["code"] != "folded_header" {
		t.Fatalf("code = %v", e["code"])
	}
	want := len("GET / HTTP/1.1\r\nX-A: v\r\n")
	if int(e["offset"].(float64)) != want {
		t.Fatalf("offset = %v, want %d", e["offset"], want)
	}
}

func TestParseEndpointCLChunked(t *testing.T) {
	raw := "POST /x HTTP/1.1\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n"
	_, out := doRaw(t, newTestServer().handleParse, []byte(raw))
	e := out["error"].(map[string]any)
	if e["code"] != "content_length_and_chunked" {
		t.Fatalf("code = %v", e["code"])
	}
	teOff := len("POST /x HTTP/1.1\r\nContent-Length: 5\r\n")
	if int(e["offset"].(float64)) != teOff {
		t.Fatalf("offset = %v, want %d", e["offset"], teOff)
	}
}

func TestParseEndpointPipeline(t *testing.T) {
	raw := "GET /1 HTTP/1.1\r\nHost: a\r\n\r\nGET /2 HTTP/1.1\r\nHost: b\r\nContent-Length: 2\r\n\r\nhi"
	_, out := doRaw(t, newTestServer().handleParse, []byte(raw))
	if out["ok"] != true || out["request_count"].(float64) != 2 {
		t.Fatalf("got %v", out)
	}
}

func TestParseEndpointEmptyAndMethod(t *testing.T) {
	code, out := doRaw(t, newTestServer().handleParse, nil)
	if code != http.StatusBadRequest {
		t.Fatalf("empty body status = %d", code)
	}
	if !strings.Contains(out["error"].(string), "empty") {
		t.Fatalf("unexpected message %v", out)
	}

	req := httptest.NewRequest(http.MethodGet, "/parse", nil)
	rec := httptest.NewRecorder()
	newTestServer().handleParse(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("GET status = %d", rec.Code)
	}
}

func TestVerifyEndpoint(t *testing.T) {
	raw := "POST /a HTTP/1.1\r\nContent-Length: 11\r\n\r\nhello world"
	req := httptest.NewRequest(http.MethodPost, "/verify", strings.NewReader(raw))
	rec := httptest.NewRecorder()
	newTestServer().handleVerify(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d body=%s", rec.Code, rec.Body.String())
	}
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	if out["ok"] != true {
		t.Fatalf("verify ok = %v: %s", out["ok"], rec.Body.String())
	}
	if out["splits_checked"].(float64) != float64(len(raw)+1) {
		t.Fatalf("splits = %v", out["splits_checked"])
	}
}

func TestVerifyEndpointMalformedStillConsistent(t *testing.T) {
	// Malformed input is still framing-consistent: parse_error present,
	// but no mismatch.
	raw := "GET / HTTP/1.1\r\nX-A: v\r\n folded\r\n\r\n"
	req := httptest.NewRequest(http.MethodPost, "/verify", strings.NewReader(raw))
	rec := httptest.NewRecorder()
	newTestServer().handleVerify(rec, req)
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	if out["ok"] != true {
		t.Fatalf("expected consistency ok even for malformed input: %s", rec.Body.String())
	}
	if out["parse_error"] == nil {
		t.Fatalf("expected parse_error to be reported")
	}
}

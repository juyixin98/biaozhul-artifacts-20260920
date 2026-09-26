package server_test

import (
	"bytes"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/example/rangeserver/internal/artifact"
	"github.com/example/rangeserver/internal/server"
)

func newTestServer(t *testing.T, opts ...server.Option) (*httptest.Server, *artifact.Artifact) {
	t.Helper()
	reg := artifact.NewRegistry()
	raw := make([]byte, 256)
	for i := range raw {
		raw[i] = byte(i)
	}
	a := artifact.NewWithMetadata("bin", raw, time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC))
	reg.Put(a)
	reg.Put(artifact.NewWithMetadata("empty", []byte{}, time.Date(2026, 1, 2, 3, 4, 6, 0, time.UTC)))
	ts := httptest.NewServer(server.New(reg, opts...))
	t.Cleanup(ts.Close)
	return ts, a
}

func do(t *testing.T, ts *httptest.Server, path string, headers map[string]string) (*http.Response, []byte) {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, ts.URL+path, nil)
	if err != nil {
		t.Fatal(err)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := ts.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	return resp, body
}

func TestRangeResponses(t *testing.T) {
	ts, _ := newTestServer(t)

	tests := []struct {
		name         string
		headers      map[string]string
		wantStatus   int
		wantCR       string
		wantDecision string
		wantLen      int
	}{
		{"no range full", nil, http.StatusOK, "", "full", 256},
		{"prefix", map[string]string{"Range": "bytes=0-99"}, http.StatusPartialContent, "bytes 0-99/256", "single-partial", 100},
		{"suffix", map[string]string{"Range": "bytes=-16"}, http.StatusPartialContent, "bytes 240-255/256", "single-partial", 16},
		{"open ended clamped", map[string]string{"Range": "bytes=250-"}, http.StatusPartialContent, "bytes 250-255/256", "single-partial", 6},
		{"overlong closed clamps", map[string]string{"Range": "bytes=250-9999"}, http.StatusPartialContent, "bytes 250-255/256", "single-partial", 6},
		{"unsatisfiable", map[string]string{"Range": "bytes=1000-"}, http.StatusRequestedRangeNotSatisfiable, "bytes */256", "unsatisfiable-416", 0},
		{"malformed ignored", map[string]string{"Range": "bytes=9-1"}, http.StatusOK, "", "ignored-malformed", 256},
		{"wrong unit ignored", map[string]string{"Range": "items=0-9"}, http.StatusOK, "", "ignored-malformed", 256},
		{"if-range stale ignored", map[string]string{"Range": "bytes=0-9", "If-Range": `"nope"`}, http.StatusOK, "", "ignored-if-range", 256},
		{"range limit", map[string]string{"Range": "bytes=0-0,1-1,2-2,3-3,4-4,5-5"}, http.StatusBadRequest, "", "rejected-range-limit", 0},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			resp, body := do(t, ts, "/artifacts/bin", tc.headers)
			if resp.StatusCode != tc.wantStatus {
				t.Fatalf("status = %d, want %d; body=%s", resp.StatusCode, tc.wantStatus, body)
			}
			if got := resp.Header.Get("Content-Range"); got != tc.wantCR {
				t.Errorf("Content-Range = %q, want %q", got, tc.wantCR)
			}
			if got := resp.Header.Get("X-Range-Decision"); got != tc.wantDecision {
				t.Errorf("decision = %q, want %q", got, tc.wantDecision)
			}
			if resp.StatusCode == http.StatusPartialContent &&
				!bytes.Contains([]byte(resp.Header.Get("Content-Type")), []byte("multipart")) {
				if len(body) != tc.wantLen {
					t.Errorf("body length = %d, want %d", len(body), tc.wantLen)
				}
			}
			if tc.name == "no range full" && resp.Header.Get("Accept-Ranges") != "bytes" {
				t.Error("missing Accept-Ranges: bytes")
			}
		})
	}
}

func TestIfRangeCurrentETagHonored(t *testing.T) {
	ts, a := newTestServer(t)
	resp, body := do(t, ts, "/artifacts/bin", map[string]string{
		"Range":    "bytes=0-9",
		"If-Range": a.ETag(),
	})
	if resp.StatusCode != http.StatusPartialContent {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	if resp.Header.Get("Content-Range") != "bytes 0-9/256" {
		t.Fatalf("content-range = %q", resp.Header.Get("Content-Range"))
	}
	if len(body) != 10 {
		t.Fatalf("len = %d", len(body))
	}
}

func TestZeroLengthArtifact(t *testing.T) {
	ts, _ := newTestServer(t)

	resp, body := do(t, ts, "/artifacts/empty", nil)
	if resp.StatusCode != http.StatusOK || len(body) != 0 {
		t.Fatalf("plain GET: status=%d len=%d", resp.StatusCode, len(body))
	}

	cases := []string{"bytes=0-0", "bytes=-1", "bytes=0-"}
	for _, rh := range cases {
		resp, _ := do(t, ts, "/artifacts/empty", map[string]string{"Range": rh})
		if resp.StatusCode != http.StatusRequestedRangeNotSatisfiable {
			t.Errorf("%s: status = %d, want 416", rh, resp.StatusCode)
		}
		if got := resp.Header.Get("Content-Range"); got != "bytes */0" {
			t.Errorf("%s: Content-Range = %q", rh, got)
		}
	}
}

func TestMultipartOverlapCoalescedAndTiled(t *testing.T) {
	ts, _ := newTestServer(t)

	t.Run("overlapping ranges coalesce to one part", func(t *testing.T) {
		resp, body := do(t, ts, "/artifacts/bin", map[string]string{
			"Range": "bytes=0-119,100-255",
		})
		if resp.StatusCode != http.StatusPartialContent {
			t.Fatalf("status = %d", resp.StatusCode)
		}
		// Coalesced => single range => not multipart.
		if ct := resp.Header.Get("Content-Type"); ct != "application/octet-stream" {
			t.Fatalf("content-type = %q, want single body", ct)
		}
		if resp.Header.Get("Content-Range") != "bytes 0-255/256" {
			t.Fatalf("content-range = %q", resp.Header.Get("Content-Range"))
		}
		if len(body) != 256 {
			t.Fatalf("len = %d", len(body))
		}
	})

	t.Run("tiled ranges produce multipart", func(t *testing.T) {
		resp, body := do(t, ts, "/artifacts/bin", map[string]string{
			"Range": "bytes=0-99,100-199,200-255",
		})
		if resp.StatusCode != http.StatusPartialContent {
			t.Fatalf("status = %d", resp.StatusCode)
		}
		mt, params, err := mime.ParseMediaType(resp.Header.Get("Content-Type"))
		if err != nil || mt != "multipart/byteranges" {
			t.Fatalf("content-type = %q, err=%v", resp.Header.Get("Content-Type"), err)
		}
		var reassembled bytes.Buffer
		mr := multipart.NewReader(bytes.NewReader(body), params["boundary"])
		ranges := []string{"bytes 0-99/256", "bytes 100-199/256", "bytes 200-255/256"}
		i := 0
		for {
			part, err := mr.NextPart()
			if err == io.EOF {
				break
			}
			if err != nil {
				t.Fatal(err)
			}
			pb, _ := io.ReadAll(part)
			if got := part.Header.Get("Content-Range"); got != ranges[i] {
				t.Errorf("part %d Content-Range = %q, want %q", i, got, ranges[i])
			}
			reassembled.Write(pb)
			i++
		}
		if i != 3 {
			t.Fatalf("parts = %d, want 3", i)
		}
		if reassembled.Len() != 256 {
			t.Fatalf("reassembled len = %d", reassembled.Len())
		}
		want := make([]byte, 256)
		for i := range want {
			want[i] = byte(i)
		}
		if !bytes.Equal(reassembled.Bytes(), want) {
			t.Fatal("reassembled multipart bytes differ from original")
		}
	})
}

func TestGzipSelectedRepresentation(t *testing.T) {
	ts, _ := newTestServer(t)

	resp, body := do(t, ts, "/artifacts/bin", map[string]string{"Accept-Encoding": "gzip"})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	if resp.Header.Get("Content-Encoding") != "gzip" {
		t.Fatal("missing gzip coding")
	}
	if resp.Header.Get("Vary") == "" {
		t.Fatal("missing Vary")
	}
	decoded, err := artifact.Gunzip(body)
	if err != nil {
		t.Fatal(err)
	}
	if len(decoded) != 256 {
		t.Fatalf("decoded len = %d", len(decoded))
	}

	// Range against the coded representation cites coded length.
	resp2, _ := do(t, ts, "/artifacts/bin", map[string]string{
		"Accept-Encoding": "gzip",
		"Range":           "bytes=0-9",
	})
	if resp2.StatusCode != http.StatusPartialContent {
		t.Fatalf("status = %d", resp2.StatusCode)
	}
	cr := resp2.Header.Get("Content-Range")
	if cr == "bytes 0-9/256" {
		t.Fatalf("range total should be coded length, got %q", cr)
	}
}

func Test406WhenNoAcceptableEncoding(t *testing.T) {
	ts, _ := newTestServer(t)
	resp, _ := do(t, ts, "/artifacts/bin", map[string]string{"Accept-Encoding": "identity;q=0, gzip;q=0"})
	if resp.StatusCode != http.StatusNotAcceptable {
		t.Fatalf("status = %d, want 406", resp.StatusCode)
	}
}

func TestUnknownArtifact(t *testing.T) {
	ts, _ := newTestServer(t)
	resp, _ := do(t, ts, "/artifacts/missing", nil)
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", resp.StatusCode)
	}
}

func TestCustomMaxRanges(t *testing.T) {
	ts, _ := newTestServer(t, server.WithMaxRanges(1))
	resp, _ := do(t, ts, "/artifacts/bin", map[string]string{"Range": "bytes=0-1,2-3"})
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", resp.StatusCode)
	}
}

func TestHeadRequest(t *testing.T) {
	ts, _ := newTestServer(t)
	req, _ := http.NewRequest(http.MethodHead, ts.URL+"/artifacts/bin", nil)
	req.Header.Set("Range", "bytes=0-9")
	resp, err := ts.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusPartialContent {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	if len(body) != 0 {
		t.Fatalf("HEAD returned %d body bytes", len(body))
	}
	if resp.Header.Get("Content-Length") != "10" {
		t.Fatalf("content-length = %q", resp.Header.Get("Content-Length"))
	}
}

func TestListAndUpload(t *testing.T) {
	ts, _ := newTestServer(t)

	// List contains the seeded artifacts.
	resp, body := do(t, ts, "/artifacts", nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("list status = %d", resp.StatusCode)
	}
	if !bytes.Contains(body, []byte(`"id":"bin"`)) || !bytes.Contains(body, []byte(`"id":"empty"`)) {
		t.Fatalf("list body = %s", body)
	}

	// Upload a new artifact, then fetch it back.
	upReq, _ := http.NewRequest(http.MethodPost, ts.URL+"/artifacts/up", bytes.NewReader([]byte("uploaded")))
	upResp, err := ts.Client().Do(upReq)
	if err != nil {
		t.Fatal(err)
	}
	upBody, _ := io.ReadAll(upResp.Body)
	_ = upResp.Body.Close()
	if upResp.StatusCode != http.StatusCreated {
		t.Fatalf("upload status = %d body=%s", upResp.StatusCode, upBody)
	}
	if !bytes.Contains(upBody, []byte(`"size":8`)) {
		t.Fatalf("upload body = %s", upBody)
	}

	resp2, body2 := do(t, ts, "/artifacts/up", nil)
	if resp2.StatusCode != http.StatusOK || string(body2) != "uploaded" {
		t.Fatalf("fetch upload: status=%d body=%q", resp2.StatusCode, body2)
	}
}

func TestWithoutGzip(t *testing.T) {
	ts, _ := newTestServer(t, server.WithoutGzip())
	resp, body := do(t, ts, "/artifacts/bin", map[string]string{"Accept-Encoding": "gzip"})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	if ce := resp.Header.Get("Content-Encoding"); ce == "gzip" {
		t.Fatalf("gzip served despite WithoutGzip; body len=%d", len(body))
	}
	if len(body) != 256 {
		t.Fatalf("body length = %d, want identity 256", len(body))
	}
}

func TestHealthz(t *testing.T) {
	ts, _ := newTestServer(t)
	resp, _ := do(t, ts, "/healthz", nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
}

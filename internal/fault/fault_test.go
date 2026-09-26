package fault

import (
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

func serveBody() *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("ETag", `"v1"`)
		_, _ = w.Write([]byte("0123456789"))
	}))
}

func roundTrip(t *testing.T, cfg Config) (*http.Response, []byte, error) {
	t.Helper()
	srv := serveBody()
	t.Cleanup(srv.Close)
	tr := &Transport{Base: http.DefaultTransport, Config: cfg}
	c := &http.Client{Transport: tr}
	resp, err := c.Get(srv.URL)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	body, rerr := io.ReadAll(resp.Body)
	return resp, body, rerr
}

func TestNoFault(t *testing.T) {
	resp, body, err := roundTrip(t, Config{Kind: KindNone})
	if err != nil {
		t.Fatal(err)
	}
	if string(body) != "0123456789" || resp.Header.Get("ETag") != `"v1"` {
		t.Fatalf("body=%q etag=%q", body, resp.Header.Get("ETag"))
	}
}

func TestStripETag(t *testing.T) {
	resp, _, err := roundTrip(t, Config{Kind: KindStripETag})
	if err != nil {
		t.Fatal(err)
	}
	if resp.Header.Get("ETag") != "" {
		t.Fatalf("etag not stripped: %q", resp.Header.Get("ETag"))
	}
}

func TestTruncateShortBody(t *testing.T) {
	resp, body, err := roundTrip(t, Config{Kind: KindTruncate, AtByte: 4})
	if err != nil {
		t.Fatalf("truncation is a clean EOF, got read error: %v", err)
	}
	if string(body) != "0123" {
		t.Fatalf("body %q", body)
	}
	if resp.ContentLength != 10 {
		t.Fatalf("Content-Length should still claim 10, got %d", resp.ContentLength)
	}
}

func TestAbortUnexpectedEOF(t *testing.T) {
	_, _, err := roundTrip(t, Config{Kind: KindAbort, AtByte: 4})
	if !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatalf("want ErrUnexpectedEOF, got %v", err)
	}
}

func TestCorruptFlipsByte(t *testing.T) {
	_, body, err := roundTrip(t, Config{Kind: KindCorrupt, AtByte: 3})
	if err != nil {
		t.Fatal(err)
	}
	if body[3] != '3'^0xFF {
		t.Fatalf("byte not flipped: %q", body)
	}
	if string(body[:3]) != "012" || string(body[4:]) != "456789" {
		t.Fatalf("surrounding bytes changed: %q", body)
	}
}

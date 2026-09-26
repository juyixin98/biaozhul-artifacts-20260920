package client

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"httprange/internal/fault"
)

func TestFetchRangesRaw(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Range") == "items=0-1" {
			_, _ = w.Write([]byte("full-body"))
			return
		}
		if r.Header.Get("Range") == "bytes=99-" {
			w.Header().Set("Content-Range", "bytes */9")
			w.WriteHeader(http.StatusRequestedRangeNotSatisfiable)
			return
		}
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()
	c := New(srv.URL)

	out, err := c.FetchRangesRaw("x", "items=0-1", map[string]string{"X-T": "1"})
	if err != nil {
		t.Fatal(err)
	}
	if out.StatusCode != http.StatusOK || string(out.FullBody) != "full-body" {
		t.Fatalf("status %d body %q", out.StatusCode, out.FullBody)
	}

	out416, err := c.FetchRangesRaw("x", "bytes=99-", nil)
	if err != nil {
		t.Fatal(err)
	}
	if out416.StatusCode != http.StatusRequestedRangeNotSatisfiable {
		t.Fatalf("status %d", out416.StatusCode)
	}

	if _, err := c.FetchRangesRaw("x", "bytes=0-", nil); err == nil {
		t.Fatal("unexpected status must produce an error")
	}
}

func TestSetAndClearFault(t *testing.T) {
	c := New("http://127.0.0.1:1")
	if c.httpClient.Transport != nil {
		t.Fatal("default transport should be nil")
	}
	c.SetFault(fault.Config{Kind: fault.KindStripETag})
	if c.httpClient.Transport == nil {
		t.Fatal("fault transport not installed")
	}
	c.ClearFault()
	if c.httpClient.Transport != nil {
		t.Fatal("fault transport not cleared")
	}
}

func TestUnreachableOriginErrors(t *testing.T) {
	// A closed test server yields immediate connection refusal without
	// depending on privileged ports or firewall behavior.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	srv.Close()
	cl := New(srv.URL)
	if _, err := cl.FetchRepresentation("x", ""); err == nil {
		t.Fatal("expected connection error")
	}
	if _, err := cl.FetchRanges("x", "0-1", "", ""); err == nil {
		t.Fatal("expected connection error")
	}
}

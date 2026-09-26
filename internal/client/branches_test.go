package client

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/example/rangeserver/internal/clock"
	"github.com/example/rangeserver/internal/rangespec"
)

func TestPlaceFullDirect(t *testing.T) {
	assembly := make([]byte, 4)
	cov := newCoverage(4)
	rep := &Report{}
	if err := placeFull([]byte{10, 20, 30, 40}, rep, cov, assembly); err != nil {
		t.Fatal(err)
	}
	if !cov.complete() || assembly[3] != 40 {
		t.Fatalf("assembly = %v complete=%v", assembly, cov.complete())
	}
	if len(rep.Segments) != 1 || rep.Segments[0].Last != 3 {
		t.Fatalf("segments = %+v", rep.Segments)
	}
}

func TestCollectSingleRejectsTruncation(t *testing.T) {
	hdr := http.Header{}
	hdr.Set("Content-Range", "bytes 0-9/10")
	rep := &Report{}
	err := new(Client).collectSingle(hdr, []byte("short"), rep, newCoverage(10), make([]byte, 10))
	if err == nil {
		t.Fatal("want truncation error")
	}
}

func TestCollectSingleRejectsBadContentRange(t *testing.T) {
	hdr := http.Header{}
	hdr.Set("Content-Range", "garbage")
	err := new(Client).collectSingle(hdr, []byte("0123456789"), &Report{}, newCoverage(10), make([]byte, 10))
	if err == nil {
		t.Fatal("want Content-Range parse error")
	}
}

func TestEncodingOrIdentity(t *testing.T) {
	if encodingOrIdentity("") != "identity" {
		t.Error("empty encoding should map to identity")
	}
	if encodingOrIdentity("gzip") != "gzip" {
		t.Error("non-empty encoding should be preserved")
	}
}

// stubCatalogServer answers the catalog listing and lets artifact GET
// behavior be supplied by artifactHandler.
func stubCatalogServer(t *testing.T, artifactHandler http.HandlerFunc) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("GET /artifacts", func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprint(w, `{"artifacts":[{"id":"x","etag":"\"e\"","size":4,"sha256":"0000"}]}`)
	})
	mux.HandleFunc("GET /artifacts/x", artifactHandler)
	return httptest.NewServer(mux)
}

func newPumpedClient(t *testing.T, url string) *Client {
	t.Helper()
	fake := clock.NewFake(time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC))
	stop := make(chan struct{})
	go func() {
		ticker := time.NewTicker(time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				fake.Advance(time.Second)
			case <-stop:
				return
			}
		}
	}()
	t.Cleanup(func() { close(stop) })
	return New(Config{BaseURL: url, Clock: fake})
}

func TestDownloadGzipRejectsNonGzipEncoding(t *testing.T) {
	ts := stubCatalogServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Length", "4")
		_, _ = w.Write([]byte("raw!"))
	})
	t.Cleanup(ts.Close)

	c := newPumpedClient(t, ts.URL)
	c.acceptGzip = true
	res, err := c.Download(context.Background(), "x", nil)
	if err == nil {
		t.Fatal("want reassembly failure for missing gzip coding")
	}
	if res.Report.StatusSummary != "reassembly-failed" {
		t.Fatalf("summary = %q", res.Report.StatusSummary)
	}
}

func TestDownloadGzipRejectsLengthMismatch(t *testing.T) {
	ts := stubCatalogServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Encoding", "gzip")
		w.Header().Set("Content-Length", "99")
		_, _ = w.Write([]byte("abcd"))
	})
	t.Cleanup(ts.Close)

	c := newPumpedClient(t, ts.URL)
	c.acceptGzip = true
	_, err := c.Download(context.Background(), "x", nil)
	if err == nil {
		t.Fatal("want coverage failure for coded length mismatch")
	}
}

func TestDownloadFailsAfterExhaustedRetriesAgainst500(t *testing.T) {
	ts := stubCatalogServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	})
	t.Cleanup(ts.Close)

	c := newPumpedClient(t, ts.URL)
	res, err := c.Download(context.Background(), "x", []string{"0-3"})
	if err == nil {
		t.Fatal("want error after retries exhausted")
	}
	if res.Report.StatusSummary != "download-failed" {
		t.Fatalf("summary = %q", res.Report.StatusSummary)
	}
	if len(res.Report.Attempts) != MaxRetries {
		t.Fatalf("attempts = %d", len(res.Report.Attempts))
	}
}

func TestDownloadUnknownArtifact(t *testing.T) {
	ts := stubCatalogServer(t, func(w http.ResponseWriter, _ *http.Request) {})
	t.Cleanup(ts.Close)

	_, err := New(Config{BaseURL: ts.URL}).Download(context.Background(), "missing", []string{"0-3"})
	if err == nil {
		t.Fatal("want not-found error")
	}
}

func TestCoverageRejectsOutOfBoundsSegment(t *testing.T) {
	cov := newCoverage(2)
	if _, err := cov.add(rangespec.Resolved{First: 0, Last: 3}, []byte{1, 2, 3, 4}, make([]byte, 4)); err == nil {
		t.Fatal("want out-of-bounds error")
	}
	cov2 := newCoverage(4)
	if _, err := cov2.add(rangespec.Resolved{First: 0, Last: 3}, []byte{1, 2}, make([]byte, 4)); err == nil {
		t.Fatal("want wrong-length error")
	}
}

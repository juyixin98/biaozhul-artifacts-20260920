package client

import (
	"bytes"
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"localcache/api"
	"localcache/internal/builder"
	"localcache/internal/cas"
	"localcache/internal/server"
)

// startRealServer runs the actual cache server on a throwaway cache dir.
func startRealServer(t *testing.T) *Client {
	t.Helper()
	cacheRoot := t.TempDir()
	store, err := cas.Open(cacheRoot, 8<<20)
	if err != nil {
		t.Fatal(err)
	}
	exec, err := builder.NewExecutor(store, t.TempDir(), 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	srv, err := server.New(store, exec, cacheRoot)
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)
	return New(ts.URL)
}

func TestPutGetRoundtrip(t *testing.T) {
	c := startRealServer(t)
	ctx := context.Background()

	data := []byte("roundtrip payload")
	digest, err := c.PutBytes(ctx, data)
	if err != nil {
		t.Fatalf("PutBytes: %v", err)
	}
	if digest != cas.DigestOf(data) {
		t.Fatalf("digest = %q", digest)
	}
	ok, err := c.Has(ctx, digest)
	if err != nil || !ok {
		t.Fatalf("Has = %v, %v", ok, err)
	}
	got, err := c.Get(ctx, digest)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if !bytes.Equal(got, data) {
		t.Fatal("Get content mismatch")
	}
}

func TestGetNotFound(t *testing.T) {
	c := startRealServer(t)
	_, err := c.Get(context.Background(), cas.DigestOf([]byte("nope")))
	if err == nil || errors.Is(err, ErrTruncated) || errors.Is(err, ErrChecksum) {
		t.Fatalf("missing object must fail with a non-retry error, got %v", err)
	}
}

// Acceptance: a download cut off mid-stream is detected as truncated and
// retried; with a recovering server the client still gets intact bytes.
func TestTruncatedDownloadDetectedAndRetried(t *testing.T) {
	data := bytes.Repeat([]byte("x"), 4096)
	digest := cas.DigestOf(data)

	var calls atomic.Int32
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) == 1 {
			// First attempt: announce the full length, then die mid-body.
			conn, _, err := w.(http.Hijacker).Hijack()
			if err != nil {
				t.Errorf("hijack: %v", err)
				return
			}
			fmt.Fprintf(conn, "HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n", len(data))
			conn.Write(data[:100])
			conn.Close()
			return
		}
		// Subsequent attempts: serve the intact object.
		w.Header().Set("Content-Length", fmt.Sprint(len(data)))
		w.Write(data)
	}))
	defer ts.Close()

	c := New(ts.URL)

	// With no retries the truncation must surface as ErrTruncated.
	c.Retries = 0
	if _, err := c.Get(context.Background(), digest); !errors.Is(err, ErrTruncated) {
		t.Fatalf("first attempt: got %v, want ErrTruncated", err)
	}

	// With retries the client recovers and verifies the digest.
	c.Retries = 2
	got, err := c.Get(context.Background(), digest)
	if err != nil {
		t.Fatalf("retrying Get: %v", err)
	}
	if !bytes.Equal(got, data) {
		t.Fatal("recovered content mismatch")
	}
	// Call 1: truncated (Retries=0, no retry). Call 2: succeeds at once.
	if n := calls.Load(); n != 2 {
		t.Fatalf("server calls = %d, want 2", n)
	}
}

// Acceptance: a full-length but corrupted download is detected via digest
// mismatch, never silently accepted.
func TestCorruptDownloadDetected(t *testing.T) {
	data := []byte("expected content")
	digest := cas.DigestOf(data)

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		bad := bytes.Repeat([]byte("!"), len(data))
		w.Header().Set("Content-Length", fmt.Sprint(len(bad)))
		w.Write(bad)
	}))
	defer ts.Close()

	c := New(ts.URL)
	c.Retries = 1
	if _, err := c.Get(context.Background(), digest); !errors.Is(err, ErrChecksum) {
		t.Fatalf("got %v, want ErrChecksum", err)
	}
}

// Acceptance: many concurrent uploads/downloads through the client,
// including duplicate digests, all verify cleanly.
func TestConcurrentPutGetMany(t *testing.T) {
	c := startRealServer(t)
	ctx := context.Background()

	const n = 24
	blobs := make([][]byte, n)
	for i := range blobs {
		b := make([]byte, 32*1024+i)
		if _, err := rand.Read(b); err != nil {
			t.Fatal(err)
		}
		blobs[i] = b
	}
	// Duplicate every blob: concurrent same-digest uploads.
	blobs = append(blobs, blobs...)

	digests, err := c.PutMany(ctx, blobs, 8)
	if err != nil {
		t.Fatalf("PutMany: %v", err)
	}
	for i, d := range digests {
		if want := cas.DigestOf(blobs[i]); d != want {
			t.Fatalf("digest[%d] = %q, want %q", i, d, want)
		}
	}

	got, err := c.GetMany(ctx, digests, 8)
	if err != nil {
		t.Fatalf("GetMany: %v", err)
	}
	for i := range got {
		if !bytes.Equal(got[i], blobs[i]) {
			t.Fatalf("blob %d mismatch", i)
		}
	}

	st, err := c.Stats(ctx)
	if err != nil {
		t.Fatalf("Stats: %v", err)
	}
	if st.Objects != int64(n) {
		t.Fatalf("Stats.Objects = %d, want %d (duplicates deduped)", st.Objects, n)
	}
}

func TestClientBuildAndFsck(t *testing.T) {
	c := startRealServer(t)
	ctx := context.Background()

	res, err := c.Build(ctx, &api.BuildRequest{Argv: []string{"echo", "fixture"}})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if res.ExitCode != 0 || res.Stdout != "fixture\n" || res.CacheHit {
		t.Fatalf("unexpected build result: %+v", res)
	}
	res2, err := c.Build(ctx, &api.BuildRequest{Argv: []string{"echo", "fixture"}})
	if err != nil {
		t.Fatalf("Build 2: %v", err)
	}
	if !res2.CacheHit {
		t.Fatal("second identical build must be a cache hit")
	}

	rep, err := c.Fsck(ctx)
	if err != nil {
		t.Fatalf("Fsck: %v", err)
	}
	if !rep.OK {
		t.Fatalf("fresh cache must be clean: %+v", rep)
	}
}

// Guard against a server that never responds to a cancelled context.
func TestGetRespectsContextCancellation(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	defer ts.Close()

	c := New(ts.URL)
	c.Retries = 0
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if _, err := c.Get(ctx, cas.DigestOf([]byte("x"))); err == nil {
		t.Fatal("expected an error after context cancellation")
	}
}

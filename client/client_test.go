package client

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"modelcache/cacheapi"
	"modelcache/digest"
	"modelcache/store"
)

func realServer(t *testing.T) (*httptest.Server, *store.Store) {
	t.Helper()
	st, err := store.New(store.Options{Root: t.TempDir(), MaxObjectSize: 8 << 20})
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(cacheapi.NewServer(st, nil).Handler())
	t.Cleanup(ts.Close)
	return ts, st
}

func testClient(url string) *Client {
	return New(Options{BaseURL: url, MaxRetries: 3, UserAgent: "client-test"})
}

func TestPutAndGetVerified(t *testing.T) {
	ts, _ := realServer(t)
	cl := testClient(ts.URL)
	ctx := context.Background()

	payload := bytes.Repeat([]byte("local-model-layer-"), 2000)
	dgst := digest.NewSHA256(payload)
	pr, err := cl.PutBytes(ctx, dgst, payload)
	if err != nil {
		t.Fatal(err)
	}
	if pr.Size != int64(len(payload)) {
		t.Fatalf("size %d", pr.Size)
	}

	rc, size, err := cl.Get(ctx, dgst)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := io.ReadAll(rc)
	rc.Close()
	if size != int64(len(payload)) || !bytes.Equal(got, payload) {
		t.Fatal("verified stream mismatch")
	}
}

func TestPutBytesRejectsLocalMismatch(t *testing.T) {
	ts, _ := realServer(t)
	cl := testClient(ts.URL)
	_, err := cl.PutBytes(context.Background(), digest.NewSHA256([]byte("a")), []byte("b"))
	if err == nil || !strings.Contains(err.Error(), "local digest mismatch") {
		t.Fatalf("expected local mismatch error, got %v", err)
	}
}

// flakyServer injects download interruptions.
//
//	failFull:  first N full (no-Range) GETs declare the full Content-Length,
//	           write only truncateAt bytes, and then terminate -> unexpected EOF
//	failRange: first N Range GETs terminate after truncateAt bytes of the range
//	serveBad:  always serves wrong bytes (client-side digest verification)
//	ignoreRange: answers 200 (full body) even to Range requests
type flakyServer struct {
	payload     []byte
	failFull    int32
	failRange   int32
	truncateAt  int
	serveBad    bool
	ignoreRange bool

	mu        sync.Mutex
	fullHits  int
	rangeHits int
}

func (h *flakyServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	total := len(h.payload)
	rangeHdr := r.Header.Get("Range")

	if rangeHdr == "" {
		h.mu.Lock()
		h.fullHits++
		truncate := atomic.LoadInt32(&h.failFull) > 0
		if truncate {
			atomic.AddInt32(&h.failFull, -1)
		}
		h.mu.Unlock()

		w.Header().Set("Content-Type", "application/octet-stream")
		w.Header().Set("Accept-Ranges", "bytes")
		if h.serveBad {
			// Bad CONTENT but exactly the declared length, so failure is due
			// to digest verification, not a truncated transfer.
			bad := make([]byte, total)
			for i := range bad {
				bad[i] = 0x7e // '~'
			}
			w.Header().Set("Content-Length", strconv.Itoa(total))
			w.WriteHeader(http.StatusOK)
			w.Write(bad)
			return
		}
		if truncate {
			// Declare full length, send only a prefix, then end the response.
			w.Header().Set("Content-Length", strconv.Itoa(total))
			w.WriteHeader(http.StatusOK)
			flusher := w.(http.Flusher)
			w.Write(h.payload[:h.truncateAt])
			flusher.Flush()
			return // handler ends -> body cut short -> unexpected EOF
		}
		w.Header().Set("Content-Length", strconv.Itoa(total))
		w.WriteHeader(http.StatusOK)
		w.Write(h.payload)
		return
	}

	// Ranged request.
	h.mu.Lock()
	h.rangeHits++
	truncate := atomic.LoadInt32(&h.failRange) > 0
	if truncate {
		atomic.AddInt32(&h.failRange, -1)
	}
	h.mu.Unlock()

	if h.ignoreRange {
		w.Header().Set("Content-Type", "application/octet-stream")
		w.Header().Set("X-Content-Length", strconv.Itoa(total))
		w.Header().Set("Content-Length", strconv.Itoa(total))
		w.WriteHeader(http.StatusOK)
		w.Write(h.payload)
		return
	}

	start, err := parseStart(rangeHdr)
	if err != nil {
		http.Error(w, "bad range", http.StatusBadRequest)
		return
	}
	if start >= int64(total) {
		http.Error(w, "range out of bounds", http.StatusRequestedRangeNotSatisfiable)
		return
	}
	chunk := h.payload[start:]
	if h.serveBad {
		chunk = make([]byte, len(chunk))
		for i := range chunk {
			chunk[i] = 0x7e
		}
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("X-Content-Length", strconv.Itoa(total))
	w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, total-1, total))
	if truncate && len(chunk) > h.truncateAt {
		w.Header().Set("Content-Length", strconv.Itoa(len(chunk)))
		w.WriteHeader(http.StatusPartialContent)
		flusher := w.(http.Flusher)
		w.Write(chunk[:h.truncateAt])
		flusher.Flush()
		return
	}
	w.Header().Set("Content-Length", strconv.Itoa(len(chunk)))
	w.WriteHeader(http.StatusPartialContent)
	w.Write(chunk)
}

func parseStart(hdr string) (int64, error) {
	hdr = strings.TrimPrefix(hdr, "bytes=")
	s, _, ok := strings.Cut(hdr, "-")
	if !ok {
		return 0, fmt.Errorf("bad range %q", hdr)
	}
	return strconv.ParseInt(s, 10, 64)
}

// TestDownloadResumeAfterTruncation is acceptance item #2: the first full
// response is cut in half; the client must keep the part, resume with a Range
// request, verify the digest and publish the destination atomically.
func TestDownloadResumeAfterTruncation(t *testing.T) {
	payload := bytes.Repeat([]byte("0123456789ABCDEF"), 64*1024) // 1 MiB
	dgst := digest.NewSHA256(payload)
	h := &flakyServer{payload: payload, failFull: 1, truncateAt: len(payload) / 2}
	ts := httptest.NewServer(h)
	defer ts.Close()

	cl := testClient(ts.URL)
	dst := filepath.Join(t.TempDir(), "model.bin")
	n, err := cl.DownloadToFile(context.Background(), dgst, dst)
	if err != nil {
		t.Fatalf("resumable download failed: %v", err)
	}
	if n != int64(len(payload)) {
		t.Fatalf("got %d bytes want %d", n, len(payload))
	}
	got, err := os.ReadFile(dst)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatal("resumed content mismatch")
	}
	if h.fullHits != 1 || h.rangeHits != 1 {
		t.Fatalf("requests: full=%d range=%d, want 1/1", h.fullHits, h.rangeHits)
	}
	if _, err := os.Stat(dst + ".part"); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("part file must be removed after atomic publish")
	}
}

// TestDownloadResumeTwice interrupts both the initial GET and one Range reply.
func TestDownloadResumeTwice(t *testing.T) {
	payload := bytes.Repeat([]byte("layer-"), 100000)
	dgst := digest.NewSHA256(payload)
	h := &flakyServer{
		payload:    payload,
		failFull:   1,
		failRange:  1,
		truncateAt: len(payload) / 3,
	}
	ts := httptest.NewServer(h)
	defer ts.Close()

	cl := testClient(ts.URL)
	dst := filepath.Join(t.TempDir(), "m.bin")
	if _, err := cl.DownloadToFile(context.Background(), dgst, dst); err != nil {
		t.Fatalf("download: %v", err)
	}
	got, _ := os.ReadFile(dst)
	if !bytes.Equal(got, payload) {
		t.Fatal("content mismatch after two interruptions")
	}
	if h.rangeHits != 2 {
		t.Fatalf("range hits = %d, want 2", h.rangeHits)
	}
}

// TestDownloadServerIgnoresRange: a 200 reply to a Range request must cause a
// clean restart from offset 0 rather than corrupting the hash.
func TestDownloadServerIgnoresRange(t *testing.T) {
	payload := bytes.Repeat([]byte("xyz-"), 50000)
	dgst := digest.NewSHA256(payload)
	h := &flakyServer{payload: payload, failFull: 1, truncateAt: 1000, ignoreRange: true}
	ts := httptest.NewServer(h)
	defer ts.Close()

	cl := testClient(ts.URL)
	dst := filepath.Join(t.TempDir(), "m.bin")
	if _, err := cl.DownloadToFile(context.Background(), dgst, dst); err != nil {
		t.Fatalf("download: %v", err)
	}
	got, _ := os.ReadFile(dst)
	if !bytes.Equal(got, payload) {
		t.Fatal("content mismatch after ignored range")
	}
}

// TestDownloadDetectsBadCache: a server returning wrong bytes for a digest
// must surface ErrDigestMismatch and never publish a destination file.
func TestDownloadDetectsBadCache(t *testing.T) {
	payload := []byte("real bytes")
	dgst := digest.NewSHA256(payload)
	h := &flakyServer{payload: payload, serveBad: true}
	ts := httptest.NewServer(h)
	defer ts.Close()

	cl := testClient(ts.URL)
	dst := filepath.Join(t.TempDir(), "m.bin")
	_, err := cl.DownloadToFile(context.Background(), dgst, dst)
	if !errors.Is(err, ErrDigestMismatch) {
		t.Fatalf("want ErrDigestMismatch, got %v", err)
	}
	if _, statErr := os.Stat(dst); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatal("verified destination must not be published when the hash is bad")
	}
}

// TestStreamVerifiesHash covers the streaming Get as well.
func TestStreamVerifiesHash(t *testing.T) {
	payload := []byte("real bytes")
	dgst := digest.NewSHA256(payload)
	h := &flakyServer{payload: payload, serveBad: true}
	ts := httptest.NewServer(h)
	defer ts.Close()

	cl := testClient(ts.URL)
	rc, _, err := cl.Get(context.Background(), dgst)
	if err != nil {
		t.Fatal(err)
	}
	_, rerr := io.ReadAll(rc)
	rc.Close()
	if !errors.Is(rerr, ErrDigestMismatch) {
		t.Fatalf("stream should fail with ErrDigestMismatch, got %v", rerr)
	}
}

func TestConcurrentUploadsAndDownloads(t *testing.T) {
	ts, _ := realServer(t)
	cl := testClient(ts.URL)
	ctx := context.Background()

	// 8 distinct files.
	dir := t.TempDir()
	var paths []string
	payloads := map[string][]byte{}
	for i := 0; i < 8; i++ {
		p := bytes.Repeat([]byte{byte('A' + i)}, 64*1024+i*123)
		f := filepath.Join(dir, fmt.Sprintf("f%d.bin", i))
		if err := os.WriteFile(f, p, 0o644); err != nil {
			t.Fatal(err)
		}
		paths = append(paths, f)
		payloads[f] = p
	}

	results, err := cl.PutFilesConcurrently(ctx, paths, 4)
	if err != nil {
		t.Fatalf("concurrent uploads: %v", err)
	}
	if len(results) != 8 {
		t.Fatalf("results = %d", len(results))
	}

	// Each file downloaded concurrently by 3 clients.
	var wg sync.WaitGroup
	var failures int32
	for i := 0; i < 3; i++ {
		for _, f := range paths {
			wg.Add(1)
			go func(f string, worker int) {
				defer wg.Done()
				dgst := digest.NewSHA256(payloads[f])
				dst := filepath.Join(t.TempDir(), fmt.Sprintf("w%d-%s", worker, filepath.Base(f)))
				if _, err := cl.DownloadToFile(ctx, dgst, dst); err != nil {
					atomic.StoreInt32(&failures, 1)
					t.Errorf("download %s: %v", f, err)
					return
				}
				got, _ := os.ReadFile(dst)
				if !bytes.Equal(got, payloads[f]) {
					atomic.StoreInt32(&failures, 1)
					t.Errorf("verify %s", f)
				}
			}(f, i)
		}
	}
	wg.Wait()
	if failures != 0 {
		t.Fatal("concurrent download/verify failures")
	}

	st, err := cl.Stats(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if st.Objects != 8 || st.TempFiles != 0 {
		t.Fatalf("stats = %+v", st)
	}
}

func TestNotFoundIsHTTP404(t *testing.T) {
	ts, _ := realServer(t)
	cl := testClient(ts.URL)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, _, err := cl.Get(ctx, digest.NewSHA256([]byte("absent")))
	var he *HTTPError
	if !errors.As(err, &he) || he.StatusCode != http.StatusNotFound {
		t.Fatalf("want 404 HTTPError, got %v", err)
	}
	exists, err := cl.Exists(ctx, digest.NewSHA256([]byte("absent")))
	if err != nil || exists {
		t.Fatalf("exists=%v err=%v", exists, err)
	}
}

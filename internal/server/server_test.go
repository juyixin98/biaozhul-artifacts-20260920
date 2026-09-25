package server

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"streamback/internal/client"
	"streamback/internal/clock"
	"streamback/internal/upstream"
)

func newTestServer(t *testing.T, cfg Config) (*Server, *httptest.Server) {
	t.Helper()
	srv, err := New(cfg, upstream.New(clock.Real{}))
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	ts := httptest.NewUnstartedServer(srv.Handler())
	// Cap the server-side kernel send buffer. Loopback autotuning would
	// otherwise grow it to megabytes and absorb whole test streams,
	// hiding TCP backpressure from the application. Must be installed
	// before Start so every accepted conn gets the hook.
	ts.Config.ConnState = func(c net.Conn, state http.ConnState) {
		if state == http.StateNew {
			if tc, ok := c.(*net.TCPConn); ok {
				_ = tc.SetWriteBuffer(16 * 1024)
			}
		}
	}
	ts.Start()
	t.Cleanup(ts.Close)
	return srv, ts
}

func streamURL(ts *httptest.Server, query string) string {
	return fmt.Sprintf("%s/stream?%s", ts.URL, query)
}

// waitFor polls cond until it holds or the deadline passes.
func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func seqsAreContiguous(seqs []int, count int) bool {
	if len(seqs) != count {
		return false
	}
	for i, s := range seqs {
		if s != i+1 {
			return false
		}
	}
	return true
}

func TestFastAndSlowClientsConcurrent(t *testing.T) {
	cfg := DefaultConfig()
	cfg.BufferSlots = 2
	cfg.BufferBytes = 8 * 1024
	cfg.MaxItemBytes = 1024
	srv, ts := newTestServer(t, cfg)

	const count = 20
	const itemBytes = 512
	url := streamURL(ts, fmt.Sprintf("count=%d&itemBytes=%d", count, itemBytes))

	var wg sync.WaitGroup
	results := make([]client.Result, 2)

	wg.Add(2)
	go func() {
		defer wg.Done()
		results[0] = client.Run(context.Background(), client.Options{URL: url, Mode: client.ModeFast})
	}()
	go func() {
		defer wg.Done()
		results[1] = client.Run(context.Background(), client.Options{
			URL: url, Mode: client.ModeSlow, ReadDelay: 15 * time.Millisecond,
		})
	}()
	wg.Wait()

	fast, slow := results[0], results[1]
	for i, res := range results {
		if res.Failure != "" {
			t.Fatalf("client %d failure: %s", i, res.Failure)
		}
		if !res.Completed {
			t.Fatalf("client %d did not see end trailer: %+v", i, res.Trailer)
		}
		if !seqsAreContiguous(res.Seqs, count) {
			t.Fatalf("client %d seqs not contiguous 1..%d: %v", i, count, res.Seqs)
		}
		if res.Trailer.Sent != count {
			t.Fatalf("client %d trailer sent=%d, want %d", i, res.Trailer.Sent, count)
		}
	}
	if slow.DurationMs <= fast.DurationMs {
		t.Fatalf("slow client (%dms) not slower than fast client (%dms)", slow.DurationMs, fast.DurationMs)
	}

	// Memory bound: the gauge sums across all in-flight requests. Each
	// request contributes at most (BufferSlots+2) items (full buffer +
	// blocked producer + consumer handoff), capped by its byte budget.
	perStream := int64((cfg.BufferSlots + 2) * itemBytes)
	if perStream > int64(cfg.BufferBytes) {
		perStream = int64(cfg.BufferBytes)
	}
	maxBound := 2 * perStream // fast client + slow client
	if hi := srv.Stat.HighWaterBytes.Load(); hi > maxBound {
		t.Fatalf("high water %d exceeds bound %d", hi, maxBound)
	}
	if hi := srv.Stat.HighWaterBytes.Load(); hi == 0 {
		t.Fatal("high water is 0; backpressure buffer was never exercised")
	}

	// After both streams finish, nothing may remain buffered or active.
	waitFor(t, "streams to drain", func() bool {
		return srv.Stat.ActiveStreams.Load() == 0 && srv.Stat.BufferedBytes.Load() == 0
	})
}

func TestSlowClientTriggersBackpressure(t *testing.T) {
	cfg := DefaultConfig()
	cfg.BufferSlots = 2
	cfg.BufferBytes = 1 << 20
	cfg.MaxItemBytes = 1 << 20
	srv, ts := newTestServer(t, cfg)

	// A single item is larger than send buffer + receive window combined,
	// so a slow reader genuinely blocks the server-side writer; that fills
	// the bounded buffer, which blocks the producer.
	const itemBytes = 128 * 1024
	url := streamURL(ts, fmt.Sprintf("count=10&itemBytes=%d", itemBytes))
	res := client.Run(context.Background(), client.Options{
		URL: url, Mode: client.ModeSlow, ReadDelay: 2 * time.Millisecond,
		// Tiny receive buffer: forces real TCP backpressure on loopback so
		// the server-side writer actually blocks.
		ReceiveBufferBytes: 16 * 1024,
	})
	if !res.Completed || res.Failure != "" {
		t.Fatalf("run failed: %+v", res)
	}
	// The gauge counts items produced but not yet dequeued by the writer.
	// With the writer blocked, the buffer fills: BufferSlots in the
	// channel plus one held by the blocked producer. A consumer handoff
	// can transiently add one more. So the high water must sit in
	// [slots+1, slots+2] items — full, but strictly bounded.
	lo := int64((cfg.BufferSlots + 1) * itemBytes)
	hiBound := int64((cfg.BufferSlots + 2) * itemBytes)
	hi := srv.Stat.HighWaterBytes.Load()
	if hi < lo || hi > hiBound {
		t.Fatalf("high water = %d, want within [%d, %d] (buffer full but bounded)", hi, lo, hiBound)
	}
}

func TestOversizedItemRejected(t *testing.T) {
	cfg := DefaultConfig()
	cfg.MaxItemBytes = 1024
	cfg.BufferBytes = 4096
	_, ts := newTestServer(t, cfg)

	res := client.Run(context.Background(), client.Options{
		URL: streamURL(ts, "count=5&itemBytes=2048"),
	})
	if res.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want 413", res.StatusCode)
	}
	if res.ItemsReceived != 0 {
		t.Fatalf("received %d items from a rejected request", res.ItemsReceived)
	}
}

func TestUpstreamErrorAfterPartialResults(t *testing.T) {
	_, ts := newTestServer(t, DefaultConfig())

	res := client.Run(context.Background(), client.Options{
		URL: streamURL(ts, "count=10&itemBytes=64&failAt=5&failMsg=upstream+exploded"),
	})
	if res.Failure != "" {
		t.Fatalf("transport failure: %s", res.Failure)
	}
	if res.Completed {
		t.Fatal("stream with injected failure reported completion")
	}
	if res.ErrorCode != "UPSTREAM_FAILURE" {
		t.Fatalf("error code = %q, want UPSTREAM_FAILURE", res.ErrorCode)
	}
	if res.ErrorMessage != "upstream exploded" {
		t.Fatalf("error message = %q", res.ErrorMessage)
	}
	if !seqsAreContiguous(res.Seqs, 4) {
		t.Fatalf("expected items 1..4 before the error, got %v", res.Seqs)
	}
	if res.Trailer == nil || res.Trailer.Sent != 4 {
		t.Fatalf("trailer = %+v, want sent=4", res.Trailer)
	}
}

func TestClientDisconnect(t *testing.T) {
	cfg := DefaultConfig()
	cfg.BufferSlots = 2
	srv, ts := newTestServer(t, cfg)

	res := client.Run(context.Background(), client.Options{
		URL:             streamURL(ts, "count=1000&itemBytes=256&itemDelayMs=5"),
		Mode:            client.ModeDisconnect,
		DisconnectAfter: 2,
	})
	if !res.Disconnected {
		t.Fatal("client did not perform its injected disconnect")
	}
	if res.ItemsReceived != 2 {
		t.Fatalf("items received = %d, want 2", res.ItemsReceived)
	}

	// The server must notice the disconnect and release every resource.
	waitFor(t, "server to release the disconnected stream", func() bool {
		return srv.Stat.ActiveStreams.Load() == 0 && srv.Stat.BufferedBytes.Load() == 0
	})
}

func TestGracefulShutdownSendsTrailer(t *testing.T) {
	cfg := DefaultConfig()
	srv, ts := newTestServer(t, cfg)

	resCh := make(chan client.Result, 1)
	go func() {
		resCh <- client.Run(context.Background(), client.Options{
			URL:  streamURL(ts, "count=1000&itemBytes=64&itemDelayMs=20"),
			Mode: client.ModeSlow, ReadDelay: 10 * time.Millisecond,
		})
	}()

	// Wait until the stream is genuinely in flight, then shut down.
	waitFor(t, "stream to become active", func() bool {
		return srv.Stat.ActiveStreams.Load() == 1
	})
	if err := srv.Shutdown(5 * time.Second); err != nil {
		t.Fatalf("Shutdown: %v", err)
	}

	res := <-resCh
	if res.ErrorCode != "SERVER_SHUTDOWN" {
		t.Fatalf("error code = %q, want SERVER_SHUTDOWN (result: %+v)", res.ErrorCode, res)
	}
	if res.Trailer == nil || res.Trailer.Sent != res.ItemsReceived {
		t.Fatalf("trailer %+v inconsistent with %d received items", res.Trailer, res.ItemsReceived)
	}
	if srv.Stat.ActiveStreams.Load() != 0 {
		t.Fatalf("active streams = %d after shutdown", srv.Stat.ActiveStreams.Load())
	}
}

func TestSequenceConsistencyAcrossRuns(t *testing.T) {
	_, ts := newTestServer(t, DefaultConfig())
	url := streamURL(ts, "count=50&itemBytes=32")

	var first []int
	for run := 0; run < 3; run++ {
		res := client.Run(context.Background(), client.Options{URL: url})
		if !res.Completed {
			t.Fatalf("run %d incomplete: %s", run, res.Failure)
		}
		if !seqsAreContiguous(res.Seqs, 50) {
			t.Fatalf("run %d seqs broken: %v", run, res.Seqs)
		}
		if run == 0 {
			first = res.Seqs
			continue
		}
		for i := range first {
			if res.Seqs[i] != first[i] {
				t.Fatalf("run %d diverges at %d", run, i)
			}
		}
	}
}

func TestBadQueryParams(t *testing.T) {
	_, ts := newTestServer(t, DefaultConfig())
	res := client.Run(context.Background(), client.Options{
		URL: streamURL(ts, "count=-3"),
	})
	if res.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", res.StatusCode)
	}
}

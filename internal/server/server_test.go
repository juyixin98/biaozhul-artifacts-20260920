package server

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"streambp/internal/client"
	"streambp/internal/clock"
	"streambp/internal/protocol"
	"streambp/internal/upstream"
)

func newTestServer(t *testing.T, cfg Config) (*Server, *httptest.Server) {
	t.Helper()
	srv, err := New(cfg, clock.Real{})
	if err != nil {
		t.Fatalf("new server: %v", err)
	}
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(func() {
		srv.Close()
		ts.Close()
	})
	return srv, ts
}

func baseConfig() Config {
	return Config{
		BufferCapacity:   4,
		MaxItemBytes:     1024,
		MaxBufferedBytes: 1 << 20,
		Defaults: upstream.Config{
			ItemCount: 50,
			ItemSize:  128,
			Delay:     0,
			FailAfter: -1,
		},
	}
}

func TestFastClientCompletesWithConsistentSequence(t *testing.T) {
	_, ts := newTestServer(t, baseConfig())
	res := client.Run(context.Background(), client.Options{
		URL:     ts.URL + "/stream",
		Timeout: 10 * time.Second,
	})
	if res.Failure != "" {
		t.Fatalf("client failure: %s", res.Failure)
	}
	if !res.SequenceOK {
		t.Fatal("sequence inconsistent")
	}
	if res.EndStatus != "ok" || res.Received != 50 || res.ServerSent != 50 {
		t.Fatalf("unexpected result: %+v", res)
	}
}

func TestSlowClientTriggersBackpressure(t *testing.T) {
	cfg := baseConfig()
	srv, ts := newTestServer(t, cfg)

	done := make(chan client.Result, 1)
	go func() {
		done <- client.Run(context.Background(), client.Options{
			URL:       ts.URL + "/stream",
			ReadDelay: 5 * time.Millisecond,
			Timeout:   30 * time.Second,
		})
	}()

	// While the slow client drips, the producer must not run ahead by
	// more than the bounded buffer (plus one item in flight).
	maxInFlight := int64(0)
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		st := srv.Stats()
		inFlight := st.ProducedTotal - st.SentTotal
		if inFlight > maxInFlight {
			maxInFlight = inFlight
		}
		if st.BufferedBytes > int64(cfg.BufferCapacity+1)*int64(cfg.Defaults.ItemSize) {
			t.Fatalf("buffered bytes %d exceed per-connection bound", st.BufferedBytes)
		}
		select {
		case res := <-done:
			if res.Failure != "" || !res.SequenceOK || res.Received != 50 {
				t.Fatalf("slow client result: %+v", res)
			}
			if maxInFlight > int64(cfg.BufferCapacity)+2 {
				t.Fatalf("producer ran ahead by %d items (buffer=%d)", maxInFlight, cfg.BufferCapacity)
			}
			return
		default:
			time.Sleep(time.Millisecond)
		}
	}
	t.Fatal("slow client did not finish in time")
}

func TestConcurrentFastAndSlowClients(t *testing.T) {
	_, ts := newTestServer(t, baseConfig())
	var wg sync.WaitGroup
	errs := make(chan string, 8)
	for i := 0; i < 4; i++ {
		wg.Add(2)
		go func() { // fast
			defer wg.Done()
			res := client.Run(context.Background(), client.Options{
				URL: ts.URL + "/stream", Timeout: 15 * time.Second})
			if res.Failure != "" || !res.SequenceOK || res.Received != 50 || res.EndStatus != "ok" {
				errs <- fmt.Sprintf("fast client: %+v", res)
			}
		}()
		go func() { // slow
			defer wg.Done()
			res := client.Run(context.Background(), client.Options{
				URL: ts.URL + "/stream", ReadDelay: 2 * time.Millisecond, Timeout: 15 * time.Second})
			if res.Failure != "" || !res.SequenceOK || res.Received != 50 || res.EndStatus != "ok" {
				errs <- fmt.Sprintf("slow client: %+v", res)
			}
		}()
	}
	wg.Wait()
	close(errs)
	for e := range errs {
		t.Error(e)
	}
}

func TestClientDisconnectReclaimsBuffer(t *testing.T) {
	cfg := baseConfig()
	srv, ts := newTestServer(t, cfg)
	res := client.Run(context.Background(), client.Options{
		URL:             ts.URL + "/stream?items=100000&item_delay_ms=1",
		ReadDelay:       5 * time.Millisecond,
		DisconnectAfter: 3,
		Timeout:         10 * time.Second,
	})
	if !res.Disconnected || res.Received != 3 || !res.SequenceOK {
		t.Fatalf("disconnect result: %+v", res)
	}
	waitFor(t, 5*time.Second, func() bool {
		st := srv.Stats()
		return st.ActiveConnections == 0 && st.BufferedBytes == 0
	}, "server to reclaim connection and buffer after disconnect")
}

func TestUpstreamErrorBecomesTrailerRecord(t *testing.T) {
	_, ts := newTestServer(t, baseConfig())
	res := client.Run(context.Background(), client.Options{
		URL:     ts.URL + "/stream?fail_after=5",
		Timeout: 10 * time.Second,
	})
	if res.Failure != "" {
		t.Fatalf("client failure: %s", res.Failure)
	}
	if res.EndStatus != "error" || res.ErrorCode != protocol.CodeUpstreamError {
		t.Fatalf("expected upstream error trailer, got %+v", res)
	}
	if res.Received != 5 || res.ServerSent != 5 || !res.SequenceOK {
		t.Fatalf("partial stream mismatch: %+v", res)
	}
}

func TestOversizedItemRejectedUpfront(t *testing.T) {
	_, ts := newTestServer(t, baseConfig()) // MaxItemBytes = 1024
	res := client.Run(context.Background(), client.Options{
		URL:     ts.URL + "/stream?item_size=2048",
		Timeout: 10 * time.Second,
	})
	if res.HTTPStatus != http.StatusRequestEntityTooLarge {
		t.Fatalf("expected 413, got %+v", res)
	}
	if !strings.Contains(res.Failure, "item_too_large") {
		t.Fatalf("expected item_too_large body, got %q", res.Failure)
	}
}

// oversizeSource emits small items, then one oversized item mid-stream.
type oversizeSource struct {
	i, total, small, big int
}

func (o *oversizeSource) Next(context.Context) ([]byte, error) {
	if o.i >= o.total {
		return nil, io.EOF
	}
	o.i++
	if o.i == 3 {
		return make([]byte, o.big), nil
	}
	return make([]byte, o.small), nil
}

func TestMidStreamOversizedItemBecomesTrailerRecord(t *testing.T) {
	cfg := baseConfig() // MaxItemBytes = 1024
	srv, err := New(cfg, clock.Real{})
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	src := &oversizeSource{total: 10, small: 16, big: 2048}
	req := httptest.NewRequest(http.MethodGet, "/stream", nil)
	rec := httptest.NewRecorder()
	srv.serveStream(rec, req, src)

	lines := strings.Split(strings.TrimSpace(rec.Body.String()), "\n")
	if len(lines) != 3 { // 2 data records + 1 error trailer
		t.Fatalf("expected 3 lines, got %d: %q", len(lines), rec.Body.String())
	}
	last, err := protocol.Decode([]byte(lines[2]))
	if err != nil {
		t.Fatal(err)
	}
	if last.Type != protocol.TypeError || last.Code != protocol.CodeItemTooLarge || last.Sent != 2 {
		t.Fatalf("unexpected trailer: %+v", last)
	}
}

func TestGracefulShutdownInterruptsStream(t *testing.T) {
	cfg := baseConfig()
	srv, err := New(cfg, clock.Real{})
	if err != nil {
		t.Fatal(err)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	hs := &http.Server{Handler: srv.Handler()}
	go func() { _ = hs.Serve(ln) }()

	clientDone := make(chan client.Result, 1)
	go func() {
		clientDone <- client.Run(context.Background(), client.Options{
			URL: fmt.Sprintf("http://%s/stream?items=100000&item_delay_ms=5", ln.Addr()),
		})
	}()
	// Let the stream get going, then shut the server down.
	time.Sleep(100 * time.Millisecond)
	srv.Close()
	shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := hs.Shutdown(shutCtx); err != nil {
		t.Fatalf("http shutdown: %v", err)
	}
	select {
	case res := <-clientDone:
		// The client must not hang; it either saw a SHUTTING_DOWN trailer
		// or the connection dropped. Any already-received data must be
		// sequence-consistent.
		if !res.SequenceOK {
			t.Fatalf("sequence broken during shutdown: %+v", res)
		}
		if res.EndStatus == "ok" {
			t.Fatalf("stream should not complete 100000 items during shutdown: %+v", res)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("client hung after server shutdown")
	}
}

func TestGlobalMemoryBoundAcrossConnections(t *testing.T) {
	cfg := baseConfig()
	// Smaller than two full per-connection buffers (4*128B each), so
	// concurrent producers genuinely contend for the global budget.
	cfg.MaxBufferedBytes = 1024
	srv, ts := newTestServer(t, cfg)

	var wg sync.WaitGroup
	results := make(chan client.Result, 4)
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			results <- client.Run(context.Background(), client.Options{
				URL:       ts.URL + "/stream?items=40",
				ReadDelay: 3 * time.Millisecond,
				Timeout:   30 * time.Second,
			})
		}()
	}
	// Poll the gauge while clients stream: the global cap must hold.
	violation := make(chan string, 1)
	stop := make(chan struct{})
	go func() {
		for {
			select {
			case <-stop:
				return
			default:
			}
			if got := srv.Stats().BufferedBytes; got > cfg.MaxBufferedBytes {
				select {
				case violation <- fmt.Sprintf("buffered %d > max %d", got, cfg.MaxBufferedBytes):
				default:
				}
				return
			}
			time.Sleep(time.Millisecond)
		}
	}()
	wg.Wait()
	close(stop)
	select {
	case v := <-violation:
		t.Fatal(v)
	default:
	}
	for i := 0; i < 4; i++ {
		res := <-results
		if res.Failure != "" || !res.SequenceOK || res.Received != 40 {
			t.Fatalf("client under memory pressure: %+v", res)
		}
	}
}

func waitFor(t *testing.T, timeout time.Duration, ok func() bool, what string) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if ok() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

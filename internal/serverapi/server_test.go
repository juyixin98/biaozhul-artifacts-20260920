package serverapi

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"testing"
	"time"

	"cancelprop/internal/clock"
	"cancelprop/internal/fakesvc"
	"cancelprop/internal/leakcheck"
)

type harnessT struct {
	t    *testing.T
	app  *Server
	a, b *fakesvc.Server
	url  string
	cli  *http.Client
}

func newHarness(t *testing.T) *harnessT {
	t.Helper()
	a := fakesvc.New("a")
	b := fakesvc.New("b")
	if err := a.Start(); err != nil {
		t.Fatalf("start a: %v", err)
	}
	if err := b.Start(); err != nil {
		t.Fatalf("start b: %v", err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = a.Shutdown(ctx)
		_ = b.Shutdown(ctx)
	})

	app := New(Config{Clock: clock.NewRealClock(), Services: []*fakesvc.Server{a, b}})
	srv := &http.Server{Handler: app.Handler()}
	ln, err := newListener()
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
		app.CloseIdleConnections()
	})
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.MaxIdleConnsPerHost = 2
	return &harnessT{
		t:   t,
		app: app,
		a:   a,
		b:   b,
		url: "http://" + ln.Addr().String(),
		cli: &http.Client{Transport: tr},
	}
}

func (h *harnessT) post(body ProcessRequest) (*http.Response, []byte) {
	h.t.Helper()
	buf, _ := json.Marshal(body)
	resp, err := h.cli.Post(h.url+"/process", "application/json", bytes.NewReader(buf))
	if err != nil {
		h.t.Fatalf("POST /process: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	out, err := readAll(resp.Body)
	if err != nil {
		h.t.Fatal(err)
	}
	return resp, out
}

func (h *harnessT) postCtx(ctx context.Context, body ProcessRequest) error {
	h.t.Helper()
	buf, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, h.url+"/process", bytes.NewReader(buf))
	if err != nil {
		h.t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := h.cli.Do(req)
	if err == nil {
		_ = resp.Body.Close()
	}
	return err
}

func (h *harnessT) stats() ServiceStats {
	h.t.Helper()
	resp, err := h.cli.Get(h.url + "/stats")
	if err != nil {
		h.t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	var st ServiceStats
	if err := json.NewDecoder(resp.Body).Decode(&st); err != nil {
		h.t.Fatal(err)
	}
	return st
}

func waitSettles(h *harnessT, name string, value func() int64, want int64) {
	h.t.Helper()
	if err := leakcheck.DefaultSettler().Wait(name, value, want); err != nil {
		h.t.Fatal(err)
	}
}

// fastWait returns as soon as value first reaches want. Appropriate for
// monotonic rendezvous points (e.g. "the handler is now blocked"), not for
// proving a drained steady state.
func fastWait(h *harnessT, name string, value func() int64, want int64) {
	h.t.Helper()
	s := leakcheck.Settler{Timeout: 2 * time.Second, Every: time.Millisecond, Stable: 1}
	if err := s.Wait(name, value, want); err != nil {
		h.t.Fatal(err)
	}
}

func TestE2E_AllSucceed(t *testing.T) {
	h := newHarness(t)
	_, body := h.post(ProcessRequest{Leaves: []LeafSpec{
		{Name: "a1", Service: "a", Fatal: true},
		{Name: "b1", Service: "b", Fatal: true},
	}})
	var rep map[string]any
	if err := json.Unmarshal(body, &rep); err != nil {
		t.Fatalf("decode: %v body=%s", err, body)
	}
	if rep["ok"] != true {
		t.Fatalf("ok != true: %s", body)
	}
}

func TestE2E_FirstFatalCancelsOthersAndCleansUp(t *testing.T) {
	h := newHarness(t)
	_, body := h.post(ProcessRequest{Leaves: []LeafSpec{
		{Name: "gated-a", Service: "a", Fatal: true, Gate: "fatal-1", Resource: "lock-a"},
		{Name: "gated-b", Service: "b", Fatal: true, Gate: "fatal-1", Resource: "lock-b"},
		// Delay the fatal failure until the gated siblings are confirmed
		// blocked server-side; otherwise an instant client-side fault could
		// cancel them before their requests ever left the process.
		{Name: "fatal-c", Service: "a", Fatal: true, FailRequest: true, DelayMS: 300},
	}})
	var rep map[string]any
	if err := json.Unmarshal(body, &rep); err != nil {
		t.Fatalf("decode: %v body=%s", err, body)
	}
	if rep["ok"] != false || rep["fatal_trigger"] != "fatal-c" {
		t.Fatalf("unexpected report: %s", body)
	}
	// The two gated siblings must have been stopped server-side.
	waitSettles(h, "service-a canceled", func() int64 { return h.a.Snapshot().Canceled }, 1)
	waitSettles(h, "service-b canceled", func() int64 { return h.b.Snapshot().Canceled }, 1)
	waitSettles(h, "service-a in-flight", func() int64 { return h.a.Snapshot().InFlight }, 0)
	waitSettles(h, "service-b in-flight", func() int64 { return h.b.Snapshot().InFlight }, 0)
	// Cleanup awaited: resources released and no request in flight.
	st := h.stats()
	if st.HeldResources != 0 || st.InFlightReq != 0 {
		t.Fatalf("resources/requests not drained: %+v", st)
	}
	if st.Released < 2 {
		t.Fatalf("released resources = %d, want >= 2", st.Released)
	}
}

func TestE2E_NonFatalFailureTolerated(t *testing.T) {
	h := newHarness(t)
	resp, body := h.post(ProcessRequest{Leaves: []LeafSpec{
		{Name: "best-effort", Service: "a", Fatal: false, Fail: true},
		{Name: "fine", Service: "b", Fatal: true},
	}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status=%d body=%s", resp.StatusCode, body)
	}
	var rep map[string]any
	_ = json.Unmarshal(body, &rep)
	if rep["ok"] != true {
		t.Fatalf("non-fatal failure must be tolerated: %s", body)
	}
}

func TestE2E_ClientDisconnectCancelsRemainingWork(t *testing.T) {
	h := newHarness(t)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- h.postCtx(ctx, ProcessRequest{Leaves: []LeafSpec{
			{Name: "a", Service: "a", Fatal: true, Gate: "dc", Resource: "r-a"},
			{Name: "b", Service: "b", Fatal: true, Gate: "dc", Resource: "r-b"},
		}})
	}()

	fastWait(h, "in-flight app request", func() int64 { return h.stats().InFlightReq }, 1)
	fastWait(h, "service-a blocked", func() int64 { return h.a.Snapshot().InFlight }, 1)
	fastWait(h, "service-b blocked", func() int64 { return h.b.Snapshot().InFlight }, 1)

	cancel() // client goes away while subtasks are still computing
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("expected client error after disconnect")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("client call did not return after cancel")
	}

	// Remaining server-side computation must be terminated and drained.
	waitSettles(h, "app in-flight drain", func() int64 { return h.stats().InFlightReq }, 0)
	waitSettles(h, "service-a canceled", func() int64 { return h.a.Snapshot().Canceled }, 1)
	waitSettles(h, "service-b canceled", func() int64 { return h.b.Snapshot().Canceled }, 1)
	st := h.stats()
	if st.HeldResources != 0 {
		t.Fatalf("held resources after disconnect = %d, want 0 (cleanup awaited)", st.HeldResources)
	}
	// Release gates and confirm handlers already returned (no late in-flight).
	h.a.Release("dc")
	h.b.Release("dc")
	time.Sleep(50 * time.Millisecond)
	if h.a.Snapshot().InFlight != 0 || h.b.Snapshot().InFlight != 0 {
		t.Fatal("downstream in-flight nonzero after disconnect drain")
	}
}

// TestE2E_ResponseVsCancelRace exercises the acceptance race between the
// downstream response becoming ready and the client giving up. Both
// orderings are driven deterministically (response-wins by releasing and
// awaiting success; cancel-wins by canceling first and then releasing), and
// every iteration must converge with no leaked work or resources.
func TestE2E_ResponseVsCancelRace(t *testing.T) {
	h := newHarness(t)
	const iterations = 40
	var responseWins, cancelWins int64

	for i := 0; i < iterations; i++ {
		gate := fmt.Sprintf("race-%d", i)
		resA := fmt.Sprintf("race-a-%d", i)
		resB := fmt.Sprintf("race-b-%d", i)
		ctx, cancel := context.WithCancel(context.Background())
		callDone := make(chan error, 1)
		go func() {
			callDone <- h.postCtx(ctx, ProcessRequest{Leaves: []LeafSpec{
				{Name: "a", Service: "a", Fatal: true, Gate: gate, Resource: resA},
				{Name: "b", Service: "b", Fatal: false, Gate: gate, Resource: resB},
			}})
		}()

		// Do not release until BOTH handlers registered the gate; releasing
		// before gate creation would be a dropped no-op.
		fastWait(h, fmt.Sprintf("a blocked %d", i), func() int64 { return h.a.Snapshot().InFlight }, 1)
		fastWait(h, fmt.Sprintf("b blocked %d", i), func() int64 { return h.b.Snapshot().InFlight }, 1)

		if i%2 == 0 {
			// Response wins: let both downstreams answer and wait for the
			// successful HTTP response without canceling.
			h.a.Release(gate)
			h.b.Release(gate)
			select {
			case err := <-callDone:
				if err != nil {
					t.Fatalf("iter %d: expected success (response-wins), got %v", i, err)
				}
				responseWins++
			case <-time.After(2 * time.Second):
				t.Fatalf("iter %d: response never arrived", i)
			}
		} else {
			// Cancel wins: client aborts while subtasks are blocked, then
			// the gates are opened (no longer needed).
			cancel()
			select {
			case err := <-callDone:
				if err == nil {
					t.Fatalf("iter %d: expected error (cancel-wins), got nil", i)
				}
				cancelWins++
			case <-time.After(2 * time.Second):
				t.Fatalf("iter %d: call did not return after cancel", i)
			}
			h.a.Release(gate)
			h.b.Release(gate)
		}
		// Boundary: wait for both server handlers to exit before the next
		// iteration, so a stale InFlight==1 cannot satisfy the next round's
		// rendezvous before that round's gates are registered. At this point
		// this round's handler is the only one in flight (counter >= 1), so
		// a single observation of 0 proves it finished.
		fastWait(h, fmt.Sprintf("a drain %d", i), func() int64 { return h.a.Snapshot().InFlight }, 0)
		fastWait(h, fmt.Sprintf("b drain %d", i), func() int64 { return h.b.Snapshot().InFlight }, 0)
		cancel()
	}
	t.Logf("race results: response-wins=%d cancel-wins=%d", responseWins, cancelWins)
	if responseWins != int64(iterations/2) || cancelWins != int64(iterations/2) {
		t.Fatalf("expected %d of each outcome", iterations/2)
	}

	// Whatever the ordering, everything must converge to a clean state.
	h.app.CloseIdleConnections()
	h.cli.CloseIdleConnections()
	waitSettles(h, "app requests drain", func() int64 { return h.stats().InFlightReq }, 0)
	waitSettles(h, "held resources drain", func() int64 { return int64(h.stats().HeldResources) }, 0)
	waitSettles(h, "service-a in-flight drain", func() int64 { return h.a.Snapshot().InFlight }, 0)
	waitSettles(h, "service-b in-flight drain", func() int64 { return h.b.Snapshot().InFlight }, 0)
	waitSettles(h, "service-a conns drain", func() int64 { return h.a.Snapshot().ActiveConns }, 0)
	waitSettles(h, "service-b conns drain", func() int64 { return h.b.Snapshot().ActiveConns }, 0)
}

// TestE2E_NoSustainedLeakage drives many requests and checks goroutines,
// downstream connections and heap do not grow without bound.
func TestE2E_NoSustainedLeakage(t *testing.T) {
	h := newHarness(t)

	// Warm up to establish steady-state pools.
	for i := 0; i < 10; i++ {
		h.post(ProcessRequest{Leaves: []LeafSpec{{Name: "a", Service: "a"}, {Name: "b", Service: "b"}}})
	}
	h.app.CloseIdleConnections()
	h.cli.CloseIdleConnections()
	waitSettles(h, "warmup conns a", func() int64 { return h.a.Snapshot().ActiveConns }, 0)
	waitSettles(h, "warmup conns b", func() int64 { return h.b.Snapshot().ActiveConns }, 0)

	measure := func(rounds int) (goroutines int, heapB uint64, st ServiceStats) {
		for i := 0; i < rounds; i++ {
			h.post(ProcessRequest{Leaves: []LeafSpec{
				{Name: "a", Service: "a", Fatal: true, Resource: fmt.Sprintf("m-a-%d-%p", i, h)},
				{Name: "b", Service: "b", Fatal: false, Resource: fmt.Sprintf("m-b-%d-%p", i, h)},
			}})
		}
		h.app.CloseIdleConnections()
		h.cli.CloseIdleConnections()
		waitSettles(h, "conns a", func() int64 { return h.a.Snapshot().ActiveConns }, 0)
		waitSettles(h, "conns b", func() int64 { return h.b.Snapshot().ActiveConns }, 0)
		waitSettles(h, "held", func() int64 { return int64(h.stats().HeldResources) }, 0)
		heapB = leakcheck.HeapSample()
		goroutines = leakcheck.GoroutineCount()
		st = h.stats()
		return
	}

	g1, heap1, st1 := measure(50)
	g2, heap2, st2 := measure(50)
	g3, heap3, st3 := measure(50)
	t.Logf("goroutines per round: %d %d %d", g1, g2, g3)
	t.Logf("heap bytes per round: %d %d %d", heap1, heap2, heap3)
	t.Logf("acquired/released: %d/%d %d/%d %d/%d", st1.Acquired, st1.Released, st2.Acquired, st2.Released, st3.Acquired, st3.Released)

	// Every acquired resource must have been released: totals equal.
	if st3.Acquired != st3.Released {
		t.Fatalf("resource totals diverged: acquired=%d released=%d", st3.Acquired, st3.Released)
	}
	// Goroutines must not grow across rounds (allow tiny scheduler slack).
	if g3 > g1+3 {
		t.Fatalf("goroutines grew: %d -> %d", g1, g3)
	}
	// Heap must not climb monotonically by more than 25% (GC noise tolerated).
	if heap3 > heap1*125/100 && heap2 > heap1 && heap3 > heap2 {
		t.Fatalf("heap grew monotonically: %d %d %d", heap1, heap2, heap3)
	}
}

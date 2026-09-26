package server

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"etagrace/internal/clock"
	"etagrace/internal/notifier"
	"etagrace/internal/store"
)

type testEnv struct {
	clk    *clock.Fake
	audit  *notifier.AuditLog
	faulty *notifier.FaultyNotifier
	srv    *httptest.Server
	hc     *http.Client
}

func newTestEnv(t *testing.T, attempts int) *testEnv {
	t.Helper()
	clk := clock.NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	audit := notifier.NewAuditLog()
	faulty := notifier.NewFaulty(audit, clk)
	notif := RetryingNotifier{Inner: faulty, Clk: clk, Attempts: attempts, BaseBackoff: time.Millisecond}
	st := store.New(clk)
	srv := New(st, clk, notif, audit)
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)
	return &testEnv{clk: clk, audit: audit, faulty: faulty, srv: ts, hc: ts.Client()}
}

func (e *testEnv) do(t *testing.T, method, key, ifMatch string, body []byte) *http.Response {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		rdr = bytes.NewReader(body)
	}
	req, err := http.NewRequest(method, e.srv.URL+"/resources/"+key, rdr)
	if err != nil {
		t.Fatal(err)
	}
	if ifMatch != "" {
		req.Header.Set("If-Match", ifMatch)
	}
	resp, err := e.hc.Do(req)
	if err != nil {
		t.Fatalf("%s: %v", method, err)
	}
	t.Cleanup(func() { resp.Body.Close() })
	return resp
}

func mustCreate(t *testing.T, e *testEnv, key string) string {
	t.Helper()
	resp := e.do(t, http.MethodPost, key, "", []byte("init"))
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("create status %d", resp.StatusCode)
	}
	tag := resp.Header.Get("ETag")
	if tag == "" || tag[0] == 'W' {
		t.Fatalf("create must return a strong ETag, got %q", tag)
	}
	return tag
}

func TestHTTPConditionalLifecycle(t *testing.T) {
	e := newTestEnv(t, 1)
	tag := mustCreate(t, e, "doc")

	// Happy update.
	resp := e.do(t, http.MethodPut, "doc", tag, []byte("next"))
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("matching PUT: %d", resp.StatusCode)
	}
	newTag := resp.Header.Get("ETag")
	if newTag == tag {
		t.Fatal("ETag must change after update")
	}
	if e.audit.Len() != 2 {
		t.Fatalf("want 2 audit events (create+update), got %d", e.audit.Len())
	}

	// Stale tag fails and reports the current ETag.
	stale := e.do(t, http.MethodPut, "doc", tag, []byte("late"))
	if stale.StatusCode != http.StatusPreconditionFailed {
		t.Fatalf("stale PUT: %d", stale.StatusCode)
	}
	if h := stale.Header.Get("ETag"); h != newTag {
		t.Fatalf("412 should carry current ETag %s, got %s", newTag, h)
	}

	// Missing precondition -> 428.
	if r := e.do(t, http.MethodPut, "doc", "", []byte("x")); r.StatusCode != http.StatusPreconditionRequired {
		t.Fatalf("no precondition PUT: %d", r.StatusCode)
	}
	if r := e.do(t, http.MethodDelete, "doc", "", nil); r.StatusCode != http.StatusPreconditionRequired {
		t.Fatalf("no precondition DELETE: %d", r.StatusCode)
	}

	// Weak tag -> 412 weak_etag_rejected.
	weak := e.do(t, http.MethodPut, "doc", "W/"+newTag, []byte("x"))
	if weak.StatusCode != http.StatusPreconditionFailed {
		t.Fatalf("weak PUT: %d", weak.StatusCode)
	}
	data, _ := io.ReadAll(weak.Body)
	if !bytes.Contains(data, []byte("weak_etag_rejected")) {
		t.Fatalf("weak response body: %s", data)
	}

	// Wildcard update then wildcard delete.
	if r := e.do(t, http.MethodPut, "doc", "*", []byte("star")); r.StatusCode != http.StatusOK {
		t.Fatalf("wildcard PUT: %d", r.StatusCode)
	}
	if r := e.do(t, http.MethodDelete, "doc", "*", nil); r.StatusCode != http.StatusNoContent {
		t.Fatalf("wildcard DELETE: %d", r.StatusCode)
	}
	if r := e.do(t, http.MethodGet, "doc", "", nil); r.StatusCode != http.StatusNotFound {
		t.Fatalf("GET after delete: %d", r.StatusCode)
	}
}

func TestHTTPRecreateAfterDelete(t *testing.T) {
	e := newTestEnv(t, 1)
	old := mustCreate(t, e, "k")
	if r := e.do(t, http.MethodDelete, "k", old, nil); r.StatusCode != http.StatusNoContent {
		t.Fatalf("delete: %d", r.StatusCode)
	}
	// Old tag can never match again.
	if r := e.do(t, http.MethodPut, "k", old, []byte("x")); r.StatusCode != http.StatusPreconditionFailed {
		t.Fatalf("stale after delete: %d", r.StatusCode)
	}
	resp := e.do(t, http.MethodPost, "k", "", []byte("rebirth"))
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("recreate: %d", resp.StatusCode)
	}
	fresh := resp.Header.Get("ETag")
	if fresh == old {
		t.Fatal("recreated ETag must differ from the deleted one")
	}
}

func TestHTTPConcurrentSameTagOneWinner(t *testing.T) {
	e := newTestEnv(t, 1)
	tag := mustCreate(t, e, "doc")

	const n = 32
	var (
		wg    sync.WaitGroup
		mu    sync.Mutex
		codes []int
	)
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			r := e.do(t, http.MethodPut, "doc", tag, []byte("b"))
			mu.Lock()
			codes = append(codes, r.StatusCode)
			mu.Unlock()
		}(i)
	}
	close(start)
	wg.Wait()

	var ok, fail int
	for _, c := range codes {
		switch c {
		case http.StatusOK:
			ok++
		case http.StatusPreconditionFailed:
			fail++
		}
	}
	if ok != 1 || fail != n-1 {
		t.Fatalf("want 1x200 and %dx412, got %d/%d", n-1, ok, fail)
	}
	if e.audit.Len() != 2 {
		t.Fatalf("exactly one update audit event expected, total=%d", e.audit.Len())
	}
}

func TestHTTPDownstreamFailureNoMutation(t *testing.T) {
	e := newTestEnv(t, 2) // server retries twice
	tag := mustCreate(t, e, "doc")
	before := e.audit.Len()

	e.faulty.ArmFailures(-1) // all notifications fail
	resp := e.do(t, http.MethodPut, "doc", tag, []byte("nope"))
	e.faulty.Disarm()

	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("want 503, got %d", resp.StatusCode)
	}
	if e.audit.Len() != before {
		t.Fatal("no audit event may be written on total failure")
	}
	got := e.do(t, http.MethodGet, "doc", "", nil)
	if h := got.Header.Get("ETag"); h != tag {
		t.Fatalf("ETag changed after failed update: %s != %s", h, tag)
	}
	body, _ := io.ReadAll(got.Body)
	if string(body) != "init" {
		t.Fatalf("content changed: %q", body)
	}
}

func TestRetryingNotifierRetriesThenSucceeds(t *testing.T) {
	clk := clock.NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	audit := notifier.NewAuditLog()
	faulty := notifier.NewFaulty(audit, clk)
	faulty.ArmFailures(2)

	rn := RetryingNotifier{Inner: faulty, Clk: clk, Attempts: 4, BaseBackoff: time.Millisecond}
	err := rn.Notify(context.Background(), notifier.Event{Type: "update", Key: "k", Version: 2})
	if err != nil {
		t.Fatalf("retry should succeed: %v", err)
	}
	if audit.Len() != 1 {
		t.Fatalf("exactly one event after recovery, got %d", audit.Len())
	}
	if faulty.Remaining() != 0 {
		t.Fatalf("faults not consumed as expected: %d", faulty.Remaining())
	}
}

func TestRetryingNotifierContextCancel(t *testing.T) {
	clk := clock.NewFake(time.Now())
	audit := notifier.NewAuditLog()
	faulty := notifier.NewFaulty(audit, clk)
	faulty.ArmFailures(-1)

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	rn := RetryingNotifier{Inner: faulty, Clk: clk, Attempts: 5, BaseBackoff: time.Millisecond}
	err := rn.Notify(ctx, notifier.Event{})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("want context.Canceled, got %v", err)
	}
}

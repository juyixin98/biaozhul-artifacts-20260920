package httpserver

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"conditionupdate/internal/clock"
	"conditionupdate/internal/fakesvc"
	"conditionupdate/internal/store"
)

func newTestServer(t *testing.T) (*httptest.Server, *fakesvc.AuditService) {
	t.Helper()
	c := clock.NewFake(time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC))
	audit := fakesvc.NewAudit(c)
	srv := New(store.New(c), audit, c)
	ts := httptest.NewServer(srv)
	t.Cleanup(ts.Close)
	return ts, audit
}

func doReq(t *testing.T, method, url string, headers map[string]string, body string) *http.Response {
	t.Helper()
	req, err := http.NewRequest(method, url, strings.NewReader(body))
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("%s %s: %v", method, url, err)
	}
	return resp
}

func createResource(t *testing.T, base, id, body string) (etag string) {
	t.Helper()
	resp := doReq(t, http.MethodPut, base+"/resources/"+id,
		map[string]string{"If-None-Match": "*"}, body)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("create %s: status %d body %s", id, resp.StatusCode, b)
	}
	return resp.Header.Get("ETag")
}

// End-to-end acceptance: two concurrent PUTs with the same ETag over
// real HTTP — exactly one wins.
func TestHTTPConcurrentSameETag(t *testing.T) {
	ts, _ := newTestServer(t)
	et := createResource(t, ts.URL, "doc", "v1")

	var wg sync.WaitGroup
	codes := make([]int, 2)
	start := make(chan struct{})
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			resp := doReq(t, http.MethodPut, ts.URL+"/resources/doc",
				map[string]string{"If-Match": et}, fmt.Sprintf("racer-%d", i))
			defer resp.Body.Close()
			codes[i] = resp.StatusCode
		}(i)
	}
	close(start)
	wg.Wait()

	ok, failed := 0, 0
	for _, c := range codes {
		switch c {
		case http.StatusOK:
			ok++
		case http.StatusPreconditionFailed:
			failed++
		default:
			t.Fatalf("unexpected status %d", c)
		}
	}
	if ok != 1 || failed != 1 {
		t.Fatalf("expected 1x200 + 1x412, got %v", codes)
	}
}

func TestHTTPMissingPreconditionIs428(t *testing.T) {
	ts, _ := newTestServer(t)
	resp := doReq(t, http.MethodPut, ts.URL+"/resources/doc", nil, "no precondition")
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusPreconditionRequired {
		t.Fatalf("expected 428, got %d", resp.StatusCode)
	}
	var b map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&b)
	if b["error"].(map[string]any)["code"] != "precondition_required" {
		t.Fatalf("unexpected body: %v", b)
	}
}

func TestHTTPWeakETagRejected(t *testing.T) {
	ts, _ := newTestServer(t)
	et := createResource(t, ts.URL, "doc", "v1")
	weak := "W/" + et

	resp := doReq(t, http.MethodPut, ts.URL+"/resources/doc",
		map[string]string{"If-Match": weak}, "weak write")
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusPreconditionFailed {
		t.Fatalf("expected 412 for weak ETag, got %d", resp.StatusCode)
	}
}

func TestHTTPDeleteRecreate(t *testing.T) {
	ts, _ := newTestServer(t)
	et1 := createResource(t, ts.URL, "doc", "first")

	resp := doReq(t, http.MethodDelete, ts.URL+"/resources/doc",
		map[string]string{"If-Match": et1}, "")
	resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("delete: expected 204, got %d", resp.StatusCode)
	}

	et2 := createResource(t, ts.URL, "doc", "second")
	if et2 == et1 {
		t.Fatal("recreated resource must have a new ETag")
	}

	resp = doReq(t, http.MethodPut, ts.URL+"/resources/doc",
		map[string]string{"If-Match": et1}, "stale")
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusPreconditionFailed {
		t.Fatalf("stale ETag after recreate: expected 412, got %d", resp.StatusCode)
	}
}

func TestHTTPWildcardIfMatch(t *testing.T) {
	ts, _ := newTestServer(t)
	createResource(t, ts.URL, "doc", "v1")

	resp := doReq(t, http.MethodPut, ts.URL+"/resources/doc",
		map[string]string{"If-Match": "*"}, "v2")
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("If-Match: * on existing: expected 200, got %d", resp.StatusCode)
	}

	resp = doReq(t, http.MethodPut, ts.URL+"/resources/ghost",
		map[string]string{"If-Match": "*"}, "nope")
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusPreconditionFailed {
		t.Fatalf("If-Match: * on missing: expected 412, got %d", resp.StatusCode)
	}
}

// Fault injection: when the fake downstream fails, the update is
// aborted and neither the store nor the audit log shows any change.
func TestHTTPFaultInjectionNoSideEffects(t *testing.T) {
	ts, audit := newTestServer(t)
	et := createResource(t, ts.URL, "doc", "v1")
	auditCount := len(audit.Entries())

	resp := doReq(t, http.MethodPost, ts.URL+"/admin/faults", nil, `{"failNext":1}`)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("set faults: %d", resp.StatusCode)
	}

	resp = doReq(t, http.MethodPut, ts.URL+"/resources/doc",
		map[string]string{"If-Match": et}, "must not land")
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("expected 503 on injected failure, got %d", resp.StatusCode)
	}

	// Store unchanged.
	resp = doReq(t, http.MethodGet, ts.URL+"/resources/doc", nil, "")
	defer resp.Body.Close()
	if got := resp.Header.Get("ETag"); got != et {
		t.Fatalf("ETag changed after failed update: %s -> %s", et, got)
	}
	// Audit log unchanged.
	if len(audit.Entries()) != auditCount {
		t.Fatalf("audit log grew after failed update: %d -> %d", auditCount, len(audit.Entries()))
	}
}

func TestHTTPClockControl(t *testing.T) {
	ts, _ := newTestServer(t)
	resp := doReq(t, http.MethodPost, ts.URL+"/admin/clock", nil,
		`{"set":"2030-01-02T03:04:05Z","advanceMs":60000}`)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("clock control: %d", resp.StatusCode)
	}
	var b map[string]string
	_ = json.NewDecoder(resp.Body).Decode(&b)
	if b["now"] != "2030-01-02T03:05:05Z" {
		t.Fatalf("unexpected clock now: %v", b)
	}
}

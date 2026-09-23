// Package integration_test runs the whole stack over real UDP loopback and
// the net/http server: crafted fake-upstream responses -> proxy validation ->
// HTTP JSON. These are the acceptance checks for compression-pointer attacks,
// truncation, ID handling and TTL boundaries, and they assert that every
// failure leaves the cache empty.
package integration_test

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"dnscomp-proxy/internal/cache"
	"dnscomp-proxy/internal/dnsmsg"
	"dnscomp-proxy/internal/fakeserver"
	"dnscomp-proxy/internal/httpapi"
	"dnscomp-proxy/internal/proxy"
)

type harness struct {
	fake *fakeserver.Server
	http *httptest.Server
}

func setup(t *testing.T, timeout time.Duration) *harness {
	t.Helper()
	zone := []fakeserver.RecordConfig{
		{Name: "example.com", Type: dnsmsg.TypeA, IP: net.IPv4(192, 0, 2, 10).To4(), TTL: 2},
		{Name: "www.example.com", Type: dnsmsg.TypeA, IP: net.IPv4(192, 0, 2, 20).To4(), TTL: 2},
		{Name: "v6.example", Type: dnsmsg.TypeAAAA, IP: net.ParseIP("2001:db8::2"), TTL: 2},
		{Name: "multi.example", Type: dnsmsg.TypeA, IP: net.IPv4(192, 0, 2, 30).To4(), TTL: 2},
	}
	srv, err := fakeserver.New("127.0.0.1:0", zone)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = srv.Close() })
	go func() { _ = srv.Serve() }()

	c := cache.New()
	p, err := proxy.New(proxy.Config{
		UpstreamAddr: srv.LocalAddr().String(),
		Timeout:      timeout,
	}, c)
	if err != nil {
		t.Fatal(err)
	}
	hs := httptest.NewServer(httpapi.NewServer(p, nil))
	t.Cleanup(hs.Close)
	return &harness{fake: srv, http: hs}
}

func (h *harness) get(path string) (int, map[string]any) {
	resp, err := http.Get(h.http.URL + path)
	if err != nil {
		panic(err)
	}
	defer resp.Body.Close()
	var body map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&body)
	return resp.StatusCode, body
}

func cacheCount(t *testing.T, h *harness) int {
	t.Helper()
	status, body := h.get("/cache")
	if status != 200 {
		t.Fatalf("cache status=%d", status)
	}
	entries, _ := body["entries"].([]any)
	return len(entries)
}

func expectResolveError(t *testing.T, h *harness, name string) {
	t.Helper()
	status, body := h.get("/resolve?name=" + name + "&type=A")
	if status != http.StatusBadGateway {
		t.Fatalf("%s: status=%d body=%v, want 502", name, status, body)
	}
	if _, ok := body["error"]; !ok {
		t.Fatalf("%s: error body missing: %v", name, body)
	}
	if n := cacheCount(t, h); n != 0 {
		t.Fatalf("%s: error response was cached (%d entries)", name, n)
	}
}

// --- Acceptance cases -------------------------------------------------------

func TestNormalResolveAndCaching(t *testing.T) {
	h := setup(t, time.Second)

	status, body := h.get("/resolve?name=example.com&type=A")
	if status != 200 {
		t.Fatalf("status=%d body=%v", status, body)
	}
	if body["source"] != "upstream" {
		t.Fatalf("source=%v", body["source"])
	}
	ans, _ := body["answers"].([]any)
	if len(ans) != 1 || ans[0].(map[string]any)["ip"] != "192.0.2.10" {
		t.Fatalf("answers=%v", ans)
	}
	if h.fake.Hits("example.com") != 1 {
		t.Fatalf("hits=%d", h.fake.Hits("example.com"))
	}

	// Second call must be served from cache: no extra upstream hit.
	status, body = h.get("/resolve?name=EXAMPLE.com.&type=A")
	if status != 200 {
		t.Fatalf("second status=%d", status)
	}
	if body["source"] != "cache" {
		t.Fatalf("second source=%v, want cache", body["source"])
	}
	if h.fake.Hits("example.com") != 1 {
		t.Fatalf("cache did not suppress upstream: hits=%d", h.fake.Hits("example.com"))
	}
	if cacheCount(t, h) != 1 {
		t.Fatal("cache should hold exactly one entry")
	}
}

func TestAAAAResolve(t *testing.T) {
	h := setup(t, time.Second)
	status, body := h.get("/resolve?name=v6.example&type=AAAA")
	if status != 200 || body["type"] != "AAAA" {
		t.Fatalf("status=%d body=%v", status, body)
	}
	ans, _ := body["answers"].([]any)
	if ans[0].(map[string]any)["ip"] != "2001:db8::2" {
		t.Fatalf("aaaa answers=%v", ans)
	}
}

func TestCacheTTLExpiry(t *testing.T) {
	h := setup(t, time.Second) // TTL in the zone is 2s
	if status, _ := h.get("/resolve?name=www.example.com&type=A"); status != 200 {
		t.Fatal("first resolve failed")
	}
	if status, body := h.get("/resolve?name=www.example.com&type=A"); status != 200 || body["source"] != "cache" {
		t.Fatalf("expected cache hit, got %v", body)
	}
	if h.fake.Hits("www.example.com") != 1 {
		t.Fatalf("hits before expiry=%d", h.fake.Hits("www.example.com"))
	}

	time.Sleep(2300 * time.Millisecond)

	status, body := h.get("/resolve?name=www.example.com&type=A")
	if status != 200 {
		t.Fatalf("post-expiry resolve: %d %v", status, body)
	}
	if body["source"] != "upstream" {
		t.Fatalf("expected upstream refetch after TTL, source=%v", body["source"])
	}
	if h.fake.Hits("www.example.com") != 2 {
		t.Fatalf("hits after expiry=%d, want 2", h.fake.Hits("www.example.com"))
	}
}

func TestTTLZeroNotCached(t *testing.T) {
	h := setup(t, time.Second)
	status, body := h.get("/resolve?name=tql0.example&type=A")
	if status != 200 {
		t.Fatalf("ttl0 resolve status=%d body=%v", status, body)
	}
	ans, _ := body["answers"].([]any)
	if ans[0].(map[string]any)["ttl"].(float64) != 0 {
		t.Fatalf("ttl=%v", ans[0])
	}
	if !strings.Contains(body["source"].(string), "not cached") {
		t.Fatalf("source=%v should disclose non-caching", body["source"])
	}
	// Every call goes upstream; cache stays empty.
	h.get("/resolve?name=tql0.example&type=A")
	if h.fake.Hits("tql0.example") != 2 {
		t.Fatalf("ttl0 hits=%d, want 2", h.fake.Hits("tql0.example"))
	}
	if n := cacheCount(t, h); n != 0 {
		t.Fatalf("ttl0 entries cached=%d", n)
	}
}

func TestMaxTTLStored(t *testing.T) {
	h := setup(t, time.Second)
	status, body := h.get("/resolve?name=ttlmax.example&type=A")
	if status != 200 {
		t.Fatalf("ttlmax status=%d body=%v", status, body)
	}
	// Two answers TTL 4294967295 and 5 -> cache lifetime is the minimum, 5s.
	status, body = h.get("/cache")
	entries, _ := body["entries"].([]any)
	if len(entries) != 1 {
		t.Fatalf("entries=%v", entries)
	}
	rem := entries[0].(map[string]any)["ttl_remaining_seconds"].(float64)
	if rem < 3 || rem > 5 {
		t.Fatalf("min-TTL remaining=%v, want ~5s", rem)
	}
}

// --- Hostile responses: all must error and never enter the cache -----------

func TestPointerRingRejected(t *testing.T) {
	h := setup(t, time.Second)
	expectResolveError(t, h, "loop.example")
}

func TestOutOfBoundsPointerRejected(t *testing.T) {
	h := setup(t, time.Second)
	expectResolveError(t, h, "fwd.example")
}

func TestLongPointerChainRejected(t *testing.T) {
	h := setup(t, time.Second)
	expectResolveError(t, h, "chain.example")
}

func TestTruncatedRRRejected(t *testing.T) {
	h := setup(t, time.Second)
	expectResolveError(t, h, "trunc.example")
}

func TestShortDatagramRejected(t *testing.T) {
	h := setup(t, time.Second)
	expectResolveError(t, h, "short.example")
}

func TestIDMismatchRejected(t *testing.T) {
	h := setup(t, time.Second)
	expectResolveError(t, h, "badid.example")
}

func TestNXDOMAINRejected(t *testing.T) {
	h := setup(t, time.Second)
	expectResolveError(t, h, "nxdomain.example")
}

func TestEmptyAnswerRejected(t *testing.T) {
	h := setup(t, time.Second)
	expectResolveError(t, h, "noans.example")
}

func TestTCFlagRejected(t *testing.T) {
	h := setup(t, time.Second)
	expectResolveError(t, h, "tc.example")
}

func TestMultiQuestionRejected(t *testing.T) {
	h := setup(t, time.Second)
	expectResolveError(t, h, "multiq.example")
}

func TestUnknownNameIsNXDOMAINNotCached(t *testing.T) {
	h := setup(t, time.Second)
	expectResolveError(t, h, "does-not-exist.example")
}

func TestUpstreamUnreachable(t *testing.T) {
	// Point the proxy at a closed port: must 502, must not cache.
	c := cache.New()
	p, err := proxy.New(proxy.Config{UpstreamAddr: "127.0.0.1:1", Timeout: 200 * time.Millisecond}, c)
	if err != nil {
		t.Fatal(err)
	}
	hs := httptest.NewServer(httpapi.NewServer(p, nil))
	defer hs.Close()

	resp, err := http.Get(hs.URL + "/resolve?name=example.com&type=A")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status=%d, want 502", resp.StatusCode)
	}
	if c.Len() != 0 {
		t.Fatal("upstream failure was cached")
	}
}

// --- HTTP-level input validation and cache management -----------------------

func TestInvalidQType(t *testing.T) {
	h := setup(t, time.Second)
	resp, err := http.Get(h.http.URL + "/resolve?name=example.com&type=MX")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d, want 400", resp.StatusCode)
	}
}

func TestInvalidName(t *testing.T) {
	h := setup(t, time.Second)
	resp, err := http.Get(h.http.URL + "/resolve?name=bad..name&type=A")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d, want 400", resp.StatusCode)
	}
}

func TestCacheFlush(t *testing.T) {
	h := setup(t, time.Second)
	if s, _ := h.get("/resolve?name=example.com&type=A"); s != 200 {
		t.Fatal("seed failed")
	}
	if cacheCount(t, h) != 1 {
		t.Fatal("seed missing")
	}
	req, _ := http.NewRequest(http.MethodDelete, h.http.URL+"/cache", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 200 || cacheCount(t, h) != 0 {
		t.Fatalf("flush failed: status=%d count=%d", resp.StatusCode, cacheCount(t, h))
	}
}

func TestHealthz(t *testing.T) {
	h := setup(t, time.Second)
	resp, err := http.Get(h.http.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("healthz=%d", resp.StatusCode)
	}
}

func TestErrorThenSuccessSameName(t *testing.T) {
	h := setup(t, time.Second)
	// First an error response...
	expectResolveError(t, h, "nxdomain.example")
	// ...then the cache must not poison a later good lookup for a different
	// name, and a good name right after must succeed.
	status, body := h.get("/resolve?name=example.com&type=A")
	if status != 200 {
		t.Fatalf("good lookup after error failed: %v", body)
	}
	if cacheCount(t, h) != 1 {
		t.Fatal("only the successful lookup should be cached")
	}
}

// human-readable failure listing for the README acceptance table.
func TestAcceptanceMatrixSummary(t *testing.T) {
	h := setup(t, time.Second)
	cases := []string{
		"loop.example", "fwd.example", "chain.example",
		"trunc.example", "short.example", "badid.example",
		"nxdomain.example", "noans.example", "tc.example", "multiq.example",
	}
	for _, name := range cases {
		status, body := h.get(fmt.Sprintf("/resolve?name=%s&type=A", name))
		t.Logf("%-22s HTTP %d  %v", name, status, body["error"])
		if status != http.StatusBadGateway {
			t.Errorf("%s unexpectedly succeeded", name)
		}
	}
	if cacheCount(t, h) != 0 {
		t.Fatal("no hostile case may be cached")
	}
}

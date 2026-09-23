package httpapi_test

import (
	"encoding/json"
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

func newTestServer(t *testing.T) (*httptest.Server, *fakeserver.Server) {
	t.Helper()
	zone := []fakeserver.RecordConfig{
		{Name: "example.com", Type: dnsmsg.TypeA, IP: net.IPv4(192, 0, 2, 10).To4(), TTL: 30},
	}
	fs, err := fakeserver.New("127.0.0.1:0", zone)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = fs.Close() })
	go func() { _ = fs.Serve() }()

	p, err := proxy.New(proxy.Config{UpstreamAddr: fs.LocalAddr().String(), Timeout: time.Second}, cache.New())
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(httpapi.NewServer(p, nil))
	t.Cleanup(srv.Close)
	return srv, fs
}

func TestResolveJSONShape(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Get(srv.URL + "/resolve?name=example.com&type=A")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if ct := resp.Header.Get("Content-Type"); !strings.Contains(ct, "application/json") {
		t.Fatalf("content-type=%q", ct)
	}
	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body["name"] != "example.com" || body["type"] != "A" || body["source"] != "upstream" {
		t.Fatalf("body=%v", body)
	}
	answers, _ := body["answers"].([]any)
	first := answers[0].(map[string]any)
	if first["ip"] != "192.0.2.10" || first["ttl"].(float64) != 30 {
		t.Fatalf("answers=%v", answers)
	}
}

func TestResolveDefaultType(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Get(srv.URL + "/resolve?name=example.com")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("default type status=%d", resp.StatusCode)
	}
}

func TestResolveBadTypeAndMethod(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Get(srv.URL + "/resolve?name=example.com&type=SRV")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d", resp.StatusCode)
	}

	// POST is not routed (only GET registered) -> 405.
	post, err := http.Post(srv.URL+"/resolve?name=example.com", "application/json", nil)
	if err != nil {
		t.Fatal(err)
	}
	post.Body.Close()
	if post.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("POST status=%d, want 405", post.StatusCode)
	}
}

func TestCacheEndpoints(t *testing.T) {
	srv, _ := newTestServer(t)

	if _, err := http.Get(srv.URL + "/resolve?name=example.com&type=A"); err != nil {
		t.Fatal(err)
	}
	resp, err := http.Get(srv.URL + "/cache")
	if err != nil {
		t.Fatal(err)
	}
	var listed map[string]any
	json.NewDecoder(resp.Body).Decode(&listed)
	resp.Body.Close()
	if entries, _ := listed["entries"].([]any); len(entries) != 1 {
		t.Fatalf("entries=%v", listed)
	}

	req, _ := http.NewRequest(http.MethodDelete, srv.URL+"/cache", nil)
	dresp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	var flushed map[string]any
	json.NewDecoder(dresp.Body).Decode(&flushed)
	dresp.Body.Close()
	if flushed["flushed"].(float64) != 1 {
		t.Fatalf("flush body=%v", flushed)
	}
}

func TestHealthz(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}

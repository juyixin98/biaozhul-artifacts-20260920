package httpapi_test

import (
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"dnsproxy/internal/dnsmsg"
	"dnsproxy/internal/fakedns"
	"dnsproxy/internal/httpapi"
	"dnsproxy/internal/proxy"
)

func startStack(t *testing.T) (*httptest.Server, *fakedns.Server) {
	t.Helper()
	srv, err := fakedns.Start("127.0.0.1:0", func(req fakedns.Request) []byte {
		q := req.Message.Questions[0]
		if dnsmsg.CanonicalName(q.Name) != "example.com." {
			resp, _ := dnsmsg.BuildResponse(req.Message.ID, q, 3, false, nil)
			return resp
		}
		var ip net.IP
		if q.Type == dnsmsg.TypeAAAA {
			ip = net.ParseIP("2001:db8::1")
		} else {
			ip = net.ParseIP("192.0.2.1")
		}
		resp, _ := dnsmsg.BuildResponse(req.Message.ID, q, 0, false, []dnsmsg.AnswerSpec{
			{Name: q.Name, TTL: 30, IP: ip},
		})
		return resp
	})
	if err != nil {
		t.Fatalf("start fakedns: %v", err)
	}
	t.Cleanup(func() { _ = srv.Close() })

	p, err := proxy.New(proxy.Config{UpstreamAddr: srv.Addr(), Timeout: time.Second})
	if err != nil {
		t.Fatalf("new proxy: %v", err)
	}
	ts := httptest.NewServer(httpapi.Handler(p))
	t.Cleanup(ts.Close)
	return ts, srv
}

func TestResolveEndpoint(t *testing.T) {
	ts, _ := startStack(t)

	resp, err := http.Get(ts.URL + "/resolve?name=example.com&type=A")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var body struct {
		Name    string `json:"name"`
		Type    string `json:"type"`
		RCode   int    `json:"rcode"`
		TTL     uint32 `json:"ttl"`
		Cached  bool   `json:"cached"`
		Answers []struct {
			Value string `json:"value"`
		} `json:"answers"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body.Name != "example.com." || body.Type != "A" || body.RCode != 0 ||
		body.TTL != 30 || len(body.Answers) != 1 || body.Answers[0].Value != "192.0.2.1" {
		t.Fatalf("unexpected body: %+v", body)
	}

	// 再查一次，应 cached=true。
	resp2, err := http.Get(ts.URL + "/resolve?name=example.com&type=A")
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	_ = json.NewDecoder(resp2.Body).Decode(&body)
	if !body.Cached {
		t.Fatal("second request should be served from cache")
	}
}

func TestResolveAAAADefaultTypeAndBadInput(t *testing.T) {
	ts, _ := startStack(t)

	resp, err := http.Get(ts.URL + "/resolve?name=example.com&type=AAAA")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}

	// 缺少 name -> 400
	resp, err = http.Get(ts.URL + "/resolve")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("missing name: status = %d", resp.StatusCode)
	}

	// 不支持的类型 -> 400
	resp, err = http.Get(ts.URL + "/resolve?name=example.com&type=MX")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("MX: status = %d", resp.StatusCode)
	}
}

func TestNXDOMAINStatus(t *testing.T) {
	ts, _ := startStack(t)
	// NXDOMAIN 是合法 DNS 应答，HTTP 层面仍然 200，rcode=3。
	resp, err := http.Get(ts.URL + "/resolve?name=nope.example.net&type=A")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var body struct {
		RCode int `json:"rcode"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&body)
	if body.RCode != 3 {
		t.Fatalf("rcode = %d, want 3", body.RCode)
	}
}

func TestUpstreamTimeoutStatus(t *testing.T) {
	srv, err := fakedns.Start("127.0.0.1:0", func(req fakedns.Request) []byte { return nil })
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()

	p, err := proxy.New(proxy.Config{UpstreamAddr: srv.Addr(), Timeout: 50 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(httpapi.Handler(p))
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/resolve?name=silent.example&type=A")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusGatewayTimeout {
		t.Fatalf("status = %d, want 504", resp.StatusCode)
	}
}

func TestCacheEndpointsAndHealth(t *testing.T) {
	ts, _ := startStack(t)

	if _, err := http.Get(ts.URL + "/resolve?name=example.com&type=A"); err != nil {
		t.Fatal(err)
	}

	resp, err := http.Get(ts.URL + "/cache")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var stats struct{ Entries int }
	if err := json.NewDecoder(resp.Body).Decode(&stats); err != nil {
		t.Fatal(err)
	}
	if stats.Entries != 1 {
		t.Fatalf("cache entries = %d, want 1", stats.Entries)
	}

	flushReq, _ := http.NewRequest(http.MethodPost, ts.URL+"/cache/flush", nil)
	resp2, err := http.DefaultClient.Do(flushReq)
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusOK {
		t.Fatalf("flush status = %d", resp2.StatusCode)
	}

	resp3, err := http.Get(ts.URL + "/cache")
	if err != nil {
		t.Fatal(err)
	}
	defer resp3.Body.Close()
	_ = json.NewDecoder(resp3.Body).Decode(&stats)
	if stats.Entries != 0 {
		t.Fatalf("entries after flush = %d", stats.Entries)
	}

	resp4, err := http.Get(ts.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp4.Body.Close()
	if resp4.StatusCode != http.StatusOK {
		t.Fatalf("healthz status = %d", resp4.StatusCode)
	}
}

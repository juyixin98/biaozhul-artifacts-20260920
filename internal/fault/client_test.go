package fault

import (
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"contractcheck/internal/clock"
)

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func okTransport(body string) http.RoundTripper {
	return roundTripperFunc(func(r *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": {"application/json"}},
			Body:       io.NopCloser(strings.NewReader(body)),
			Request:    r,
		}, nil
	})
}

func TestInjectsLatencyUsingControllableClock(t *testing.T) {
	clk := clock.NewFake(time.Unix(0, 0))
	tr := NewTransport(okTransport(`{}`), clk, Config{Latency: 250 * time.Millisecond})
	req, _ := http.NewRequest("GET", "http://fake/", nil)
	if _, err := tr.RoundTrip(req); err != nil {
		t.Fatalf("RoundTrip: %v", err)
	}
	if got := clk.Now(); !got.Equal(time.Unix(0, 0).Add(250 * time.Millisecond)) {
		t.Fatalf("clock = %v, latency was not applied", got)
	}
}

func TestInjectsErrorsAtFullRate(t *testing.T) {
	tr := NewTransport(okTransport(`{}`), nil, Config{ErrorRate: 1.0})
	req, _ := http.NewRequest("GET", "http://fake/", nil)
	resp, err := tr.RoundTrip(req)
	if err != nil {
		t.Fatalf("RoundTrip: %v", err)
	}
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", resp.StatusCode)
	}
}

func TestZeroRateNeverInjects(t *testing.T) {
	tr := NewTransport(okTransport(`{"ok":true}`), nil, Config{})
	req, _ := http.NewRequest("GET", "http://fake/", nil)
	for i := 0; i < 20; i++ {
		resp, err := tr.RoundTrip(req)
		if err != nil {
			t.Fatalf("RoundTrip: %v", err)
		}
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200", resp.StatusCode)
		}
	}
}

func TestHeaderOverrides(t *testing.T) {
	clk := clock.NewFake(time.Unix(0, 0))
	tr := NewTransport(okTransport(`{}`), clk, Config{})
	tr.AllowHeaderOverride = true
	req, _ := http.NewRequest("GET", "http://fake/", nil)
	req.Header.Set("X-Fault-Latency-Ms", "100")
	req.Header.Set("X-Fault-Error-Rate", "1")
	resp, err := tr.RoundTrip(req)
	if err != nil {
		t.Fatalf("RoundTrip: %v", err)
	}
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want injected 503", resp.StatusCode)
	}
	if got := clk.Now(); !got.Equal(time.Unix(0, 0).Add(100 * time.Millisecond)) {
		t.Fatalf("clock = %v, header latency was not applied", got)
	}
}

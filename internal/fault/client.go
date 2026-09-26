// Package fault provides an HTTP RoundTripper that injects latency and
// errors into outbound calls, so the service's behaviour under dependency
// failure can be exercised without any real external system.
package fault

import (
	"io"
	"math/rand"
	"net/http"
	"strconv"
	"strings"
	"time"

	"contractcheck/internal/clock"
)

// Config controls fault injection.
type Config struct {
	// Latency is added to every request before it is sent.
	Latency time.Duration
	// ErrorRate in [0,1] is the probability of a request failing with an
	// injected error response.
	ErrorRate float64
	// StatusCode is used for injected error responses (default 503).
	StatusCode int
}

// Transport wraps a base RoundTripper with fault injection. Per-request
// overrides are accepted via headers when AllowHeaderOverride is true:
//
//	X-Fault-Latency-Ms: 250
//	X-Fault-Error-Rate: 0.5
type Transport struct {
	Base                http.RoundTripper
	Clock               clock.Clock
	Rand                *rand.Rand
	Config              Config
	AllowHeaderOverride bool
}

// NewTransport builds a Transport with sane defaults.
func NewTransport(base http.RoundTripper, clk clock.Clock, cfg Config) *Transport {
	if base == nil {
		base = http.DefaultTransport
	}
	if clk == nil {
		clk = clock.Real{}
	}
	return &Transport{
		Base:   base,
		Clock:  clk,
		Rand:   rand.New(rand.NewSource(1)),
		Config: cfg,
	}
}

func (t *Transport) RoundTrip(req *http.Request) (*http.Response, error) {
	cfg := t.Config
	if t.AllowHeaderOverride {
		cfg = applyHeaderOverrides(cfg, req.Header)
	}
	if cfg.Latency > 0 {
		t.Clock.Sleep(cfg.Latency)
	}
	if cfg.ErrorRate > 0 && t.Rand.Float64() < cfg.ErrorRate {
		code := cfg.StatusCode
		if code == 0 {
			code = http.StatusServiceUnavailable
		}
		return &http.Response{
			StatusCode: code,
			Status:     strconv.Itoa(code) + " Injected Fault",
			Header:     http.Header{"Content-Type": {"application/json"}},
			Body:       io.NopCloser(strings.NewReader(`{"error":"injected fault"}`)),
			Request:    req,
		}, nil
	}
	return t.Base.RoundTrip(req)
}

func applyHeaderOverrides(cfg Config, h http.Header) Config {
	if v := h.Get("X-Fault-Latency-Ms"); v != "" {
		if ms, err := strconv.Atoi(v); err == nil && ms >= 0 {
			cfg.Latency = time.Duration(ms) * time.Millisecond
		}
	}
	if v := h.Get("X-Fault-Error-Rate"); v != "" {
		if rate, err := strconv.ParseFloat(v, 64); err == nil && rate >= 0 && rate <= 1 {
			cfg.ErrorRate = rate
		}
	}
	return cfg
}

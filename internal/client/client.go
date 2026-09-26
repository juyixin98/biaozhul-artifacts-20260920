// Package client is the fault-injecting HTTP client used by the cancellation
// tree. It wraps a standard net/http client (so context cancellation really
// tears down the in-flight TCP request and the upstream observes the
// disconnect) and adds per-task fault injection, bounded retries and
// connection accounting.
package client

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync/atomic"
	"time"

	"canceltree/internal/clock"
)

// Fault kinds understood by the client.
const (
	// FaultNone performs a plain HTTP call.
	FaultNone = ""
	// FaultLatency sleeps for Delay (honoring context cancellation), then
	// performs the call normally.
	FaultLatency = "latency"
	// FaultStall blocks until the context is canceled and never sends a
	// request. Models a client stuck in local work.
	FaultStall = "stall"
	// FaultError returns a synthetic error without touching the network.
	FaultError = "error"
	// FaultReset performs a real call against an endpoint that closes the
	// TCP connection without responding, producing a transport-level error.
	FaultReset = "reset"
)

// Fault declares a client-side failure to inject.
type Fault struct {
	Kind  string        `json:"kind"`
	Delay time.Duration `json:"-"`
}

// Call describes one outbound attempt group.
type Call struct {
	TaskID   string
	Method   string
	URL      string // absolute URL
	Fault    Fault
	Attempts int // <=0 means the client default
}

// Result is the structured outcome of a Call.
type Result struct {
	TaskID     string        `json:"task_id"`
	StatusCode int           `json:"status_code"`
	Attempts   int           `json:"attempts"`
	Latency    time.Duration `json:"-"`
	LatencyMS  int64         `json:"latency_ms"`
	FaultUsed  string        `json:"fault_used,omitempty"`
	Error      string        `json:"error,omitempty"`
}

// OK reports whether the upstream answered with a 2xx status.
func (r Result) OK() bool { return r.Error == "" && r.StatusCode >= 200 && r.StatusCode < 300 }

// ConnStats is a snapshot of tracked TCP connections and in-flight calls.
type ConnStats struct {
	TotalEstablished int64 `json:"total_established"`
	// Open is currently-open TCP connections, including idle keep-alive
	// connections parked in the pool. Bounded by the transport's pool size.
	Open int64 `json:"open"`
	// InFlight is the number of HTTP calls currently executing.
	InFlight int64 `json:"in_flight"`
}

// Config configures Logic.
type Config struct {
	// Attempts is the default number of attempts on transport errors (1 = no retry).
	Attempts int
	// Backoff between attempts.
	Backoff time.Duration
	// DialTimeout bounds TCP connection setup.
	DialTimeout time.Duration
	// DisableKeepAlives turns off connection reuse (every call opens and
	// closes its own connection). Intended for tests that need to observe
	// connection counts settle to zero without a pool teardown step.
	DisableKeepAlives bool
}

// Logic is the fault-injecting client.
type Logic struct {
	clk      clock.Clock
	httpc    *http.Client
	attempts int
	backoff  time.Duration
	tracker  *connTracker
	inFlight atomic.Int64
}

// New builds a Logic sharing one transport (and therefore one connection
// pool) across all calls.
func New(clk clock.Clock, cfg Config) *Logic {
	tracker := &connTracker{}
	dialer := &net.Dialer{Timeout: orDuration(cfg.DialTimeout, 2*time.Second)}
	transport := &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		DialContext:           tracker.wrapDial(dialer.DialContext),
		ForceAttemptHTTP2:     false,
		DisableKeepAlives:     cfg.DisableKeepAlives,
		MaxIdleConns:          100,
		MaxIdleConnsPerHost:   100,
		IdleConnTimeout:       30 * time.Second,
		TLSHandshakeTimeout:   2 * time.Second,
		ExpectContinueTimeout: time.Second,
	}
	httpc := &http.Client{Transport: transport}
	return &Logic{
		clk:      clk,
		httpc:    httpc,
		attempts: orInt(cfg.Attempts, 1),
		backoff:  cfg.Backoff,
		tracker:  tracker,
	}
}

// CloseIdleConnections releases pooled keep-alive connections.
func (l *Logic) CloseIdleConnections() { l.httpc.CloseIdleConnections() }

// ConnStats returns connection accounting since construction.
func (l *Logic) ConnStats() ConnStats {
	return ConnStats{
		TotalEstablished: l.tracker.total.Load(),
		Open:             l.tracker.active.Load(),
		InFlight:         l.inFlight.Load(),
	}
}

// Do executes the call. Retries are bounded and fully abort as soon as ctx is
// canceled; no retry ever outlives the request tree.
func (l *Logic) Do(ctx context.Context, call Call) Result {
	start := l.clk.Now()
	out := Result{TaskID: call.TaskID, FaultUsed: faultName(call.Fault)}
	attempts := call.Attempts
	if attempts <= 0 {
		attempts = l.attempts
	}

	if err := l.applyPreCallFault(ctx, call.Fault); err != nil {
		out.Error = err.Error()
		out.Latency = l.clk.Now().Sub(start)
		out.LatencyMS = out.Latency.Milliseconds()
		return out
	}

	method := call.Method
	if method == "" {
		method = http.MethodGet
	}
	var lastErr error
	for attempt := 1; attempt <= attempts; attempt++ {
		out.Attempts = attempt
		req, err := http.NewRequestWithContext(ctx, method, call.URL, nil)
		if err != nil {
			lastErr = err
			break
		}
		l.inFlight.Add(1)
		resp, err := l.httpc.Do(req)
		l.inFlight.Add(-1)
		if err == nil {
			out.StatusCode = resp.StatusCode
			// Drain and close so the connection can be reused or, when the
			// server replied to a canceled request, returned promptly.
			_ = resp.Body.Close()
			lastErr = nil
			break
		}
		lastErr = err
		if ctxErr := ctx.Err(); ctxErr != nil || attempt == attempts {
			break
		}
		if !l.waitBackoff(ctx, attempt) {
			lastErr = ctx.Err()
			if lastErr == nil {
				lastErr = err
			}
			break
		}
	}
	if lastErr != nil {
		out.Error = classifyTransportError(lastErr)
	}
	out.Latency = l.clk.Now().Sub(start)
	out.LatencyMS = out.Latency.Milliseconds()
	return out
}

// applyPreCallFault handles faults that run before the HTTP call.
func (l *Logic) applyPreCallFault(ctx context.Context, f Fault) error {
	switch f.Kind {
	case FaultNone, FaultReset:
		// reset is realized by pointing the call URL at a resetting endpoint;
		// the caller is expected to have resolved that URL.
		return nil
	case FaultError:
		return errors.New("injected client fault: synthetic error")
	case FaultLatency:
		if !l.wait(ctx, f.Delay) {
			return ctx.Err()
		}
		return nil
	case FaultStall:
		<-ctx.Done()
		return ctx.Err()
	default:
		return fmt.Errorf("injected client fault: unknown kind %q", f.Kind)
	}
}

func (l *Logic) wait(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		return ctx.Err() == nil
	}
	t := l.clk.Timer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C():
		return true
	}
}

// waitBackoff sleeps before the next attempt; returns false if canceled.
func (l *Logic) waitBackoff(ctx context.Context, attempt int) bool {
	if l.backoff <= 0 {
		return ctx.Err() == nil
	}
	d := time.Duration(attempt) * l.backoff
	return l.wait(ctx, d)
}

// ResolveURL turns a task's path or absolute URL into an absolute URL.
func ResolveURL(base, target string) (string, error) {
	if target == "" {
		return "", errors.New("empty url")
	}
	if strings.HasPrefix(target, "http://") || strings.HasPrefix(target, "https://") {
		if _, err := url.Parse(target); err != nil {
			return "", err
		}
		return target, nil
	}
	u, err := url.Parse(base)
	if err != nil {
		return "", err
	}
	ref, err := url.Parse(target)
	if err != nil {
		return "", err
	}
	return u.ResolveReference(ref).String(), nil
}

// connTracker counts established and currently-open TCP connections.
type connTracker struct {
	total  atomic.Int64
	active atomic.Int64
}

func (c *connTracker) wrapDial(
	next func(ctx context.Context, network, addr string) (net.Conn, error),
) func(ctx context.Context, network, addr string) (net.Conn, error) {
	return func(ctx context.Context, network, addr string) (net.Conn, error) {
		conn, err := next(ctx, network, addr)
		if err != nil {
			return nil, err
		}
		c.total.Add(1)
		c.active.Add(1)
		return &trackedConn{Conn: conn, tracker: c}, nil
	}
}

type trackedConn struct {
	net.Conn
	tracker  *connTracker
	released atomic.Bool
}

func (c *trackedConn) Close() error {
	if c.released.CompareAndSwap(false, true) {
		c.tracker.active.Add(-1)
	}
	return c.Conn.Close()
}

func classifyTransportError(err error) string {
	if errors.Is(err, context.Canceled) {
		return "context canceled (request tree canceled this call)"
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "deadline exceeded while waiting on upstream"
	}
	return fmt.Sprintf("transport error: %v", err)
}

func faultName(f Fault) string {
	if f.Kind == FaultNone {
		return ""
	}
	return f.Kind
}

func orDuration(a, b time.Duration) time.Duration {
	if a > 0 {
		return a
	}
	return b
}

func orInt(a, b int) int {
	if a > 0 {
		return a
	}
	return b
}

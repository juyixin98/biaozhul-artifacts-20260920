// Package upstream hosts an in-process fake HTTP service standing in for an
// external dependency. It is deliberately controllable: calls can be made to
// block until explicitly released, fail on demand, and every in-flight /
// canceled / completed call is counted so tests can observe cancellation
// reaching the far end and verify that nothing is left hanging.
package upstream

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"time"
)

// Stats is a point-in-time snapshot of the fake service counters.
type Stats struct {
	Started     int64 `json:"started"`
	Completed   int64 `json:"completed"`
	Failed      int64 `json:"failed"`
	Canceled    int64 `json:"canceled"`
	Cleanups    int64 `json:"cleanups"`
	Inflight    int64 `json:"inflight"`
	MaxInflight int64 `json:"max_inflight"`
	ActiveHolds int   `json:"active_holds"`
}

// Fake is the controllable upstream service.
type Fake struct {
	mux    *http.ServeMux
	server *httptest.Server

	started   atomic.Int64
	completed atomic.Int64
	failed    atomic.Int64
	canceled  atomic.Int64
	cleanups  atomic.Int64
	inflight  atomic.Int64
	maxIn     atomic.Int64

	mu    sync.Mutex
	holds map[string]chan struct{}
}

// New starts the fake service on an ephemeral local port. Callers must
// Close it. Prefer NewHandler plus an existing mux when embedding the fake
// into another in-process server.
func New() *Fake {
	f := NewHandler()
	f.server = httptest.NewServer(f.mux)
	return f
}

// NewHandler builds the fake without starting a server, exposing Handler for
// mounting under a prefix.
func NewHandler() *Fake {
	f := &Fake{holds: make(map[string]chan struct{}), mux: http.NewServeMux()}
	f.mux.HandleFunc("/work", f.handleWork)
	f.mux.HandleFunc("/reset", f.handleReset)
	f.mux.HandleFunc("/cleanup", f.handleCleanup)
	f.mux.HandleFunc("/stats", f.handleStats)
	f.mux.HandleFunc("/admin/release", f.handleRelease)
	f.mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	return f
}

// Handler returns the HTTP handler surface of the fake.
func (f *Fake) Handler() http.Handler { return f.mux }

// URL is the base URL of the fake service.
func (f *Fake) URL() string { return f.server.URL }

// Close stops the underlying HTTP server.
func (f *Fake) Close() { f.server.Close() }

// Release unblocks every /work call waiting on the given hold id.
// Releasing an unknown id is a no-op.
func (f *Fake) Release(id string) {
	f.mu.Lock()
	ch, ok := f.holds[id]
	if ok {
		delete(f.holds, id)
	}
	f.mu.Unlock()
	if ok {
		close(ch)
	}
}

// ReleaseAll unblocks every held call.
func (f *Fake) ReleaseAll() {
	f.mu.Lock()
	ids := make([]string, 0, len(f.holds))
	for id := range f.holds {
		ids = append(ids, id)
	}
	f.mu.Unlock()
	for _, id := range ids {
		f.Release(id)
	}
}

// Snapshot returns the current counters.
func (f *Fake) Snapshot() Stats {
	f.mu.Lock()
	holds := len(f.holds)
	f.mu.Unlock()
	return Stats{
		Started:     f.started.Load(),
		Completed:   f.completed.Load(),
		Failed:      f.failed.Load(),
		Canceled:    f.canceled.Load(),
		Cleanups:    f.cleanups.Load(),
		Inflight:    f.inflight.Load(),
		MaxInflight: f.maxIn.Load(),
		ActiveHolds: holds,
	}
}

func (f *Fake) enter() {
	n := f.inflight.Add(1)
	for {
		max := f.maxIn.Load()
		if n <= max || f.maxIn.CompareAndSwap(max, n) {
			break
		}
	}
	f.started.Add(1)
}

func (f *Fake) leave() { f.inflight.Add(-1) }

// handleWork accepts:
//
//	delay=duration  working time before responding
//	hold=1          register under hold_id and wait for release
//	hold_id=id      hold identifier (required with hold=1)
//	fail=1          respond with status (default 503) instead of 200
//	status=N        failure status code
func (f *Fake) handleWork(w http.ResponseWriter, r *http.Request) {
	f.enter()
	defer f.leave()

	q := r.URL.Query()
	delay := parseDuration(q.Get("delay"), 0)
	status := parseInt(q.Get("status"), http.StatusServiceUnavailable)

	var release <-chan struct{}
	if q.Get("hold") == "1" {
		id := q.Get("hold_id")
		if id == "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "hold_id required with hold=1"})
			return
		}
		ch := f.registerHold(id)
		defer f.unregisterHold(id, ch)
		release = ch
	}

	var timerC <-chan time.Time
	if delay > 0 {
		timer := time.NewTimer(delay)
		defer timer.Stop()
		timerC = timer.C
	}
	// Only wait when there is something to wait for; otherwise a plain call
	// with no hold and no delay must answer immediately.
	if release != nil || timerC != nil {
		select {
		case <-release:
			// Explicitly released; fall through to the response.
		case <-timerC:
			// Delay elapsed; fall through to the response.
		case <-r.Context().Done():
			// The client went away (request canceled or transport torn down).
			f.canceled.Add(1)
			return
		}
	}

	if q.Get("fail") == "1" {
		f.failed.Add(1)
		w.WriteHeader(status)
		_, _ = w.Write([]byte(`{"error":"injected failure"}`))
		return
	}
	f.completed.Add(1)
	writeJSON(w, http.StatusOK, map[string]any{
		"status":      "ok",
		"delay":       delay.String(),
		"server_note": "processed by in-process fake upstream",
	})
}

// handleReset hijacks the TCP connection and closes it without sending any
// HTTP response, forcing a transport-level error on the client. Used by the
// client's "reset" fault.
func (f *Fake) handleReset(w http.ResponseWriter, r *http.Request) {
	f.enter()
	defer f.leave()
	hj, ok := w.(http.Hijacker)
	if !ok {
		f.failed.Add(1)
		http.Error(w, "hijack unsupported", http.StatusInternalServerError)
		return
	}
	conn, _, err := hj.Hijack()
	if err != nil {
		f.failed.Add(1)
		return
	}
	f.failed.Add(1)
	_ = conn.Close()
}

// handleCleanup stands in for a resource-release callback at the dependency.
// It answers quickly so canceled trees can verify cleanup still ran.
func (f *Fake) handleCleanup(w http.ResponseWriter, r *http.Request) {
	f.cleanups.Add(1)
	// Cleanup deliberately ignores the request deadline of the work call;
	// it is invoked with a fresh, bounded context by the engine.
	_ = parseDuration(r.URL.Query().Get("delay"), 0)
	w.WriteHeader(http.StatusNoContent)
}

func (f *Fake) handleStats(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, f.Snapshot())
}

func (f *Fake) handleRelease(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
		return
	}
	id := r.URL.Query().Get("id")
	if id == "all" || id == "" {
		f.ReleaseAll()
	} else {
		f.Release(id)
	}
	w.WriteHeader(http.StatusNoContent)
}

func (f *Fake) registerHold(id string) chan struct{} {
	ch := make(chan struct{})
	f.mu.Lock()
	if existing, ok := f.holds[id]; ok {
		ch = existing
	} else {
		f.holds[id] = ch
	}
	f.mu.Unlock()
	return ch
}

// unregisterHold only removes the registry entry if it still points at ch,
// i.e. it has not already been released and replaced by a new holder.
func (f *Fake) unregisterHold(id string, ch chan struct{}) {
	f.mu.Lock()
	if cur, ok := f.holds[id]; ok && cur == ch {
		delete(f.holds, id)
	}
	f.mu.Unlock()
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func parseDuration(s string, fallback time.Duration) time.Duration {
	if s == "" {
		return fallback
	}
	d, err := time.ParseDuration(s)
	if err != nil {
		return fallback
	}
	return d
}

func parseInt(s string, fallback int) int {
	if s == "" {
		return fallback
	}
	var n int
	if _, err := fmt.Sscanf(s, "%d", &n); err != nil || n <= 0 {
		return fallback
	}
	return n
}

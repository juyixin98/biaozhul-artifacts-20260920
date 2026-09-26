// Package fakesvc is the in-process fake external dependency. Faults are
// injected per request via X-Fault-* headers; failure counts are keyed by a
// client-supplied fault ID so retries of the same logical request see a
// consistent, stateful fault plan.
package fakesvc

import (
	"encoding/json"
	"net/http"
	"strconv"
	"sync"
	"time"
)

// Fault headers understood by the fake service.
const (
	// HeaderFaultID identifies the logical request; state (e.g. remaining
	// failures) is tracked per ID.
	HeaderFaultID = "X-Fault-Id"
	// HeaderFaultFailTimes makes the first N calls for this fault ID fail.
	HeaderFaultFailTimes = "X-Fault-Fail-Times"
	// HeaderFaultStatus is the failure status code (default 503).
	HeaderFaultStatus = "X-Fault-Status"
	// HeaderFaultRetryAfter sets a Retry-After header (delta-seconds) on
	// failure responses.
	HeaderFaultRetryAfter = "X-Fault-Retry-After"
	// HeaderFaultHangMs makes the handler block for the given milliseconds
	// (or until the request context is cancelled).
	HeaderFaultHangMs = "X-Fault-Hang-Ms"
)

type state struct {
	failuresLeft int
	calls        int
}

// Service is a fake external dependency with stateful fault injection.
type Service struct {
	mu     sync.Mutex
	states map[string]*state
}

func New() *Service { return &Service{states: map[string]*state{}} }

// Calls reports how many requests arrived for a fault ID (test introspection).
func (s *Service) Calls(faultID string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	if st, ok := s.states[faultID]; ok {
		return st.calls
	}
	return 0
}

func (s *Service) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	h := r.Header
	id := h.Get(HeaderFaultID)
	failTimes := intHeader(h, HeaderFaultFailTimes, 0)
	status := intHeader(h, HeaderFaultStatus, http.StatusServiceUnavailable)
	hangMs := intHeader(h, HeaderFaultHangMs, 0)

	if hangMs > 0 {
		t := time.NewTimer(time.Duration(hangMs) * time.Millisecond)
		defer t.Stop()
		select {
		case <-r.Context().Done():
			writeJSON(w, 499, map[string]any{"ok": false, "layer": "fake", "reason": "canceled_while_hanging"})
			return
		case <-t.C:
		}
	}

	shouldFail := false
	if id != "" {
		s.mu.Lock()
		st := s.states[id]
		if st == nil {
			st = &state{failuresLeft: failTimes}
			s.states[id] = st
		}
		st.calls++
		if st.failuresLeft > 0 {
			st.failuresLeft--
			shouldFail = true
		}
		s.mu.Unlock()
	}

	if shouldFail {
		if ra := h.Get(HeaderFaultRetryAfter); ra != "" {
			w.Header().Set("Retry-After", ra)
		}
		writeJSON(w, status, map[string]any{"ok": false, "layer": "fake", "reason": "injected_fault"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "layer": "fake"})
}

func intHeader(h http.Header, name string, def int) int {
	s := h.Get(name)
	if s == "" {
		return def
	}
	n, err := strconv.Atoi(s)
	if err != nil {
		return def
	}
	return n
}

func writeJSON(w http.ResponseWriter, status int, body map[string]any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(body)
}

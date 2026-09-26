// Package fakedep is an in-process stand-in for an external dependency. It is
// a real HTTP server bound to localhost only — no production systems involved —
// and supports fault injection: latency, hard errors, and hangs that can be
// released. It is also registered as a shutdown.Resource.
package fakedep

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"time"
)

// Fault describes how the fake dependency should misbehave.
type Fault struct {
	Latency time.Duration // normal response delay
	Fail    bool          // return 500
	Hang    bool          // ignore latency; block until released or request ctx ends
}

// Server is the controllable fake external service.
type Server struct {
	mu        sync.Mutex
	fault     Fault
	release   chan struct{}
	hangCount atomic.Int64
	openHangs sync.WaitGroup
	closed    atomic.Bool

	httpd *httptest.Server
}

func New() *Server {
	s := &Server{release: make(chan struct{})}
	mux := http.NewServeMux()
	mux.HandleFunc("/work", s.handleWork)
	mux.HandleFunc("/health", s.handleHealth)
	s.httpd = httptest.NewServer(mux)
	return s
}

func (s *Server) URL() string { return s.httpd.URL }

// SetFault atomically changes injected behavior.
func (s *Server) SetFault(f Fault) {
	s.mu.Lock()
	s.fault = f
	s.mu.Unlock()
}

func (s *Server) faultSnapshot() Fault {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.fault
}

func (s *Server) currentRelease() chan struct{} {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.release
}

// ReleaseHangs unblocks all currently hanging requests.
func (s *Server) ReleaseHangs() {
	s.mu.Lock()
	close(s.release)
	s.release = make(chan struct{})
	s.mu.Unlock()
}

// ActiveHangs reports how many requests are currently hung.
func (s *Server) ActiveHangs() int64 { return s.hangCount.Load() }

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	if s.closed.Load() {
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	w.WriteHeader(http.StatusOK)
}

func (s *Server) handleWork(w http.ResponseWriter, r *http.Request) {
	if s.closed.Load() {
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	f := s.faultSnapshot()

	if f.Hang {
		s.hangCount.Add(1)
		defer s.hangCount.Add(-1)
		s.openHangs.Add(1)
		defer s.openHangs.Done()
		select {
		case <-r.Context().Done():
			w.WriteHeader(499) // client cancelled / gone
			return
		case <-s.currentRelease():
		}
	} else if f.Latency > 0 {
		select {
		case <-r.Context().Done():
			w.WriteHeader(499)
			return
		case <-time.After(f.Latency):
		}
	}
	if f.Fail {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("injected failure"))
		return
	}
	w.Header().Set("X-Fake-Dep", "ok")
	_, _ = fmt.Fprintf(w, "processed at %s", time.Now().Format(time.RFC3339Nano))
}

// Name / Close implement shutdown.Resource.
func (s *Server) Name() string { return "fake-external-dependency" }

// Close marks the dependency unavailable, releases hung calls, waits for them
// to unwind (bounded by ctx), drops client connections and stops the server.
func (s *Server) Close(ctx context.Context) error {
	s.closed.Store(true)
	s.ReleaseHangs()

	done := make(chan struct{})
	go func() { s.openHangs.Wait(); close(done) }()
	select {
	case <-done:
	case <-ctx.Done():
		s.httpd.CloseClientConnections()
		<-done
	}
	s.httpd.Close()
	return nil
}

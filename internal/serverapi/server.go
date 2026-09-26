// Package serverapi exposes the HTTP entry point. A POST /process fans the
// request out to several downstream subtasks as a cancellation tree and
// returns a structured report. Downstream dependencies are in-process fake
// services; no production system is contacted.
package serverapi

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"runtime"
	"sync/atomic"
	"time"

	"cancelprop/internal/canceltree"
	"cancelprop/internal/clock"
	"cancelprop/internal/fakesvc"
	"cancelprop/internal/faultclient"
	"cancelprop/internal/resguard"
)

// LeafSpec declares one downstream subtask.
type LeafSpec struct {
	// Name identifies the leaf in the report (required, unique).
	Name string `json:"name"`
	// Service selects the fake downstream ("a", "b", ...). Default "a".
	Service string `json:"service,omitempty"`
	// Fatal makes this leaf's own failure cancel the other leaves.
	Fatal bool `json:"fatal"`
	// Gate makes the downstream block until that gate is released.
	Gate string `json:"gate,omitempty"`
	// HoldMS makes the downstream wait this long before answering.
	HoldMS int64 `json:"hold_ms,omitempty"`
	// Fail forces a downstream HTTP 500.
	Fail bool `json:"fail,omitempty"`
	// FailRequest injects an immediate client-side transport error.
	FailRequest bool `json:"fail_request,omitempty"`
	// DelayMS injects a clock-driven delay before the request is sent.
	DelayMS int64 `json:"delay_ms,omitempty"`
	// TimeoutMS bounds the call using the injected clock.
	TimeoutMS int64 `json:"timeout_ms,omitempty"`
	// Resource names an exclusive resource the leaf holds until cleanup.
	Resource string `json:"resource,omitempty"`
}

// ProcessRequest is the body of POST /process.
type ProcessRequest struct {
	Leaves []LeafSpec `json:"leaves"`
}

// Config wires the server.
type Config struct {
	Clock    clock.Clock
	Services []*fakesvc.Server
}

// Server is the application HTTP server.
type Server struct {
	clk      clock.Clock
	services map[string]*fakesvc.Server
	clients  map[string]*faultclient.Client
	reg      *resguard.Registry

	reqID       atomic.Int64
	inFlightReq atomic.Int64
}

// New builds the app server over the given fake downstream services.
func New(cfg Config) *Server {
	clk := cfg.Clock
	if clk == nil {
		clk = clock.NewRealClock()
	}
	s := &Server{
		clk:      clk,
		services: make(map[string]*fakesvc.Server),
		clients:  make(map[string]*faultclient.Client),
		reg:      resguard.NewRegistry(),
	}
	for _, fs := range cfg.Services {
		s.services[fs.Name] = fs
		s.clients[fs.Name] = faultclient.New("client-"+fs.Name, fs.URL(), clk)
	}
	return s
}

// Handler returns the HTTP mux.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealth)
	mux.HandleFunc("/process", s.handleProcess)
	mux.HandleFunc("/stats", s.handleStats)
	mux.HandleFunc("/admin/release", s.handleAdminRelease)
	mux.HandleFunc("/admin/close-idle-connections", s.handleCloseIdle)
	return mux
}

// Resources exposes the resource registry (tests).
func (s *Server) Resources() *resguard.Registry { return s.reg }

// FakeService returns a registered fake service by name (tests/admin).
func (s *Server) FakeService(name string) *fakesvc.Server { return s.services[name] }

// InFlightRequests reports requests currently executing a tree.
func (s *Server) InFlightRequests() int64 { return s.inFlightReq.Load() }

// CloseIdleConnections drains pooled client connections.
func (s *Server) CloseIdleConnections() {
	for _, c := range s.clients {
		c.CloseIdleConns()
	}
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleProcess(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
		return
	}
	var req ProcessRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}
	if len(req.Leaves) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "at least one leaf is required"})
		return
	}

	reqID := s.reqID.Add(1)
	s.inFlightReq.Add(1)
	defer s.inFlightReq.Add(-1)

	leaves := make([]canceltree.Leaf, 0, len(req.Leaves))
	for i := range req.Leaves {
		spec := req.Leaves[i]
		if spec.Name == "" {
			spec.Name = fmt.Sprintf("leaf-%d", i+1)
		}
		svcName := spec.Service
		if svcName == "" {
			svcName = "a"
		}
		client, ok := s.clients[svcName]
		if !ok {
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error": fmt.Sprintf("unknown service %q (leaf %q)", svcName, spec.Name),
			})
			return
		}
		leaves = append(leaves, s.buildLeaf(reqID, spec, client))
	}

	// The request context is the root of the tree: closing the client
	// connection cancels r.Context() and therefore every leaf.
	tree := canceltree.New(leaves...).WithClock(s.clk.Now)
	report := tree.Run(r.Context())

	if report.Canceled {
		// The client is gone (or the request was aborted): writing a body
		// cannot succeed, so do not emit one. All leaves and cleanups have
		// nonetheless completed before this point.
		return
	}
	status := http.StatusOK
	if report.OK {
		status = http.StatusOK
	} else {
		status = http.StatusBadGateway
	}
	writeJSON(w, status, report)
}

// buildLeaf wires one spec to a fault-client call, holding any named
// resource for the duration and releasing it in the always-awaited cleanup.
func (s *Server) buildLeaf(reqID int64, spec LeafSpec, c *faultclient.Client) canceltree.Leaf {
	var release func() bool
	leaf := canceltree.Leaf{
		Name:  spec.Name,
		Fatal: spec.Fatal,
		Run: func(ctx context.Context) error {
			if spec.Resource != "" {
				release = s.reg.Acquire(reqID, spec.Resource)
			}
			path := buildPath(spec)
			f := faultclient.Faults{
				FailRequest:         spec.FailRequest,
				ForceDownstreamFail: spec.Fail,
				DelayBefore:         time.Duration(spec.DelayMS) * time.Millisecond,
				Timeout:             time.Duration(spec.TimeoutMS) * time.Millisecond,
			}
			_, err := c.CallWith(ctx, path, f)
			return err
		},
		Cleanup: func(context.Context) error {
			if release != nil {
				release()
			}
			return nil
		},
	}
	return leaf
}

func buildPath(spec LeafSpec) string {
	path := "/work?"
	if spec.Gate != "" {
		path += "gate=" + spec.Gate
	}
	if spec.HoldMS > 0 {
		if spec.Gate != "" {
			path += "&"
		}
		path += fmt.Sprintf("hold_ms=%d", spec.HoldMS)
	}
	return path
}

// ServiceStats is the stats view returned by GET /stats.
type ServiceStats struct {
	Goroutines    int             `json:"goroutines"`
	HeapAllocB    uint64          `json:"heap_alloc_bytes"`
	InFlightReq   int64           `json:"in_flight_requests"`
	HeldResources int             `json:"held_resources"`
	Acquired      int64           `json:"resources_acquired_total"`
	Released      int64           `json:"resources_released_total"`
	Services      []fakesvc.Stats `json:"services"`
}

func (s *Server) handleStats(w http.ResponseWriter, _ *http.Request) {
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	acq, rel := s.reg.Totals()
	out := ServiceStats{
		Goroutines:    runtime.NumGoroutine(),
		HeapAllocB:    ms.HeapAlloc,
		InFlightReq:   s.inFlightReq.Load(),
		HeldResources: s.reg.Active(),
		Acquired:      acq,
		Released:      rel,
		Services:      make([]fakesvc.Stats, 0, len(s.services)),
	}
	for _, fs := range s.services {
		out.Services = append(out.Services, fs.Snapshot())
	}
	writeJSON(w, http.StatusOK, out)
}

// handleAdminRelease opens a gate on every fake service.
func (s *Server) handleAdminRelease(w http.ResponseWriter, r *http.Request) {
	gate := r.URL.Query().Get("gate")
	if gate == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "gate query parameter required"})
		return
	}
	for _, fs := range s.services {
		fs.Release(gate)
	}
	writeJSON(w, http.StatusOK, map[string]string{"released": gate})
}

// handleCloseIdle drains all pooled client keep-alive connections so a
// post-request /stats reading reflects zero active downstream connections.
func (s *Server) handleCloseIdle(w http.ResponseWriter, _ *http.Request) {
	s.CloseIdleConnections()
	writeJSON(w, http.StatusOK, map[string]string{"status": "closed"})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// Package demoapp wires the breaker, fault-injecting client, fake upstream
// and virtual clock into a local HTTP service.
//
// Everything is in-process and deterministic: the virtual clock only moves
// when POST /clock/advance is called, the upstream only answers in the mode
// set via POST /upstream/mode, and hung upstream calls only complete after
// POST /upstream/release. This makes races like "an old failure arriving
// after the breaker recovered" reproducible on demand.
package demoapp

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"sync"
	"time"

	"cbhalfopen/internal/breaker"
	"cbhalfopen/internal/fakeupstream"
	"cbhalfopen/internal/faultclient"
	"cbhalfopen/internal/vclock"
)

// Server is the demo HTTP service.
type Server struct {
	vc     *vclock.VirtualClock
	fake   *fakeupstream.Fake
	client *faultclient.Client

	mu      sync.Mutex
	breaker *breaker.Breaker
	cfg     breaker.Config

	calls   map[uint64]*asyncCall
	nextID  uint64
	handler http.Handler
}

type asyncCall struct {
	ID      uint64              `json:"id"`
	Started time.Time           `json:"started"`
	Done    bool                `json:"done"`
	Result  *faultclient.Result `json:"result,omitempty"`
	cancel  context.CancelFunc
}

// New builds the server. bcfg zero fields receive breaker defaults; ccfg
// configures the fault-injecting client.
func New(bcfg breaker.Config, ccfg faultclient.Config) *Server {
	vc := vclock.NewVirtual(time.Time{})
	fake := fakeupstream.New(vc, fakeupstream.ModeOK)
	client := faultclient.New(fake, vc, ccfg)

	bcfg.Clock = vc
	b, err := breaker.New(bcfg)
	if err != nil {
		panic(fmt.Sprintf("demoapp: invalid breaker config: %v", err))
	}

	s := &Server{
		vc:      vc,
		fake:    fake,
		client:  client,
		breaker: b,
		cfg:     bcfg,
		calls:   make(map[uint64]*asyncCall),
	}
	s.handler = s.routes()
	return s
}

// Handler returns the root HTTP handler.
func (s *Server) Handler() http.Handler { return s.handler }

func (s *Server) routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("POST /call", s.handleCall)
	mux.HandleFunc("POST /call/async", s.handleCallAsync)
	mux.HandleFunc("POST /call/{id}/cancel", s.handleCallCancel)
	mux.HandleFunc("GET /calls", s.handleCalls)
	mux.HandleFunc("GET /state", s.handleState)
	mux.HandleFunc("GET /transitions", s.handleTransitions)
	mux.HandleFunc("GET /clock", s.handleClock)
	mux.HandleFunc("POST /clock/advance", s.handleClockAdvance)
	mux.HandleFunc("POST /upstream/mode", s.handleUpstreamMode)
	mux.HandleFunc("GET /upstream/pending", s.handleUpstreamPending)
	mux.HandleFunc("POST /upstream/release", s.handleUpstreamRelease)
	mux.HandleFunc("GET /upstream/attempts", s.handleUpstreamAttempts)
	mux.HandleFunc("POST /client/config", s.handleClientConfig)
	mux.HandleFunc("GET /client/config", s.handleClientConfigGet)
	mux.HandleFunc("POST /reset", s.handleReset)
	return mux
}

func (s *Server) getBreaker() *breaker.Breaker {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.breaker
}

// --- call endpoints ---------------------------------------------------------

type callRequest struct {
	// TimeoutMS overrides the client timeout for this call (virtual ms).
	TimeoutMS *int64 `json:"timeout_ms,omitempty"`
}

func (s *Server) handleCall(w http.ResponseWriter, r *http.Request) {
	var req callRequest
	if r.Body != nil && r.ContentLength != 0 {
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
			return
		}
	}
	res, rejected := s.doCall(r.Context(), req)
	writeJSON(w, http.StatusOK, map[string]any{
		"rejected": rejected,
		"result":   res,
		"snapshot": s.getBreaker().Snapshot(),
	})
}

func (s *Server) doCall(ctx context.Context, req callRequest) (*faultclient.Result, string) {
	b := s.getBreaker()
	permit, err := b.Allow()
	if err != nil {
		return nil, err.Error()
	}

	if req.TimeoutMS != nil {
		d := time.Duration(*req.TimeoutMS) * time.Millisecond
		res := s.client.Do(ctx, permit, faultclient.WithTimeout(d))
		return &res, ""
	}

	res := s.client.Do(ctx, permit)
	return &res, ""
}

func (s *Server) handleCallAsync(w http.ResponseWriter, r *http.Request) {
	var req callRequest
	if r.Body != nil && r.ContentLength != 0 {
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
			return
		}
	}

	s.mu.Lock()
	s.nextID++
	id := s.nextID
	ctx, cancel := context.WithCancel(context.Background())
	call := &asyncCall{ID: id, Started: s.vc.Now(), cancel: cancel}
	s.calls[id] = call
	s.mu.Unlock()

	go func() {
		res, rejected := s.doCall(ctx, req)
		s.mu.Lock()
		call.Done = true
		if res != nil {
			r := *res
			call.Result = &r
		} else {
			call.Result = &faultclient.Result{Reason: "rejected: " + rejected}
		}
		s.mu.Unlock()
	}()

	writeJSON(w, http.StatusAccepted, map[string]any{"id": id})
}

func (s *Server) handleCallCancel(w http.ResponseWriter, r *http.Request) {
	var id uint64
	if _, err := fmt.Sscanf(r.PathValue("id"), "%d", &id); err != nil {
		writeError(w, http.StatusBadRequest, "invalid call id")
		return
	}
	s.mu.Lock()
	call, ok := s.calls[id]
	s.mu.Unlock()
	if !ok {
		writeError(w, http.StatusNotFound, "no such call")
		return
	}
	call.cancel()
	writeJSON(w, http.StatusOK, map[string]any{"canceled": id})
}

func (s *Server) handleCalls(w http.ResponseWriter, _ *http.Request) {
	s.mu.Lock()
	out := make([]asyncCall, 0, len(s.calls))
	for _, c := range s.calls {
		out = append(out, *c)
	}
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{"calls": out})
}

// --- inspection endpoints ---------------------------------------------------

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *Server) handleState(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.getBreaker().Snapshot())
}

func (s *Server) handleTransitions(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"transitions": s.getBreaker().Transitions()})
}

func (s *Server) handleClock(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"now":            s.vc.Now(),
		"pending_timers": s.vc.Pending(),
	})
}

type advanceRequest struct {
	MS int64 `json:"ms"`
}

func (s *Server) handleClockAdvance(w http.ResponseWriter, r *http.Request) {
	var req advanceRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if req.MS < 0 {
		writeError(w, http.StatusBadRequest, "ms must be >= 0")
		return
	}
	fired := s.vc.Advance(time.Duration(req.MS) * time.Millisecond)
	writeJSON(w, http.StatusOK, map[string]any{
		"now":          s.vc.Now(),
		"timers_fired": fired,
	})
}

// --- upstream / client control ----------------------------------------------

type modeRequest struct {
	Mode fakeupstream.Mode `json:"mode"`
}

func (s *Server) handleUpstreamMode(w http.ResponseWriter, r *http.Request) {
	var req modeRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	switch req.Mode {
	case fakeupstream.ModeOK, fakeupstream.ModeFail, fakeupstream.ModeError, fakeupstream.ModeHang:
		s.fake.SetMode(req.Mode)
		writeJSON(w, http.StatusOK, map[string]any{"mode": req.Mode})
	default:
		writeError(w, http.StatusBadRequest, "unknown mode")
	}
}

func (s *Server) handleUpstreamPending(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"pending": s.fake.Pending()})
}

type releaseRequest struct {
	Mode fakeupstream.Mode `json:"mode"`
	ID   *uint64           `json:"id,omitempty"`
	All  bool              `json:"all,omitempty"`
}

func (s *Server) handleUpstreamRelease(w http.ResponseWriter, r *http.Request) {
	var req releaseRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if req.Mode == "" {
		req.Mode = fakeupstream.ModeOK
	}
	var n int
	switch {
	case req.All:
		n = s.fake.ReleaseAll(req.Mode)
	case req.ID != nil:
		if s.fake.Release(req.Mode, *req.ID) {
			n = 1
		}
	default:
		if s.fake.Release(req.Mode) {
			n = 1
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"released": n})
}

func (s *Server) handleUpstreamAttempts(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"attempts": s.fake.Attempts()})
}

type clientConfigRequest struct {
	TimeoutMS    *int64 `json:"timeout_ms,omitempty"`
	FailNextN    *int   `json:"fail_next_n,omitempty"`
	FailEveryNth *int   `json:"fail_every_nth,omitempty"`
}

func (s *Server) handleClientConfig(w http.ResponseWriter, r *http.Request) {
	var req clientConfigRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	cfg := s.client.Config()
	if req.TimeoutMS != nil {
		cfg.Timeout = time.Duration(*req.TimeoutMS) * time.Millisecond
	}
	if req.FailNextN != nil {
		cfg.FailNextN = *req.FailNextN
	}
	if req.FailEveryNth != nil {
		cfg.FailEveryNth = *req.FailEveryNth
	}
	s.client.SetConfig(cfg)
	writeJSON(w, http.StatusOK, s.client.Config())
}

func (s *Server) handleClientConfigGet(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.client.Config())
}

func (s *Server) handleReset(w http.ResponseWriter, _ *http.Request) {
	s.mu.Lock()
	b, err := breaker.New(s.cfg)
	if err != nil {
		s.mu.Unlock()
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.breaker = b
	s.calls = make(map[uint64]*asyncCall)
	s.mu.Unlock()
	s.client.SetConfig(faultclient.Config{})
	writeJSON(w, http.StatusOK, map[string]any{"reset": true, "state": b.Snapshot().State})
}

// --- helpers ----------------------------------------------------------------

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		log.Printf("demoapp: encode response: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"error": msg})
}

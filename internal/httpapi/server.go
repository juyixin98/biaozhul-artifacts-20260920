// Package httpapi exposes the breaker demo over localhost HTTP. Everything is
// in-process: the "downstream" is the fake upstream and time is a virtual
// clock advanced explicitly via the API, so a curl session can reproduce
// tripping, half-open probe contention and late failures deterministically.
package httpapi

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"sync"
	"time"

	"breakerhalfopen/internal/appclient"
	"breakerhalfopen/internal/breaker"
	"breakerhalfopen/internal/clock"
	"breakerhalfopen/internal/scenario"
	"breakerhalfopen/internal/upstream"
)

// Service is the HTTP-served demo environment.
type Service struct {
	cfg breaker.Config

	mu     sync.Mutex
	clk    *clock.Virtual
	up     *upstream.Upstream
	brk    *breaker.Breaker
	client *appclient.Client
}

// NewService builds the demo environment (virtual clock, healthy upstream).
func NewService(cfg breaker.Config) *Service {
	s := &Service{cfg: cfg}
	s.resetLocked()
	return s
}

func (s *Service) resetLocked() {
	clk := clock.NewVirtual()
	up := upstream.New(clk, upstream.Directive{}) // default: healthy
	brk := breaker.New(s.cfg, clk)
	s.clk = clk
	s.up = up
	s.brk = brk
	s.client = appclient.New(brk, up, clk, 0)
}

// Handler wires the routes.
func (s *Service) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /", s.handleIndex)
	mux.HandleFunc("POST /api/reset", s.handleReset)
	mux.HandleFunc("GET /api/state", s.handleState)
	mux.HandleFunc("GET /api/history", s.handleHistory)
	mux.HandleFunc("POST /api/call", s.handleCall)
	mux.HandleFunc("POST /api/clock/advance", s.handleAdvance)
	mux.HandleFunc("POST /api/upstream/behavior", s.handleBehavior)
	mux.HandleFunc("POST /api/upstream/release", s.handleRelease)
	mux.HandleFunc("GET /api/upstream", s.handleUpstream)
	mux.HandleFunc("GET /api/scenarios", s.handleListScenarios)
	mux.HandleFunc("POST /api/scenarios/{name}/run", s.handleRunScenario)
	return logRequests(mux)
}

type envelope struct {
	Success bool   `json:"success"`
	Data    any    `json:"data,omitempty"`
	Error   string `json:"error,omitempty"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func okJSON(w http.ResponseWriter, data any) {
	writeJSON(w, http.StatusOK, envelope{Success: true, Data: data})
}

func errJSON(w http.ResponseWriter, status int, format string, args ...any) {
	writeJSON(w, status, envelope{Success: false, Error: fmt.Sprintf(format, args...)})
}

func (s *Service) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		errJSON(w, http.StatusNotFound, "not found")
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	fmt.Fprint(w, indexText)
}

func (s *Service) handleReset(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	s.resetLocked()
	cfg := s.cfg
	now := s.clk.Now()
	s.mu.Unlock()
	okJSON(w, map[string]any{"reset": true, "config": cfg, "virtual_now": now})
}

func (s *Service) handleState(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	snap := s.brk.Snapshot()
	now := s.clk.Now()
	s.mu.Unlock()
	okJSON(w, map[string]any{"virtual_now": now, "breaker": snap})
}

func (s *Service) handleHistory(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	h := s.client.History()
	recs := s.up.Records()
	s.mu.Unlock()
	okJSON(w, map[string]any{"client_attempts": h, "upstream_calls": recs})
}

type callReq struct {
	// TimeoutMS optionally bounds the call with a clock-driven deadline.
	TimeoutMS int64 `json:"timeout_ms"`
	// Label annotates the attempt in server logs.
	Label string `json:"label"`
}

type callResp struct {
	Attempt    appclient.Attempt `json:"attempt"`
	VirtualNow time.Time         `json:"virtual_now"`
}

func (s *Service) handleCall(w http.ResponseWriter, r *http.Request) {
	req := callReq{}
	if r.ContentLength != 0 {
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			errJSON(w, http.StatusBadRequest, "invalid JSON body: %v", err)
			return
		}
	}

	s.mu.Lock()
	clk := s.clk
	client := s.client
	s.mu.Unlock()

	ctx := r.Context()
	var cancel context.CancelFunc
	if req.TimeoutMS > 0 {
		ctx, cancel = clock.WithTimeout(ctx, clk, time.Duration(req.TimeoutMS)*time.Millisecond)
		defer cancel()
	}

	a := client.Call(ctx)
	if req.Label != "" {
		log.Printf("call label=%q result=%s", req.Label, a.Result)
	}
	resp := callResp{Attempt: a, VirtualNow: clk.Now()}
	if a.Result == appclient.ResultRejected {
		// The breaker refused to start the call: 503 with the structured
		// attempt still included for inspection.
		writeJSON(w, http.StatusServiceUnavailable, envelope{Success: false, Data: resp, Error: breaker.ErrOpen.Error()})
		return
	}
	okJSON(w, resp)
}

type advanceReq struct {
	DurationMS int64 `json:"duration_ms"`
}

func (s *Service) handleAdvance(w http.ResponseWriter, r *http.Request) {
	req := advanceReq{}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		errJSON(w, http.StatusBadRequest, "invalid JSON body: %v", err)
		return
	}
	s.mu.Lock()
	before := s.clk.Now()
	s.clk.Advance(time.Duration(req.DurationMS) * time.Millisecond)
	after := s.clk.Now()
	snap := s.brk.Snapshot()
	s.mu.Unlock()
	okJSON(w, map[string]any{"before": before, "after": after, "breaker": snap})
}

type behaviorReq struct {
	Fail    bool  `json:"fail"`
	Stall   bool  `json:"stall"`
	DelayMS int64 `json:"delay_ms"`
	// Dynamic=false clears the override, reverting to the (healthy) default.
	Dynamic *bool `json:"dynamic,omitempty"`
}

func (s *Service) handleBehavior(w http.ResponseWriter, r *http.Request) {
	req := behaviorReq{}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		errJSON(w, http.StatusBadRequest, "invalid JSON body: %v", err)
		return
	}
	s.mu.Lock()
	if req.Dynamic != nil && !*req.Dynamic {
		s.up.SetBehavior(nil)
	} else {
		s.up.SetBehavior(&upstream.Directive{
			Fail:  req.Fail,
			Stall: req.Stall,
			Delay: time.Duration(req.DelayMS) * time.Millisecond,
		})
	}
	b := s.up.Behavior()
	s.mu.Unlock()
	okJSON(w, map[string]any{"behavior": b})
}

func (s *Service) handleRelease(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	n := s.up.ReleaseStalled()
	s.mu.Unlock()
	okJSON(w, map[string]any{"released": n})
}

func (s *Service) handleUpstream(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	state := map[string]any{
		"active_calls":  s.up.ActiveCalls(),
		"stalled_calls": s.up.StalledCalls(),
		"behavior":      s.up.Behavior(),
		"records":       s.up.Records(),
	}
	s.mu.Unlock()
	okJSON(w, state)
}

var scenarioList = []struct {
	Name string
	Fn   func() scenario.Report
}{
	{"late_failure", scenario.ScenarioLateFailure},
	{"probe_contention", scenario.ScenarioProbeContention},
	{"cancel_is_not_failure", scenario.ScenarioCancelIsNotFailure},
}

func (s *Service) handleListScenarios(w http.ResponseWriter, r *http.Request) {
	names := make([]string, 0, len(scenarioList))
	for _, sc := range scenarioList {
		names = append(names, sc.Name)
	}
	okJSON(w, map[string]any{"scenarios": names})
}

func (s *Service) handleRunScenario(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	for _, sc := range scenarioList {
		if sc.Name != name {
			continue
		}
		// Each scenario builds its own isolated environment; virtual time
		// inside a run is real-time fast (no goroutine sleeps).
		rep := sc.Fn()
		status := http.StatusOK
		if !rep.Pass {
			status = http.StatusUnprocessableEntity
		}
		writeJSON(w, status, envelope{Success: rep.Pass, Data: rep})
		return
	}
	errJSON(w, http.StatusNotFound, "unknown scenario %q", name)
}

const indexText = `Circuit breaker half-open contention demo (in-process, virtual clock)

Endpoints:
  GET  /api/state                     breaker snapshot + virtual time
  GET  /api/history                   client attempts + upstream call records
  POST /api/call                      {"timeout_ms":0} make one guarded call
  POST /api/clock/advance             {"duration_ms":10000} move virtual time
  POST /api/upstream/behavior         {"fail":true} | {"stall":true} | {"fail":false}
  POST /api/upstream/release          release all stalled fake-upstream calls
  GET  /api/upstream                  active/stalled calls and records
  POST /api/reset                     reset clock, breaker and fake upstream
  GET  /api/scenarios                 list built-in acceptance scenarios
  POST /api/scenarios/{name}/run      run one scenario, structured JSON report

Scenarios: late_failure, probe_contention, cancel_is_not_failure
`

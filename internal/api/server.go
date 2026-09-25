// Package api exposes the engine over HTTP.
package api

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"

	"alertfsm/internal/engine"
	"alertfsm/internal/model"
	"alertfsm/internal/store"
)

// Server wires the engine to HTTP handlers.
type Server struct {
	eng *engine.Engine
	st  *store.Store
	mux *http.ServeMux
}

func NewServer(eng *engine.Engine, st *store.Store) *Server {
	s := &Server{eng: eng, st: st}
	mux := http.NewServeMux()

	mux.HandleFunc("GET /health", s.health)
	mux.HandleFunc("GET /api/v1/clock", s.getClock)
	mux.HandleFunc("POST /api/v1/admin/tick", s.tick)

	mux.HandleFunc("POST /api/v1/rules", s.createRule)
	mux.HandleFunc("GET /api/v1/rules", s.listRules)
	mux.HandleFunc("GET /api/v1/rules/{id}", s.getRule)
	mux.HandleFunc("PUT /api/v1/rules/{id}", s.updateRule)
	mux.HandleFunc("DELETE /api/v1/rules/{id}", s.deleteRule)

	mux.HandleFunc("GET /api/v1/states", s.listStates)
	mux.HandleFunc("GET /api/v1/rules/{id}/state", s.getState)

	mux.HandleFunc("POST /api/v1/ingest", s.ingest)
	mux.HandleFunc("GET /api/v1/samples", s.listSamples)

	mux.HandleFunc("GET /api/v1/events", s.listEvents)

	s.mux = mux
	return s
}

func (s *Server) Handler() http.Handler {
	return logging(s.mux)
}

// ruleDTO is the rule representation on the wire.
type ruleDTO struct {
	ID          string  `json:"id"`
	Metric      string  `json:"metric"`
	Threshold   float64 `json:"threshold"`
	Direction   string  `json:"direction"`
	PendingFor  int64   `json:"pending_for_ms"`
	RecoveryFor int64   `json:"recovery_for_ms"`
	NoDataFor   int64   `json:"no_data_for_ms"`
	Enabled     bool    `json:"enabled"`
}

func toDTO(r model.Rule) ruleDTO {
	return ruleDTO{
		ID:          r.ID,
		Metric:      r.Metric,
		Threshold:   r.Threshold,
		Direction:   r.Direction,
		PendingFor:  r.PendingFor.Milliseconds(),
		RecoveryFor: r.RecoveryFor.Milliseconds(),
		NoDataFor:   r.NoDataFor.Milliseconds(),
		Enabled:     r.Enabled,
	}
}

// ruleRequest accepts durations as ms (number) or strings ("30s").
type ruleRequest struct {
	ID          string           `json:"id"`
	Metric      string           `json:"metric"`
	Threshold   float64          `json:"threshold"`
	Direction   string           `json:"direction"`
	PendingFor  model.DurationMS `json:"pending_for"`
	RecoveryFor model.DurationMS `json:"recovery_for"`
	NoDataFor   model.DurationMS `json:"no_data_for"`
}

func (req ruleRequest) toModel() model.Rule {
	return model.Rule{
		ID:          req.ID,
		Metric:      req.Metric,
		Threshold:   req.Threshold,
		Direction:   req.Direction,
		PendingFor:  req.PendingFor,
		RecoveryFor: req.RecoveryFor,
		NoDataFor:   req.NoDataFor,
	}
}

type ingestRequest struct {
	Samples []model.Sample `json:"samples"`
}

type tickRequest struct {
	// Advance to an absolute virtual time.
	ToMS int64 `json:"to_ms"`
	// Or advance by a relative duration ("30s" or milliseconds).
	ByMS int64            `json:"by_ms"`
	By   model.DurationMS `json:"by"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"error": msg})
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "clock_ms": s.eng.Clock()})
}

func (s *Server) getClock(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"clock_ms": s.eng.Clock()})
}

func (s *Server) tick(w http.ResponseWriter, r *http.Request) {
	var req tickRequest
	if r.Body != nil {
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil && !errors.Is(err, io.EOF) {
			writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
	}
	target := req.ToMS
	if req.ByMS != 0 {
		target = s.eng.Clock() + req.ByMS
	}
	if req.By != 0 {
		target = s.eng.Clock() + req.By.Milliseconds()
	}
	if target == 0 {
		writeError(w, http.StatusBadRequest, "provide to_ms or by (e.g. \"30s\")")
		return
	}
	newClock, err := s.eng.Tick(target)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"clock_ms": newClock})
}

func (s *Server) createRule(w http.ResponseWriter, r *http.Request) {
	var req ruleRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	created, err := s.eng.CreateRule(req.toModel())
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, toDTO(created))
}

func (s *Server) updateRule(w http.ResponseWriter, r *http.Request) {
	var req ruleRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	req.ID = r.PathValue("id")
	updated, err := s.eng.UpdateRule(req.toModel())
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, toDTO(updated))
}

func (s *Server) deleteRule(w http.ResponseWriter, r *http.Request) {
	if err := s.eng.DeleteRule(r.PathValue("id")); err != nil {
		writeError(w, http.StatusNotFound, err.Error())
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) listRules(w http.ResponseWriter, r *http.Request) {
	s.st.Lock()
	rs := s.st.ListRulesLocked()
	s.st.Unlock()
	out := make([]ruleDTO, 0, len(rs))
	for _, rr := range rs {
		out = append(out, toDTO(rr))
	}
	writeJSON(w, http.StatusOK, map[string]any{"rules": out})
}

func (s *Server) getRule(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	s.st.Lock()
	rr, ok := s.st.GetRuleLocked(id)
	s.st.Unlock()
	if !ok {
		writeError(w, http.StatusNotFound, "rule not found")
		return
	}
	writeJSON(w, http.StatusOK, toDTO(rr))
}

func (s *Server) getState(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	s.st.Lock()
	st, ok := s.st.GetStateLocked(id)
	s.st.Unlock()
	if !ok {
		writeError(w, http.StatusNotFound, "state not found")
		return
	}
	writeJSON(w, http.StatusOK, st)
}

func (s *Server) listStates(w http.ResponseWriter, r *http.Request) {
	s.st.Lock()
	states := s.st.ListStatesLocked()
	s.st.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{"states": states, "clock_ms": s.eng.Clock()})
}

func (s *Server) ingest(w http.ResponseWriter, r *http.Request) {
	var req ingestRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	res, err := s.eng.Ingest(req.Samples)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusAccepted, res)
}

func (s *Server) listSamples(w http.ResponseWriter, r *http.Request) {
	metric := r.URL.Query().Get("metric")
	if metric == "" {
		writeError(w, http.StatusBadRequest, "metric query parameter is required")
		return
	}
	from, _ := strconv.ParseInt(r.URL.Query().Get("from_ms"), 10, 64)
	to, _ := strconv.ParseInt(r.URL.Query().Get("to_ms"), 10, 64)
	samples := s.st.ListSamples(metric, from, to)
	writeJSON(w, http.StatusOK, map[string]any{"metric": metric, "samples": samples})
}

func (s *Server) listEvents(w http.ResponseWriter, r *http.Request) {
	after, _ := strconv.ParseInt(r.URL.Query().Get("after_id"), 10, 64)
	rule := r.URL.Query().Get("rule_id")
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	events := s.st.ListEvents(after, rule, limit)
	writeJSON(w, http.StatusOK, map[string]any{"events": events})
}

// logging is a minimal request-logging middleware.
func logging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		next.ServeHTTP(w, r)
	})
}

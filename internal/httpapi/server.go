// Package httpapi exposes the hysteresis engine over HTTP: sample intake,
// rule management, a virtual-clock tick endpoint and state/event/sample
// queries.
package httpapi

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"strconv"
	"time"

	"github.com/example/hysteresis-alerter/internal/engine"
)

// Server wires the engine to HTTP routes.
type Server struct {
	eng *engine.Engine
	mux *http.ServeMux
}

// New builds the HTTP handler around eng.
func New(eng *engine.Engine) http.Handler {
	s := &Server{eng: eng, mux: http.NewServeMux()}
	s.routes()
	return s.mux
}

func (s *Server) routes() {
	s.mux.HandleFunc("GET /health", s.handleHealth)
	s.mux.HandleFunc("GET /clock", s.handleGetClock)
	s.mux.HandleFunc("POST /clock/tick", s.handleTick)
	s.mux.HandleFunc("POST /clock/tick-to", s.handleTickTo)

	s.mux.HandleFunc("POST /rules", s.handleCreateRule)
	s.mux.HandleFunc("GET /rules", s.handleListRules)
	s.mux.HandleFunc("GET /rules/{id}", s.handleGetRule)
	s.mux.HandleFunc("PUT /rules/{id}", s.handleUpdateRule)
	s.mux.HandleFunc("DELETE /rules/{id}", s.handleDeleteRule)

	s.mux.HandleFunc("POST /samples", s.handleIngest)
	s.mux.HandleFunc("GET /samples/{metric}", s.handleQuerySamples)

	s.mux.HandleFunc("GET /states", s.handleListStates)
	s.mux.HandleFunc("GET /events", s.handleQueryEvents)
}

// ---------- envelopes ----------

type envelope struct {
	OK    bool        `json:"ok"`
	Data  interface{} `json:"data,omitempty"`
	Error *errBody    `json:"error,omitempty"`
}

type errBody struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("write response: %v", err)
	}
}

func writeData(w http.ResponseWriter, v interface{}) {
	writeJSON(w, http.StatusOK, envelope{OK: true, Data: v})
}

func writeErr(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, envelope{OK: false, Error: &errBody{Code: code, Message: msg}})
}

// badJSON covers all 400 cases from malformed client input.
func writeBadRequest(w http.ResponseWriter, err error) {
	writeErr(w, http.StatusBadRequest, "bad_request", err.Error())
}

func writeNotFound(w http.ResponseWriter, msg string) {
	writeErr(w, http.StatusNotFound, "not_found", msg)
}

// ---------- DTOs ----------

type ruleReq struct {
	ID                string          `json:"id"`
	Metric            string          `json:"metric"`
	Operator          engine.Operator `json:"operator"`
	Threshold         float64         `json:"threshold"`
	TriggerFor        engine.Duration `json:"trigger_for"`
	RecoverFor        engine.Duration `json:"recover_for"`
	NoDataFor         engine.Duration `json:"no_data_for"`
	RecoveryThreshold *float64        `json:"recovery_threshold"`
}

func (r ruleReq) toRule() engine.Rule {
	out := engine.Rule{
		ID:         r.ID,
		Metric:     r.Metric,
		Operator:   r.Operator,
		Threshold:  r.Threshold,
		TriggerFor: r.TriggerFor,
		RecoverFor: r.RecoverFor,
		NoDataFor:  r.NoDataFor,
	}
	if r.RecoveryThreshold != nil {
		out.RecoveryThreshold = *r.RecoveryThreshold
		out.HasRecovery = true
	}
	return out
}

type stateView struct {
	RuleID       string       `json:"rule_id"`
	Metric       string       `json:"metric"`
	State        engine.State `json:"state"`
	HotSince     *time.Time   `json:"hot_since,omitempty"`
	ColdSince    *time.Time   `json:"cold_since,omitempty"`
	LastSampleTS *time.Time   `json:"last_sample_ts,omitempty"`
	LastValue    *float64     `json:"last_value,omitempty"`
	EnteredAt    time.Time    `json:"entered_at"`
	UpdatedAt    time.Time    `json:"updated_at"`
	InStateFor   string       `json:"in_state_for"`
	Version      int          `json:"version"`
}

func newStateView(rs engine.RuleSnapshot, now time.Time) stateView {
	st := rs.State
	return stateView{
		RuleID:       rs.Rule.ID,
		Metric:       rs.Rule.Metric,
		State:        st.State,
		HotSince:     st.HotSince,
		ColdSince:    st.ColdSince,
		LastSampleTS: st.LastSampleTS,
		LastValue:    st.LastValue,
		EnteredAt:    st.EnteredAt,
		UpdatedAt:    st.UpdatedAt,
		InStateFor:   now.Sub(st.EnteredAt).String(),
		Version:      rs.Rule.Version,
	}
}

type sampleReq struct {
	Metric string       `json:"metric"`
	TS     engine.VTime `json:"ts"`
	Value  float64      `json:"value"`
}

type ingestReq struct {
	Samples []sampleReq `json:"samples"`
}

type tickReq struct {
	Duration engine.Duration `json:"duration"`
	To       engine.VTime    `json:"to"`
	HasTo    bool            `json:"-"`
}

// ---------- handlers: meta ----------

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeData(w, map[string]interface{}{
		"status":    "ok",
		"clock_now": s.eng.Now().UTC().Format(time.RFC3339Nano),
	})
}

func (s *Server) handleGetClock(w http.ResponseWriter, r *http.Request) {
	writeData(w, map[string]interface{}{
		"now": s.eng.Now().UTC().Format(time.RFC3339Nano),
	})
}

// ---------- handlers: clock ----------

func (s *Server) handleTick(w http.ResponseWriter, r *http.Request) {
	var req tickReq
	if r.ContentLength != 0 {
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeBadRequest(w, err)
			return
		}
	}
	if req.Duration.Duration <= 0 {
		writeBadRequest(w, errors.New(`field "duration" must be a positive duration, e.g. "30s"`))
		return
	}
	evs, err := s.eng.Tick(req.Duration.Duration)
	if err != nil {
		writeBadRequest(w, err)
		return
	}
	writeData(w, map[string]interface{}{
		"clock_now": s.eng.Now().UTC().Format(time.RFC3339Nano),
		"events":    eventsOrEmpty(evs),
	})
}

func (s *Server) handleTickTo(w http.ResponseWriter, r *http.Request) {
	var body struct {
		To engine.VTime `json:"to"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeBadRequest(w, err)
		return
	}
	if body.To.Time.IsZero() {
		writeBadRequest(w, errors.New(`field "to" must be an RFC3339 timestamp or Unix seconds`))
		return
	}
	evs, err := s.eng.TickTo(body.To.Time)
	if err != nil {
		writeBadRequest(w, err)
		return
	}
	writeData(w, map[string]interface{}{
		"clock_now": s.eng.Now().UTC().Format(time.RFC3339Nano),
		"events":    eventsOrEmpty(evs),
	})
}

func eventsOrEmpty(evs []engine.Event) []engine.Event {
	if evs == nil {
		return []engine.Event{}
	}
	return evs
}

// ---------- handlers: rules ----------

func (s *Server) handleCreateRule(w http.ResponseWriter, r *http.Request) {
	var req ruleReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeBadRequest(w, err)
		return
	}
	rule := req.toRule()
	if err := s.eng.CreateRule(rule); err != nil {
		writeBadRequest(w, err)
		return
	}
	rs, _ := s.eng.GetRule(rule.ID)
	writeJSON(w, http.StatusCreated, envelope{OK: true, Data: ruleView(rs)})
}

func (s *Server) handleListRules(w http.ResponseWriter, r *http.Request) {
	all := s.eng.ListRules()
	views := make([]map[string]interface{}, 0, len(all))
	now := s.eng.Now()
	for _, rs := range all {
		views = append(views, map[string]interface{}{
			"rule":  ruleView(rs),
			"state": newStateView(rs, now),
		})
	}
	writeData(w, views)
}

func (s *Server) handleGetRule(w http.ResponseWriter, r *http.Request) {
	rs, ok := s.eng.GetRule(r.PathValue("id"))
	if !ok {
		writeNotFound(w, "rule not found")
		return
	}
	writeData(w, map[string]interface{}{
		"rule":  ruleView(rs),
		"state": newStateView(rs, s.eng.Now()),
	})
}

func (s *Server) handleUpdateRule(w http.ResponseWriter, r *http.Request) {
	var req ruleReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeBadRequest(w, err)
		return
	}
	rule := req.toRule()
	rule.ID = r.PathValue("id") // path is authoritative
	if err := s.eng.UpdateRule(rule); err != nil {
		// Distinguish 404 from validation failures.
		if rs, _ := s.eng.GetRule(rule.ID); rs.Rule.ID == "" {
			writeNotFound(w, err.Error())
			return
		}
		writeBadRequest(w, err)
		return
	}
	rs, _ := s.eng.GetRule(rule.ID)
	writeData(w, map[string]interface{}{
		"rule":  ruleView(rs),
		"state": newStateView(rs, s.eng.Now()),
	})
}

func (s *Server) handleDeleteRule(w http.ResponseWriter, r *http.Request) {
	if err := s.eng.DeleteRule(r.PathValue("id")); err != nil {
		writeNotFound(w, err.Error())
		return
	}
	writeData(w, map[string]interface{}{"deleted": r.PathValue("id")})
}

func ruleView(rs engine.RuleSnapshot) engine.Rule { return rs.Rule }

// ---------- handlers: samples ----------

func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	var req ingestReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeBadRequest(w, err)
		return
	}
	if len(req.Samples) == 0 {
		writeBadRequest(w, errors.New(`field "samples" must contain at least one item`))
		return
	}
	for i, sm := range req.Samples {
		if sm.Metric == "" {
			writeBadRequest(w, errors.New("samples["+strconv.Itoa(i)+"].metric is required"))
			return
		}
	}
	items := make([]engine.IngestItem, len(req.Samples))
	for i, sm := range req.Samples {
		items[i] = engine.IngestItem{Metric: sm.Metric, TS: sm.TS.Time, Value: sm.Value}
	}
	results := s.eng.Ingest(items)
	writeData(w, map[string]interface{}{
		"clock_now": s.eng.Now().UTC().Format(time.RFC3339Nano),
		"results":   results,
	})
}

func (s *Server) handleQuerySamples(w http.ResponseWriter, r *http.Request) {
	metric := r.PathValue("metric")
	var from, to *time.Time
	if v := r.URL.Query().Get("from"); v != "" {
		t, err := parseQueryTime(v)
		if err != nil {
			writeBadRequest(w, err)
			return
		}
		from = &t
	}
	if v := r.URL.Query().Get("to"); v != "" {
		t, err := parseQueryTime(v)
		if err != nil {
			writeBadRequest(w, err)
			return
		}
		to = &t
	}
	limit := 0
	if v := r.URL.Query().Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			writeBadRequest(w, errors.New("limit must be a non-negative integer"))
			return
		}
		limit = n
	}
	samples := s.eng.QuerySamples(metric, from, to, limit)
	writeData(w, map[string]interface{}{"metric": metric, "samples": samples})
}

// ---------- handlers: states / events ----------

func (s *Server) handleListStates(w http.ResponseWriter, r *http.Request) {
	all := s.eng.ListRules()
	now := s.eng.Now()
	views := make([]stateView, 0, len(all))
	for _, rs := range all {
		views = append(views, newStateView(rs, now))
	}
	writeData(w, views)
}

func (s *Server) handleQueryEvents(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	ruleID := q.Get("rule_id")
	notifOnly := q.Get("notifications_only") != "false" && q.Get("all") != "true"
	var since *time.Time
	if v := q.Get("since"); v != "" {
		t, err := parseQueryTime(v)
		if err != nil {
			writeBadRequest(w, err)
			return
		}
		since = &t
	}
	evs := s.eng.QueryEvents(ruleID, notifOnly, since)
	writeData(w, map[string]interface{}{"events": evs})
}

func parseQueryTime(v string) (time.Time, error) {
	if t, err := time.Parse(time.RFC3339Nano, v); err == nil {
		return t.UTC(), nil
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil {
		return time.Time{}, errors.New("time must be RFC3339 or Unix seconds: " + v)
	}
	return time.Unix(n, 0).UTC(), nil
}

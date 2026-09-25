// Package httpapi exposes the scheduler over a small local HTTP API,
// including a Server-Sent Events endpoint that streams structured events.
package httpapi

import (
	"encoding/json"
	"net/http"

	"pilab/scenario"
	"pilab/scheduler"
)

// Server wires the HTTP routes.
type Server struct {
	mux *http.ServeMux
}

// NewServer constructs the API handler.
func NewServer() *Server {
	s := &Server{mux: http.NewServeMux()}
	s.routes()
	return s
}

func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("/healthz", s.health)
	s.mux.HandleFunc("/api/scenarios", s.listScenarios)
	s.mux.HandleFunc("/api/scenarios/", s.scenarioByName)
	s.mux.HandleFunc("/api/simulate", s.simulate)
	s.mux.HandleFunc("/api/compare/inversion", s.compareInversion)
	s.mux.HandleFunc("/api/simulate/stream", s.simulateStream)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeErr(w, http.StatusMethodNotAllowed, "GET required")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) listScenarios(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeErr(w, http.StatusMethodNotAllowed, "GET required")
		return
	}
	writeJSON(w, http.StatusOK, scenario.All())
}

func (s *Server) scenarioByName(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodPost {
		writeErr(w, http.StatusMethodNotAllowed, "GET or POST required")
		return
	}
	id := r.URL.Path[len("/api/scenarios/"):]
	if id == "" {
		writeErr(w, http.StatusBadRequest, "scenario id required")
		return
	}
	sc, ok := scenario.Get(scenario.ID(id))
	if !ok {
		writeErr(w, http.StatusNotFound, "unknown scenario: "+id)
		return
	}
	if r.Method == http.MethodGet {
		writeJSON(w, http.StatusOK, sc)
		return
	}
	rep, err := scenario.Run(sc)
	if err != nil {
		writeErr(w, http.StatusUnprocessableEntity, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

func decodeConfig(w http.ResponseWriter, r *http.Request) (scheduler.Config, bool) {
	var cfg scheduler.Config
	if r.Body == nil {
		writeErr(w, http.StatusBadRequest, "request body required")
		return cfg, false
	}
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&cfg); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return cfg, false
	}
	return cfg, true
}

func (s *Server) simulate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeErr(w, http.StatusMethodNotAllowed, "POST required")
		return
	}
	cfg, ok := decodeConfig(w, r)
	if !ok {
		return
	}
	sched, err := scheduler.New(cfg)
	if err != nil {
		writeErr(w, http.StatusUnprocessableEntity, err.Error())
		return
	}
	rep := sched.Run()
	writeJSON(w, http.StatusOK, rep)
}

func (s *Server) simulateStream(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeErr(w, http.StatusMethodNotAllowed, "POST required")
		return
	}
	cfg, ok := decodeConfig(w, r)
	if !ok {
		return
	}

	flusher, ok := w.(http.Flusher)
	if !ok {
		writeErr(w, http.StatusInternalServerError, "streaming unsupported")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.WriteHeader(http.StatusOK)

	send := func(event string, v any) {
		data, _ := json.Marshal(v)
		_, _ = w.Write([]byte("event: " + event + "\ndata: " + string(data) + "\n\n"))
		flusher.Flush()
	}

	sched, err := scheduler.New(cfg, scheduler.WithEventSink(func(e scheduler.Event) {
		send("event", e)
	}))
	if err != nil {
		send("error", map[string]string{"error": err.Error()})
		return
	}
	rep := sched.Run()
	send("report", rep)
}

func (s *Server) compareInversion(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeErr(w, http.StatusMethodNotAllowed, "GET required")
		return
	}
	cmp, err := scenario.CompareInversion()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, cmp)
}

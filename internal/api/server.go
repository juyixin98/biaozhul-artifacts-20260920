// Package api exposes the trigger engine over HTTP using only net/http.
package api

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"tztrig/internal/engine"
	"tztrig/internal/zoneinfo"
)

// Server wires the engine to HTTP routes.
type Server struct {
	eng *engine.Engine
}

// New builds the application handler.
func New(eng *engine.Engine) http.Handler {
	s := &Server{eng: eng}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/health", s.health)
	mux.HandleFunc("GET /api/info", s.info)
	mux.HandleFunc("GET /api/zones", s.listZones)
	mux.HandleFunc("POST /api/schedules", s.createSchedule)
	mux.HandleFunc("GET /api/schedules", s.listSchedules)
	mux.HandleFunc("GET /api/schedules/{id}", s.getSchedule)
	mux.HandleFunc("DELETE /api/schedules/{id}", s.deleteSchedule)
	mux.HandleFunc("POST /api/schedules/{id}/pause", s.pauseSchedule)
	mux.HandleFunc("POST /api/schedules/{id}/resume", s.resumeSchedule)
	mux.HandleFunc("GET /api/schedules/{id}/next", s.nextFires)
	mux.HandleFunc("GET /api/fires", s.listFires)
	mux.HandleFunc("GET /api/skips", s.listSkips)
	return logging(mux)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "time": time.Now().UTC().Format(time.RFC3339)})
}

func (s *Server) info(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"tzdataVersion": zoneinfo.TZDataVersion,
		"catchUpLimit":  s.eng.CatchUpLimit(),
		"scheduleCount": len(s.eng.List()),
	})
}

func (s *Server) listZones(w http.ResponseWriter, r *http.Request) {
	prefix := r.URL.Query().Get("prefix")
	names, err := zoneinfo.Names()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	if prefix == "" {
		writeJSON(w, http.StatusOK, map[string][]string{"zones": names})
		return
	}
	var filtered []string
	for _, n := range names {
		if strings.HasPrefix(n, prefix) {
			filtered = append(filtered, n)
		}
	}
	writeJSON(w, http.StatusOK, map[string][]string{"zones": filtered})
}

func (s *Server) createSchedule(w http.ResponseWriter, r *http.Request) {
	var in engine.CreateInput
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	st, err := s.eng.CreateSchedule(in)
	if err != nil {
		s.handleEngineErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, st)
}

func (s *Server) listSchedules(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"schedules": s.eng.List()})
}

func (s *Server) getSchedule(w http.ResponseWriter, r *http.Request) {
	st, err := s.eng.Get(r.PathValue("id"))
	if err != nil {
		s.handleEngineErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, st)
}

func (s *Server) deleteSchedule(w http.ResponseWriter, r *http.Request) {
	if err := s.eng.DeleteSchedule(r.PathValue("id")); err != nil {
		s.handleEngineErr(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) pauseSchedule(w http.ResponseWriter, r *http.Request) {
	s.setEnabled(w, r, false)
}

func (s *Server) resumeSchedule(w http.ResponseWriter, r *http.Request) {
	s.setEnabled(w, r, true)
}

func (s *Server) setEnabled(w http.ResponseWriter, r *http.Request, enabled bool) {
	if err := s.eng.SetEnabled(r.PathValue("id"), enabled); err != nil {
		s.handleEngineErr(w, err)
		return
	}
	st, _ := s.eng.Get(r.PathValue("id"))
	writeJSON(w, http.StatusOK, st)
}

func (s *Server) nextFires(w http.ResponseWriter, r *http.Request) {
	n := 3
	if v := r.URL.Query().Get("n"); v != "" {
		parsed, err := strconv.Atoi(v)
		if err != nil || parsed < 1 || parsed > 100 {
			writeErr(w, http.StatusBadRequest, "n must be 1-100")
			return
		}
		n = parsed
	}
	times, err := s.eng.NextFires(r.PathValue("id"), n)
	if err != nil {
		s.handleEngineErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"next": times})
}

func (s *Server) listFires(w http.ResponseWriter, r *http.Request) {
	limit := 50
	if v := r.URL.Query().Get("limit"); v != "" {
		parsed, err := strconv.Atoi(v)
		if err != nil || parsed < 1 || parsed > 500 {
			writeErr(w, http.StatusBadRequest, "limit must be 1-500")
			return
		}
		limit = parsed
	}
	writeJSON(w, http.StatusOK, map[string]any{"fires": s.eng.History(limit)})
}

func (s *Server) listSkips(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"skipped": s.eng.LastSkips()})
}

func (s *Server) handleEngineErr(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, engine.ErrNotFound):
		writeErr(w, http.StatusNotFound, err.Error())
	default:
		writeErr(w, http.StatusBadRequest, err.Error())
	}
}

// logging is a minimal request logger using the standard logger.
func logging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rw, r)
		log.Printf("%s %s -> %d (%s)", r.Method, r.URL.RequestURI(), rw.status, time.Since(start).Round(time.Millisecond))
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

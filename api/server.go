// Package api wires the gang scheduler to an HTTP API using only net/http.
package api

import (
	"encoding/json"
	"errors"
	"net/http"

	"gangscheduler/gang"
)

// Server holds the dependencies of the HTTP handlers.
type Server struct {
	Sched *gang.Scheduler
}

// NewRouter registers every route on the given mux (Go 1.22+ method+pattern
// routing) and returns it.
func (s *Server) NewRouter() *http.ServeMux {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /healthz", s.health)
	mux.HandleFunc("GET /state", s.getState)

	mux.HandleFunc("POST /nodes", s.addNode)
	mux.HandleFunc("GET /nodes/{id}", s.getNode)
	mux.HandleFunc("POST /nodes/{id}/state", s.setNodeState)

	mux.HandleFunc("POST /gangs", s.submitGang)
	mux.HandleFunc("GET /gangs/{id}", s.getGang)
	mux.HandleFunc("POST /gangs/{id}/retry", s.retryReserve)
	mux.HandleFunc("POST /gangs/{id}/complete", s.completeGang)
	mux.HandleFunc("POST /gangs/{id}/plans/{plan}/commit", s.commitPlan)
	mux.HandleFunc("POST /gangs/{id}/plans/{plan}/release", s.releasePlan)

	return mux
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type nodeReq struct {
	ID       string            `json:"id"`
	Zone     string            `json:"zone"`
	Labels   map[string]string `json:"labels"`
	Capacity int               `json:"capacity"`
}

func (s *Server) addNode(w http.ResponseWriter, r *http.Request) {
	var req nodeReq
	if !decode(w, r, &req) {
		return
	}
	if err := s.Sched.AddNode(req.ID, req.Zone, req.Labels, req.Capacity); err != nil {
		writeError(w, err)
		return
	}
	v, err := s.Sched.GetNode(req.ID)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, v)
}

func (s *Server) getNode(w http.ResponseWriter, r *http.Request) {
	v, err := s.Sched.GetNode(r.PathValue("id"))
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

type nodeStateReq struct {
	Online bool `json:"online"`
}

func (s *Server) setNodeState(w http.ResponseWriter, r *http.Request) {
	var req nodeStateReq
	if !decode(w, r, &req) {
		return
	}
	if err := s.Sched.SetNodeOnline(r.PathValue("id"), req.Online); err != nil {
		writeError(w, err)
		return
	}
	v, err := s.Sched.GetNode(r.PathValue("id"))
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

// gangResponse wraps a gang with the plan produced by its latest reservation
// attempt (nil when still waiting).
type gangResponse struct {
	Gang gang.GangView  `json:"gang"`
	Plan *gang.PlanView `json:"plan"`
}

func (s *Server) submitGang(w http.ResponseWriter, r *http.Request) {
	var spec gang.GangSpec
	if !decode(w, r, &spec) {
		return
	}
	g, p, err := s.Sched.SubmitGang(spec)
	if err != nil {
		writeError(w, err)
		return
	}
	gv, pv, _ := s.Sched.GetGang(g.Spec.ID)
	_ = p
	writeJSON(w, http.StatusCreated, gangResponse{Gang: gv, Plan: pv})
}

func (s *Server) getGang(w http.ResponseWriter, r *http.Request) {
	gv, pv, err := s.Sched.GetGang(r.PathValue("id"))
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, gangResponse{Gang: gv, Plan: pv})
}

func (s *Server) retryReserve(w http.ResponseWriter, r *http.Request) {
	g, p, err := s.Sched.RetryReserve(r.PathValue("id"))
	if err != nil {
		writeError(w, err)
		return
	}
	gv, pv, _ := s.Sched.GetGang(g.Spec.ID)
	_ = p
	writeJSON(w, http.StatusOK, gangResponse{Gang: gv, Plan: pv})
}

type commitReq struct {
	Version int64 `json:"version"`
}

func (s *Server) commitPlan(w http.ResponseWriter, r *http.Request) {
	var req commitReq
	if !decode(w, r, &req) {
		return
	}
	gangID, planID := r.PathValue("id"), r.PathValue("plan")
	g, p, err := s.Sched.CommitPlan(gangID, planID, req.Version)
	if err != nil {
		writeError(w, err)
		return
	}
	gv, pv, _ := s.Sched.GetGang(g.Spec.ID)
	_ = p
	writeJSON(w, http.StatusOK, gangResponse{Gang: gv, Plan: pv})
}

func (s *Server) releasePlan(w http.ResponseWriter, r *http.Request) {
	g, err := s.Sched.ReleasePlan(r.PathValue("id"), r.PathValue("plan"))
	if err != nil {
		writeError(w, err)
		return
	}
	gv, pv, _ := s.Sched.GetGang(g.Spec.ID)
	writeJSON(w, http.StatusOK, gangResponse{Gang: gv, Plan: pv})
}

func (s *Server) completeGang(w http.ResponseWriter, r *http.Request) {
	g, err := s.Sched.CompleteGang(r.PathValue("id"))
	if err != nil {
		writeError(w, err)
		return
	}
	gv, _, _ := s.Sched.GetGang(g.Spec.ID)
	writeJSON(w, http.StatusOK, gangResponse{Gang: gv})
}

func (s *Server) getState(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.Sched.GetState())
}

// --- small JSON helpers ---

func decode(w http.ResponseWriter, r *http.Request, dst any) bool {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "invalid JSON body: " + err.Error(),
		})
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, err error) {
	status := http.StatusInternalServerError
	switch {
	case errors.Is(err, gang.ErrNotFound):
		status = http.StatusNotFound
	case errors.Is(err, gang.ErrInvalid):
		status = http.StatusBadRequest
	case errors.Is(err, gang.ErrConflict):
		status = http.StatusConflict
	case errors.Is(err, gang.ErrExpired):
		status = http.StatusGone
	}
	writeJSON(w, status, map[string]string{"error": err.Error()})
}

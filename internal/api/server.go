// Package api exposes the gang scheduler over net/http using only the
// standard library. All payloads are JSON.
package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/example/gangscheduler/internal/scheduler"
)

// Server wires the scheduler to HTTP routes.
type Server struct {
	sched *scheduler.Scheduler
	mux   *http.ServeMux
}

// NewServer builds the handler tree.
func NewServer(s *scheduler.Scheduler) *Server {
	srv := &Server{sched: s, mux: http.NewServeMux()}
	srv.routes()
	return srv
}

// Handler returns the root http.Handler.
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("GET /healthz", s.health)
	s.mux.HandleFunc("GET /v1/state", s.getState)

	s.mux.HandleFunc("POST /v1/nodes", s.addNode)
	s.mux.HandleFunc("GET /v1/nodes", s.listNodes)
	s.mux.HandleFunc("GET /v1/nodes/{name}", s.getNode)
	s.mux.HandleFunc("POST /v1/nodes/{name}/status", s.setNodeStatus)

	s.mux.HandleFunc("POST /v1/gangs", s.submitGang)
	s.mux.HandleFunc("GET /v1/gangs", s.listGangs)
	s.mux.HandleFunc("GET /v1/gangs/{id}", s.getGang)
	s.mux.HandleFunc("POST /v1/gangs/{id}/commit", s.commitGang)
	s.mux.HandleFunc("POST /v1/gangs/{id}/release", s.releaseGang)
	s.mux.HandleFunc("POST /v1/gangs/{id}/replan", s.replanGang)
	s.mux.HandleFunc("DELETE /v1/gangs/{id}", s.deleteGang)
}

// ---------------------------------------------------------------------------
// DTOs
// ---------------------------------------------------------------------------

type nodeReq struct {
	Name     string            `json:"name"`
	Labels   map[string]string `json:"labels"`
	Capacity int               `json:"capacity"`
}

type statusReq struct {
	Status string `json:"status"`
}

type taskReq struct {
	ID          string              `json:"id"`
	Slots       int                 `json:"slots"`
	MatchLabels map[string]string   `json:"match_labels"`
	InLabels    map[string][]string `json:"in_labels"`
}

type gangReq struct {
	ID            string    `json:"id"`
	Tasks         []taskReq `json:"tasks"`
	DistinctNodes bool      `json:"distinct_nodes"`
	TTLMillis     int64     `json:"ttl_ms"`
}

type commitReq struct {
	ExpectedNodeVersions map[string]int64 `json:"expected_node_versions"`
}

type errResp struct {
	Error errBody `json:"error"`
}

type errBody struct {
	Code   string `json:"code"`
	Reason string `json:"reason"`
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) addNode(w http.ResponseWriter, r *http.Request) {
	var req nodeReq
	if !decode(w, r, &req) {
		return
	}
	if err := s.sched.AddNode(req.Name, req.Labels, req.Capacity); err != nil {
		writeSchedulerError(w, err)
		return
	}
	for _, n := range s.sched.Snapshot().Nodes {
		if n.Name == req.Name {
			writeJSON(w, http.StatusCreated, n)
			return
		}
	}
	writeErr(w, http.StatusInternalServerError, "INTERNAL", "node vanished after insert")
}

func (s *Server) listNodes(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.sched.Snapshot().Nodes)
}

func (s *Server) getNode(w http.ResponseWriter, r *http.Request) {
	for _, n := range s.sched.Snapshot().Nodes {
		if n.Name == r.PathValue("name") {
			writeJSON(w, http.StatusOK, n)
			return
		}
	}
	writeErr(w, http.StatusNotFound, "NOT_FOUND", "node not found")
}

func (s *Server) setNodeStatus(w http.ResponseWriter, r *http.Request) {
	var req statusReq
	if !decode(w, r, &req) {
		return
	}
	if err := s.sched.SetNodeStatus(r.PathValue("name"), strings.ToUpper(req.Status)); err != nil {
		writeSchedulerError(w, err)
		return
	}
	for _, n := range s.sched.Snapshot().Nodes {
		if n.Name == r.PathValue("name") {
			writeJSON(w, http.StatusOK, n)
			return
		}
	}
	writeErr(w, http.StatusNotFound, "NOT_FOUND", "node not found")
}

func (s *Server) submitGang(w http.ResponseWriter, r *http.Request) {
	var req gangReq
	if !decode(w, r, &req) {
		return
	}
	tasks := make([]scheduler.TaskSpec, len(req.Tasks))
	for i, t := range req.Tasks {
		tasks[i] = scheduler.TaskSpec{
			ID:    t.ID,
			Slots: t.Slots,
			Match: t.MatchLabels,
			In:    t.InLabels,
		}
	}
	view, err := s.sched.SubmitGang(
		req.ID, tasks, req.DistinctNodes, time.Duration(req.TTLMillis)*time.Millisecond,
	)
	if err != nil {
		writeSchedulerError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, view)
}

func (s *Server) listGangs(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.sched.Snapshot().Gangs)
}

func (s *Server) getGang(w http.ResponseWriter, r *http.Request) {
	view, err := s.sched.GetGang(r.PathValue("id"))
	if err != nil {
		writeSchedulerError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) commitGang(w http.ResponseWriter, r *http.Request) {
	var req commitReq
	if !decode(w, r, &req) {
		return
	}
	if req.ExpectedNodeVersions == nil {
		req.ExpectedNodeVersions = map[string]int64{}
	}
	view, err := s.sched.Commit(r.PathValue("id"), req.ExpectedNodeVersions)
	if err != nil {
		writeSchedulerError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) releaseGang(w http.ResponseWriter, r *http.Request) {
	view, err := s.sched.ReleaseGang(r.PathValue("id"))
	if err != nil {
		writeSchedulerError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) replanGang(w http.ResponseWriter, r *http.Request) {
	view, err := s.sched.Replan(r.PathValue("id"))
	if err != nil {
		writeSchedulerError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) deleteGang(w http.ResponseWriter, r *http.Request) {
	if err := s.sched.DeleteGang(r.PathValue("id")); err != nil {
		writeSchedulerError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) getState(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.sched.Snapshot())
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func findNode(nodes []scheduler.NodeView, name string) scheduler.NodeView {
	for _, n := range nodes {
		if n.Name == name {
			return n
		}
	}
	return scheduler.NodeView{}
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		writeErr(w, http.StatusBadRequest, "BAD_REQUEST", "invalid JSON: "+err.Error())
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code, reason string) {
	writeJSON(w, status, errResp{Error: errBody{Code: code, Reason: reason}})
}

func writeSchedulerError(w http.ResponseWriter, err error) {
	var ve *scheduler.ValidationError
	if errors.As(err, &ve) {
		writeErr(w, http.StatusBadRequest, "BAD_REQUEST", ve.Error())
		return
	}
	var ce *scheduler.ConflictError
	if errors.As(err, &ce) {
		status := http.StatusConflict
		if ce.Code == "STATE" {
			status = http.StatusConflict
		}
		writeErr(w, status, ce.Code, ce.Reason)
		return
	}
	switch {
	case errors.Is(err, scheduler.ErrNotFound):
		writeErr(w, http.StatusNotFound, "NOT_FOUND", err.Error())
	case errors.Is(err, scheduler.ErrAlreadyExists):
		writeErr(w, http.StatusConflict, "ALREADY_EXISTS", err.Error())
	case errors.Is(err, scheduler.ErrInvalidState):
		writeErr(w, http.StatusConflict, "STATE", err.Error())
	default:
		writeErr(w, http.StatusInternalServerError, "INTERNAL", err.Error())
	}
}

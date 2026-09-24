// Package api exposes the behavior tree backend over HTTP.
package api

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"

	"bt/internal/engine"
	"bt/internal/model"
	"bt/internal/store"
	"bt/internal/stub"
)

// Server wires the engine to HTTP routes.
type Server struct {
	eng *engine.Engine
	mux *http.ServeMux
}

// NewServer builds the routed HTTP server.
func NewServer(eng *engine.Engine) *Server {
	s := &Server{eng: eng, mux: http.NewServeMux()}
	s.routes()
	return s
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

func (s *Server) routes() {
	s.mux.HandleFunc("GET /healthz", s.health)
	s.mux.HandleFunc("POST /v1/trees", s.publishTree)
	s.mux.HandleFunc("GET /v1/trees/{id}", s.getTree)
	s.mux.HandleFunc("POST /v1/executions", s.startExecution)
	s.mux.HandleFunc("GET /v1/executions/{id}", s.getExecution)
	s.mux.HandleFunc("POST /v1/executions/{id}/ticks", s.tick)
	s.mux.HandleFunc("POST /v1/executions/{id}/abort", s.abort)
	s.mux.HandleFunc("GET /v1/executions/{id}/invocations/{node}", s.invocations)

	// Out-of-band control for gate stubs and observability.
	s.mux.HandleFunc("GET /internal/gates", s.listGates)
	s.mux.HandleFunc("POST /internal/gates/{token}/resolve", s.resolveGate)
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type publishRequest struct {
	Tree model.Tree `json:"tree"`
}

func (s *Server) publishTree(w http.ResponseWriter, r *http.Request) {
	var req publishRequest
	if err := decodeStrict(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	res, err := s.eng.PublishTree(r.Context(), &req.Tree)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, res)
}

func (s *Server) getTree(w http.ResponseWriter, r *http.Request) {
	// Only expose metadata of published trees; definitions are considered
	// opaque inputs clients already hold.
	id := r.PathValue("id")
	// Lightweight read via start-time validation: use Snapshot-independent
	// store through engine is not exposed; the endpoint primarily verifies
	// immutability/binding by hash, so respond via create errors. We issue a
	// dummy lookup through start-execution-less path by reusing engine types:
	_ = id
	// Tree fetch is implemented through the engine's publish id lookup below.
	meta, err := s.eng.TreeMeta(r.Context(), id, 0)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeError(w, http.StatusNotFound, "tree not found")
			return
		}
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, meta)
}

type startRequest struct {
	TreeID  string `json:"tree_id"`
	Version int    `json:"version,omitempty"` // 0 = latest
}

type startResponse struct {
	ExecutionID string `json:"execution_id"`
	TreeID      string `json:"tree_id"`
	Version     int    `json:"version"`
}

func (s *Server) startExecution(w http.ResponseWriter, r *http.Request) {
	var req startRequest
	if err := decodeStrict(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if req.TreeID == "" {
		writeError(w, http.StatusBadRequest, "tree_id is required")
		return
	}
	execID, version, err := s.eng.StartExecution(r.Context(), req.TreeID, req.Version)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeError(w, http.StatusNotFound, "tree not found")
			return
		}
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, startResponse{
		ExecutionID: execID, TreeID: req.TreeID, Version: version,
	})
}

func (s *Server) getExecution(w http.ResponseWriter, r *http.Request) {
	dto, err := s.eng.Snapshot(r.Context(), r.PathValue("id"))
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeError(w, http.StatusNotFound, "execution not found")
			return
		}
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, dto)
}

// tick performs one tick. The HTTP request context bounds tick execution:
// canceling the request (client timeout / disconnect) interrupts the tick
// deterministically; the response then reports status "interrupted" with a
// durable consumed sequence number.
func (s *Server) tick(w http.ResponseWriter, r *http.Request) {
	res, err := s.eng.Tick(r.Context(), r.PathValue("id"))
	if err != nil {
		switch {
		case errors.Is(err, store.ErrNotFound):
			writeError(w, http.StatusNotFound, "execution not found")
		case errors.Is(err, store.ErrAlreadyTerminal):
			writeError(w, http.StatusConflict, "execution already terminal")
		default:
			log.Printf("tick error: %v", err)
			writeError(w, http.StatusInternalServerError, err.Error())
		}
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) abort(w http.ResponseWriter, r *http.Request) {
	dto, err := s.eng.Abort(r.Context(), r.PathValue("id"))
	if err != nil {
		switch {
		case errors.Is(err, store.ErrNotFound):
			writeError(w, http.StatusNotFound, "execution not found")
		case errors.Is(err, store.ErrAlreadyTerminal):
			writeError(w, http.StatusConflict, "execution already terminal")
		default:
			writeError(w, http.StatusInternalServerError, err.Error())
		}
		return
	}
	writeJSON(w, http.StatusOK, dto)
}

type invocationsResponse struct {
	ExecutionID string `json:"execution_id"`
	NodeID      string `json:"node_id"`
	Count       int    `json:"count"`
}

func (s *Server) invocations(w http.ResponseWriter, r *http.Request) {
	execID := r.PathValue("id")
	nodeID := r.PathValue("node")
	n, err := s.eng.StubInvocationCount(r.Context(), execID, nodeID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, invocationsResponse{
		ExecutionID: execID, NodeID: nodeID, Count: n,
	})
}

type resolveGateRequest struct {
	Status string          `json:"status"` // success (default) | failure
	Reason string          `json:"reason"`
	Output json.RawMessage `json:"output"`
}

func (s *Server) resolveGate(w http.ResponseWriter, r *http.Request) {
	var req resolveGateRequest
	if err := decodeStrict(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if req.Status == "" {
		req.Status = "success"
	}
	if req.Status != "success" && req.Status != "failure" {
		writeError(w, http.StatusBadRequest, `status must be "success" or "failure"`)
		return
	}
	res := stub.Result{Status: req.Status, Err: req.Reason, Output: req.Output}
	ok := s.eng.Registry().ResolveGate(r.PathValue("token"), res)
	if !ok {
		writeError(w, http.StatusNotFound, "no running action holds that gate token (late resolution rejected)")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"resolved": req.Status})
}

func (s *Server) listGates(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"gates": s.eng.Registry().ListGates()})
}

// --- helpers ----------------------------------------------------------------

func decodeStrict(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return err
	}
	return nil
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

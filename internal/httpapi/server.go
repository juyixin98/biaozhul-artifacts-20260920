// Package httpapi exposes the migration simulator over net/http using only
// the standard library.
package httpapi

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"

	"shardmigrator/internal/cluster"
)

// Server wires the cluster to HTTP routes.
type Server struct {
	cl  *cluster.Cluster
	mux *http.ServeMux
}

// NewServer builds a server with routes registered.
func NewServer(cl *cluster.Cluster) *Server {
	s := &Server{cl: cl, mux: http.NewServeMux()}
	s.routes()
	return s
}

// Handler returns the root http.Handler (with panic recovery).
func (s *Server) Handler() http.Handler { return recoverer(s.mux) }

func (s *Server) routes() {
	s.mux.HandleFunc("GET /healthz", s.healthz)
	s.mux.HandleFunc("GET /state", s.state)
	s.mux.HandleFunc("GET /shards/{id}", s.getShard)
	s.mux.HandleFunc("GET /shards/{id}/writes", s.listWrites)
	s.mux.HandleFunc("GET /shards/{id}/audit", s.audit)
	s.mux.HandleFunc("POST /shards/{id}/writes", s.clientWrite)
	s.mux.HandleFunc("POST /shards/{id}/migration/start", s.migrationStart)
	s.mux.HandleFunc("POST /shards/{id}/migration/snapshot-complete", s.snapshotComplete)
	s.mux.HandleFunc("POST /shards/{id}/migration/catchup", s.catchup)
	s.mux.HandleFunc("POST /shards/{id}/migration/switch", s.switchRoute)
	s.mux.HandleFunc("POST /shards/{id}/reset", s.resetShard)
	s.mux.HandleFunc("POST /nodes/{id}/disconnect", s.disconnect)
	s.mux.HandleFunc("POST /nodes/{id}/reconnect", s.reconnect)
}

// ---- envelope -------------------------------------------------------------

type envelope struct {
	OK     bool   `json:"ok"`
	Data   any    `json:"data,omitempty"`
	Replay bool   `json:"idempotent_replay,omitempty"`
	Code   string `json:"error_code,omitempty"`
	Error  string `json:"error,omitempty"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeData(w http.ResponseWriter, status int, data any, replay bool) {
	writeJSON(w, status, envelope{OK: true, Data: data, Replay: replay})
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, envelope{OK: false, Code: code, Error: msg})
}

// errorStatus maps cluster sentinel errors to HTTP status + stable code.
func errorStatus(err error) (int, string) {
	switch {
	case errors.Is(err, cluster.ErrShardNotFound), errors.Is(err, cluster.ErrNodeNotFound):
		return http.StatusNotFound, "not_found"
	case errors.Is(err, cluster.ErrStaleRoute):
		return http.StatusConflict, "stale_route"
	case errors.Is(err, cluster.ErrPrimaryDisconnected):
		return http.StatusServiceUnavailable, "primary_unavailable"
	case errors.Is(err, cluster.ErrTargetDisconnected):
		return http.StatusConflict, "target_disconnected"
	case errors.Is(err, cluster.ErrLagRemaining):
		return http.StatusConflict, "lag_remaining"
	case errors.Is(err, cluster.ErrWrongPhase):
		return http.StatusConflict, "wrong_phase"
	case errors.Is(err, cluster.ErrTargetIsPrimary):
		return http.StatusConflict, "target_is_primary"
	case errors.Is(err, cluster.ErrIdemConflict):
		return http.StatusConflict, "idempotency_conflict"
	case errors.Is(err, cluster.ErrShardExists), errors.Is(err, cluster.ErrNodeExists):
		return http.StatusConflict, "already_exists"
	case errors.Is(err, cluster.ErrBadTarget):
		return http.StatusBadRequest, "bad_request"
	default:
		return http.StatusInternalServerError, "internal_error"
	}
}

func writeClusterErr(w http.ResponseWriter, err error) {
	status, code := errorStatus(err)
	writeError(w, status, code, err.Error())
}

// ---- helpers --------------------------------------------------------------

type controlReq struct {
	IdempotencyKey string `json:"idempotency_key"`
}

func decodeBody(w http.ResponseWriter, r *http.Request, dst any) bool {
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(dst); err != nil && !errors.Is(err, io.EOF) {
		writeError(w, http.StatusBadRequest, "bad_json", "invalid JSON body: "+err.Error())
		return false
	}
	return true
}

func idemKey(r *http.Request, bodyKey string) string {
	if h := r.Header.Get("Idempotency-Key"); h != "" {
		return h
	}
	return bodyKey
}

// ---- handlers -------------------------------------------------------------

func (s *Server) healthz(w http.ResponseWriter, r *http.Request) {
	writeData(w, http.StatusOK, map[string]string{"status": "ok"}, false)
}

func (s *Server) state(w http.ResponseWriter, r *http.Request) {
	writeData(w, http.StatusOK, s.cl.State(), false)
}

func (s *Server) getShard(w http.ResponseWriter, r *http.Request) {
	v, err := s.cl.Shard(r.PathValue("id"))
	if err != nil {
		writeClusterErr(w, err)
		return
	}
	writeData(w, http.StatusOK, v, false)
}

func (s *Server) listWrites(w http.ResponseWriter, r *http.Request) {
	ws, err := s.cl.Writes(r.PathValue("id"))
	if err != nil {
		writeClusterErr(w, err)
		return
	}
	writeData(w, http.StatusOK, map[string]any{"shard_id": r.PathValue("id"), "writes": ws}, false)
}

func (s *Server) audit(w http.ResponseWriter, r *http.Request) {
	res, err := s.cl.Audit(r.PathValue("id"))
	if err != nil {
		writeClusterErr(w, err)
		return
	}
	writeData(w, http.StatusOK, res, false)
}

type writeReq struct {
	Payload        string `json:"payload"`
	ClientRouteVer int    `json:"client_route_ver"`
	IdempotencyKey string `json:"idempotency_key"`
}

func (s *Server) clientWrite(w http.ResponseWriter, r *http.Request) {
	var req writeReq
	if !decodeBody(w, r, &req) {
		return
	}
	cw, replay, err := s.cl.ClientWrite(cluster.WriteInput{
		ShardID:        r.PathValue("id"),
		Payload:        req.Payload,
		ClientRouteVer: req.ClientRouteVer,
		IdempotencyKey: idemKey(r, req.IdempotencyKey),
	})
	if err != nil {
		writeClusterErr(w, err)
		return
	}
	writeData(w, http.StatusOK, cw, replay)
}

type startReq struct {
	TargetID       string `json:"target_id"`
	IdempotencyKey string `json:"idempotency_key"`
}

func (s *Server) migrationStart(w http.ResponseWriter, r *http.Request) {
	var req startReq
	if !decodeBody(w, r, &req) {
		return
	}
	res, replay, err := s.cl.Start(r.PathValue("id"), req.TargetID, idemKey(r, req.IdempotencyKey))
	if err != nil {
		writeClusterErr(w, err)
		return
	}
	writeData(w, http.StatusOK, res, replay)
}

func (s *Server) snapshotComplete(w http.ResponseWriter, r *http.Request) {
	var req controlReq
	if !decodeBody(w, r, &req) {
		return
	}
	res, replay, err := s.cl.CompleteSnapshot(r.PathValue("id"), idemKey(r, req.IdempotencyKey))
	if err != nil {
		writeClusterErr(w, err)
		return
	}
	writeData(w, http.StatusOK, res, replay)
}

func (s *Server) catchup(w http.ResponseWriter, r *http.Request) {
	var req controlReq
	if !decodeBody(w, r, &req) {
		return
	}
	res, replay, err := s.cl.Catchup(r.PathValue("id"), idemKey(r, req.IdempotencyKey))
	if err != nil {
		writeClusterErr(w, err)
		return
	}
	writeData(w, http.StatusOK, res, replay)
}

func (s *Server) switchRoute(w http.ResponseWriter, r *http.Request) {
	var req controlReq
	if !decodeBody(w, r, &req) {
		return
	}
	res, replay, err := s.cl.Switch(r.PathValue("id"), idemKey(r, req.IdempotencyKey))
	if err != nil {
		writeClusterErr(w, err)
		return
	}
	writeData(w, http.StatusOK, res, replay)
}

func (s *Server) resetShard(w http.ResponseWriter, r *http.Request) {
	v, err := s.cl.ResetShard(r.PathValue("id"))
	if err != nil {
		writeClusterErr(w, err)
		return
	}
	writeData(w, http.StatusOK, v, false)
}

type connResp struct {
	NodeID    string `json:"node_id"`
	Connected bool   `json:"connected"`
}

func (s *Server) disconnect(w http.ResponseWriter, r *http.Request) {
	v, err := s.cl.SetConnected(r.PathValue("id"), false)
	if err != nil {
		writeClusterErr(w, err)
		return
	}
	writeData(w, http.StatusOK, connResp{NodeID: v.ID, Connected: v.Connected}, false)
}

func (s *Server) reconnect(w http.ResponseWriter, r *http.Request) {
	v, err := s.cl.SetConnected(r.PathValue("id"), true)
	if err != nil {
		writeClusterErr(w, err)
		return
	}
	writeData(w, http.StatusOK, connResp{NodeID: v.ID, Connected: v.Connected}, false)
}

// recoverer turns an unexpected panic into a 500 instead of dropping the
// connection (net/http's default), so the API always speaks JSON.
func recoverer(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				writeError(w, http.StatusInternalServerError, "panic", "internal server error")
			}
		}()
		h.ServeHTTP(w, r)
	})
}

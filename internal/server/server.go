// Package server exposes the engine over a small JSON HTTP API.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/example/snapshotprune/internal/engine"
	"github.com/example/snapshotprune/internal/replay"
	"github.com/example/snapshotprune/internal/snapshot"
	"github.com/example/snapshotprune/internal/types"
)

// Server wraps an http.Server.
type Server struct {
	eng   *engine.Engine
	log   *slog.Logger
	httpd *http.Server
}

// New wires routes. addr is the listen address.
func New(eng *engine.Engine, addr string, log *slog.Logger) *Server {
	mux := http.NewServeMux()
	s := &Server{
		eng: eng,
		log: log,
		httpd: &http.Server{
			Addr:              addr,
			ReadHeaderTimeout: 10 * time.Second,
		},
	}
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /v1/info", s.handleInfo)
	mux.HandleFunc("GET /v1/blocks/", s.handleGetBlock)
	mux.HandleFunc("POST /v1/blocks", s.handlePropose)
	mux.HandleFunc("POST /v1/leases", s.handleCreateLease)
	mux.HandleFunc("GET /v1/leases", s.handleListLeases)
	mux.HandleFunc("GET /v1/leases/", s.handleGetLease)
	mux.HandleFunc("DELETE /v1/leases/", s.handleReleaseLease)
	mux.HandleFunc("GET /v1/state", s.handleQuery)
	mux.HandleFunc("GET /v1/snapshots", s.handleListSnapshots)
	mux.HandleFunc("POST /v1/snapshots", s.handleBuildSnapshot)
	mux.HandleFunc("DELETE /v1/snapshots/", s.handleDeleteSnapshot)
	mux.HandleFunc("POST /v1/recover", s.handleRecover)
	mux.HandleFunc("POST /v1/prune", s.handlePrune)
	mux.HandleFunc("POST /v1/replay", s.handleReplay)
	s.httpd.Handler = mux
	return s
}

// Addr returns the effective listen address after ListenAndServe.
func (s *Server) Addr() string { return s.httpd.Addr }

// Start serves until Shutdown.
func (s *Server) Start() error {
	s.log.Info("http server listening", "addr", s.httpd.Addr)
	if err := s.httpd.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

// Shutdown gracefully stops the server.
func (s *Server) Shutdown(ctx context.Context) error { return s.httpd.Shutdown(ctx) }

type errBody struct {
	Error string `json:"error"`
	Code  string `json:"code,omitempty"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, errBody{Error: msg, Code: code})
}

// mapEngineError translates engine sentinels to HTTP status + code.
func mapEngineError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, engine.ErrLeaseExpired):
		writeErr(w, http.StatusGone, "lease_expired", err.Error())
	case errors.Is(err, engine.ErrLeaseInvalid):
		writeErr(w, http.StatusNotFound, "lease_invalid", err.Error())
	case errors.Is(err, engine.ErrLeaseBadHeight):
		writeErr(w, http.StatusBadRequest, "bad_height", err.Error())
	case errors.Is(err, engine.ErrBadHeight):
		writeErr(w, http.StatusBadRequest, "bad_height", err.Error())
	case errors.Is(err, engine.ErrBadParent):
		writeErr(w, http.StatusConflict, "bad_parent", err.Error())
	case errors.Is(err, engine.ErrSnapshotBusy):
		writeErr(w, http.StatusConflict, "snapshot_busy", err.Error())
	case errors.Is(err, engine.ErrSnapshotExists):
		writeErr(w, http.StatusConflict, "snapshot_exists", err.Error())
	case errors.Is(err, engine.ErrSnapshotMissing):
		writeErr(w, http.StatusNotFound, "snapshot_missing", err.Error())
	case errors.Is(err, engine.ErrMissingDeltas):
		writeErr(w, http.StatusGone, "history_pruned", err.Error())
	default:
		writeErr(w, http.StatusBadRequest, "invalid", err.Error())
	}
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	tip, err := s.eng.Tip()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "tip": tip})
}

func (s *Server) handleInfo(w http.ResponseWriter, r *http.Request) {
	tip, _ := s.eng.Tip()
	gen, _ := s.eng.GenesisBlock()
	writeJSON(w, http.StatusOK, map[string]any{
		"chain_id":     s.eng.ChainID(),
		"tip":          tip,
		"node_pubkey":  s.eng.NodePubHex(),
		"genesis_hash": gen.Hash().Hex(),
		"genesis_root": gen.Header.StateRoot.Hex(),
		"snapshots":    s.eng.Snaps().List(),
	})
}

func parseHeightSuffix(prefix, path string) (uint64, error) {
	rest := strings.TrimPrefix(path, prefix)
	rest = strings.Trim(rest, "/")
	if rest == "" {
		return 0, errors.New("missing height")
	}
	h, err := strconv.ParseUint(rest, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid height %q", rest)
	}
	return h, nil
}

func (s *Server) handleGetBlock(w http.ResponseWriter, r *http.Request) {
	h, err := parseHeightSuffix("/v1/blocks/", r.URL.Path)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad_height", err.Error())
		return
	}
	blk, err := s.eng.Block(h)
	if err != nil {
		mapEngineError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, blk)
}

type proposeResp struct {
	Hash  string       `json:"hash"`
	Block *types.Block `json:"block"`
}

func (s *Server) handlePropose(w http.ResponseWriter, r *http.Request) {
	var p engine.Proposal
	if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
		writeErr(w, http.StatusBadRequest, "bad_json", err.Error())
		return
	}
	blk, err := s.eng.AppendProposal(&p)
	if err != nil {
		mapEngineError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, proposeResp{Hash: blk.Hash().Hex(), Block: blk})
}

type createLeaseReq struct {
	Height uint64 `json:"height"`
}

func (s *Server) handleCreateLease(w http.ResponseWriter, r *http.Request) {
	var req createLeaseReq
	if r.Body != nil {
		_ = json.NewDecoder(r.Body).Decode(&req)
	}
	l, err := s.eng.CreateLease(req.Height)
	if err != nil {
		mapEngineError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, l)
}

func (s *Server) handleListLeases(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"leases": s.eng.ActiveLeases()})
}

func leaseIDFromPath(path string) string {
	return strings.Trim(strings.TrimPrefix(path, "/v1/leases/"), "/")
}

func (s *Server) handleGetLease(w http.ResponseWriter, r *http.Request) {
	id := leaseIDFromPath(r.URL.Path)
	l, err := s.eng.Lease(id)
	if err != nil {
		mapEngineError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, l)
}

func (s *Server) handleReleaseLease(w http.ResponseWriter, r *http.Request) {
	id := leaseIDFromPath(r.URL.Path)
	if err := s.eng.ReleaseLease(id); err != nil {
		mapEngineError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"released": id})
}

func (s *Server) handleQuery(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("lease")
	if id == "" {
		writeErr(w, http.StatusBadRequest, "lease_required",
			"history reads require an explicit reader lease: POST /v1/leases then pass ?lease=<id>")
		return
	}
	var acct *types.Address
	if a := r.URL.Query().Get("account"); a != "" {
		addr, err := types.ParseAddress(a)
		if err != nil {
			writeErr(w, http.StatusBadRequest, "bad_account", err.Error())
			return
		}
		acct = &addr
	}
	view, src, err := s.eng.QueryAtWithLease(id, acct)
	if err != nil {
		mapEngineError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"source": src, "view": view})
}

func (s *Server) handleListSnapshots(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"snapshots": s.eng.Snaps().List(),
		"keep":      snapshot.KeepCount,
		"leftovers": leftoversOrEmpty(s.eng),
	})
}

func leftoversOrEmpty(eng *engine.Engine) []string {
	l, err := eng.Snaps().Leftovers()
	if err != nil {
		return []string{}
	}
	return l
}

type buildReq struct {
	Height uint64 `json:"height"`
}

func (s *Server) handleBuildSnapshot(w http.ResponseWriter, r *http.Request) {
	var req buildReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "bad_json", err.Error())
		return
	}
	info, err := s.eng.BuildSnapshot(req.Height)
	if err != nil {
		mapEngineError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, info)
}

func (s *Server) handleDeleteSnapshot(w http.ResponseWriter, r *http.Request) {
	h, err := parseHeightSuffix("/v1/snapshots/", r.URL.Path)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad_height", err.Error())
		return
	}
	if err := s.eng.Snaps().Delete(h); err != nil {
		if strings.Contains(err.Error(), "active readers") {
			writeErr(w, http.StatusConflict, "snapshot_pinned", err.Error())
			return
		}
		writeErr(w, http.StatusNotFound, "snapshot_missing", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"deleted": h})
}

func (s *Server) handleRecover(w http.ResponseWriter, r *http.Request) {
	removed, err := s.eng.Snaps().Recover()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"removed_temp_snapshots": removed})
}

func (s *Server) handlePrune(w http.ResponseWriter, r *http.Request) {
	rep, err := s.eng.Prune()
	if err != nil {
		mapEngineError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

func (s *Server) handleReplay(w http.ResponseWriter, r *http.Request) {
	chk := replay.NewChecker(s.eng.Store(), s.eng.Snaps(), s.eng)
	res, err := chk.VerifyFromScratch()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "replay_failed", err.Error())
		return
	}
	status := http.StatusOK
	if !res.Matches {
		status = http.StatusConflict
	}
	writeJSON(w, status, res)
}

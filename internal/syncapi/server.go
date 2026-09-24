package syncapi

import (
	"encoding/json"
	"net/http"
	"sync"

	"merklesync/internal/merkle"
	"merklesync/internal/store"
)

// ---------- protocol types ----------

// RootInfo describes a replica's current tree root.
type RootInfo struct {
	Epoch    int64  `json:"epoch"`
	Revision int64  `json:"revision"`
	Root     string `json:"root"`
}

// EntryMeta is a record without its value, used to compare versions in a
// divergent bucket before deciding which values actually need transferring.
type EntryMeta struct {
	Key     string `json:"key"`
	Version int64  `json:"version"`
	Deleted bool   `json:"deleted,omitempty"`
}

type nodesRequest struct {
	Revision int64 `json:"revision"`
	Level    int   `json:"level"`
	Indexes  []int `json:"indexes"`
}

type nodesResponse struct {
	Hashes []string `json:"hashes"`
}

type leavesRequest struct {
	Revision int64 `json:"revision"`
	Buckets  []int `json:"buckets"`
}

type leavesResponse struct {
	Entries []EntryMeta `json:"entries"`
}

type entriesRequest struct {
	Revision int64    `json:"revision"`
	Keys     []string `json:"keys"`
}

type entriesResponse struct {
	Entries []store.Entry `json:"entries"`
}

type applyRequest struct {
	Entries []store.Entry `json:"entries"`
}

type applyResponse struct {
	Applied int `json:"applied"`
}

type writeRequest struct {
	Key     string `json:"key"`
	Value   string `json:"value"`
	Version int64  `json:"version"`
}

type resetResponse struct {
	Epoch int64 `json:"epoch"`
}

type snapshotResponse struct {
	store.Snapshot
}

// conflictError is the JSON body returned with HTTP 409. It tells the client
// that the revision it scanned against has changed and the round must restart.
type conflictError struct {
	Error           string `json:"error"`
	CurrentRevision int64  `json:"currentRevision"`
	CurrentEpoch    int64  `json:"currentEpoch"`
}

// chaosStep is a mutation injected before serving a revision-checked read.
// Test/demo only: it simulates a writer committing during a scan. When Repeat
// is true the step re-arms itself (until explicitly replaced or reset), which
// models a continuously mutating replica.
type chaosStep struct {
	after      int // fire before the after-th revision-checked scan read (0-based)
	op         string
	req        writeRequest
	repeatLeft int   // re-arm this many additional rounds
	scanReads  int64 // scan reads seen in the current round
}

// Server exposes one store replica over HTTP.
type Server struct {
	store *store.Store
	mux   *http.ServeMux

	mu          sync.Mutex
	cachedEpoch int64
	cachedRev   int64
	cachedTree  *merkle.Tree
	chaos       *chaosStep
}

func NewServer(st *store.Store) *Server {
	s := &Server{store: st, cachedEpoch: -1, cachedRev: -1}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /root", s.handleRoot)
	mux.HandleFunc("POST /nodes", s.handleNodes)
	mux.HandleFunc("POST /leaves", s.handleLeaves)
	mux.HandleFunc("POST /entries", s.handleEntries)
	mux.HandleFunc("POST /apply", s.handleApply)
	mux.HandleFunc("GET /snapshot", s.handleSnapshot)
	mux.HandleFunc("POST /put", s.handlePut)
	mux.HandleFunc("POST /delete", s.handleDelete)
	mux.HandleFunc("POST /reset", s.handleReset)
	mux.HandleFunc("POST /debug/chaos", s.handleChaos)
	s.mux = mux
	return s
}

func (s *Server) Handler() http.Handler { return s.mux }

// treeAt returns the tree for the store's current state, building/caching it.
func (s *Server) treeLocked() *merkle.Tree {
	rev, epoch := s.store.Revision(), s.store.Epoch()
	if s.cachedTree != nil && s.cachedEpoch == epoch && s.cachedRev == rev {
		return s.cachedTree
	}
	t := merkle.Build(s.store.Snapshot())
	s.cachedTree, s.cachedEpoch, s.cachedRev = t, epoch, rev
	return t
}

// fireChaosLocked applies a pending scan-time mutation at the armed position
// of a sync round. Must be called with s.mu held, and only for revision-
// checked scan reads (GET /root is excluded).
//
// Positions are counted per round from 0; GET /root restarts the count (see
// beginRoundLocked). Arming with after=0 fires before the first POST /nodes
// of every round — the canonical "mid-scan" point — aborting the round with
// 409 until repeatLeft is exhausted.
func (s *Server) fireChaosLocked() {
	c := s.chaos
	if c == nil {
		return
	}
	if c.scanReads != int64(c.after) {
		c.scanReads++
		return
	}
	if c.op == "delete" {
		_, _ = s.store.Delete(c.req.Key, c.req.Version)
	} else {
		_, _ = s.store.Put(c.req.Key, c.req.Value, c.req.Version)
	}
	if c.repeatLeft > 0 {
		c.repeatLeft--
		c.scanReads = 0
		c.req.Version++
		if c.op != "delete" {
			c.req.Value = c.req.Value + "!"
		}
	} else {
		s.chaos = nil
	}
}

// beginRoundLocked marks a GET /root as the start of a new scan round.
func (s *Server) beginRoundLocked() {
	if s.chaos != nil {
		s.chaos.scanReads = 0
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// checkRevision verifies a scan read against a consistent snapshot. The chaos
// mutation is injected only when the read's revision is still current; it is
// applied first so the very read that observes it returns 409, modelling a
// writer committing between the round's /root and this scan call.
func (s *Server) checkRevision(w http.ResponseWriter, revision int64) bool {
	s.mu.Lock()
	cur := s.store.Revision()
	epoch := s.store.Epoch()
	if revision != cur {
		s.mu.Unlock()
		writeJSON(w, http.StatusConflict, conflictError{
			Error:           "revision mismatch; retry with a fresh root",
			CurrentRevision: cur,
			CurrentEpoch:    epoch,
		})
		return false
	}
	// Revision is current: apply any armed mid-scan mutation so this read
	// is the first to observe the new revision.
	s.fireChaosLocked()
	cur = s.store.Revision()
	s.mu.Unlock()
	if revision != cur {
		writeJSON(w, http.StatusConflict, conflictError{
			Error:           "revision changed during scan; retry with a fresh root",
			CurrentRevision: cur,
			CurrentEpoch:    epoch,
		})
		return false
	}
	return true
}

func (s *Server) handleRoot(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	s.beginRoundLocked()
	t := s.treeLocked()
	info := RootInfo{Epoch: s.store.Epoch(), Revision: s.store.Revision(), Root: t.Root()}
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, info)
}

func (s *Server) handleNodes(w http.ResponseWriter, r *http.Request) {
	var req nodesRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !s.checkRevision(w, req.Revision) {
		return
	}
	s.mu.Lock()
	t := s.treeLocked()
	s.mu.Unlock()
	resp := nodesResponse{Hashes: make([]string, len(req.Indexes))}
	for i, idx := range req.Indexes {
		resp.Hashes[i] = t.Node(req.Level, idx)
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleLeaves(w http.ResponseWriter, r *http.Request) {
	var req leavesRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !s.checkRevision(w, req.Revision) {
		return
	}
	s.mu.Lock()
	t := s.treeLocked()
	s.mu.Unlock()
	resp := leavesResponse{}
	for _, b := range req.Buckets {
		for _, e := range t.LeafEntries(b) {
			resp.Entries = append(resp.Entries, EntryMeta{Key: e.Key, Version: e.Version, Deleted: e.Deleted})
		}
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleEntries(w http.ResponseWriter, r *http.Request) {
	var req entriesRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !s.checkRevision(w, req.Revision) {
		return
	}
	resp := entriesResponse{Entries: make([]store.Entry, 0, len(req.Keys))}
	for _, k := range req.Keys {
		if e, ok := s.store.Get(k); ok {
			resp.Entries = append(resp.Entries, e)
		}
	}
	writeJSON(w, http.StatusOK, resp)
}

// handleApply merges entries with LWW semantics. It deliberately does NOT
// require a matching revision: applying a snapshot can never corrupt state,
// because a stale entry loses by version. This keeps the retry loop simple.
func (s *Server) handleApply(w http.ResponseWriter, r *http.Request) {
	var req applyRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	applied := s.store.ApplyEntries(req.Entries)
	writeJSON(w, http.StatusOK, applyResponse{Applied: len(applied)})
}

func (s *Server) handleSnapshot(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.store.Snapshot())
}

func (s *Server) handlePut(w http.ResponseWriter, r *http.Request) {
	var req writeRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if req.Key == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "key required"})
		return
	}
	changed, err := s.store.Put(req.Key, req.Value, req.Version)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"changed": changed})
}

func (s *Server) handleDelete(w http.ResponseWriter, r *http.Request) {
	var req writeRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if req.Key == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "key required"})
		return
	}
	changed, err := s.store.Delete(req.Key, req.Version)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"changed": changed})
}

func (s *Server) handleReset(w http.ResponseWriter, r *http.Request) {
	s.store.Reset()
	s.mu.Lock()
	s.cachedTree, s.cachedEpoch, s.cachedRev = nil, -1, -1
	s.chaos = nil
	epoch := s.store.Epoch()
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, resetResponse{Epoch: epoch})
}

type chaosRequest struct {
	After   int    `json:"after"`
	Op      string `json:"op"`
	Key     string `json:"key"`
	Value   string `json:"value"`
	Version int64  `json:"version"`
	Repeat  int    `json:"repeat"` // re-arm this many additional times
}

// handleChaos arms a scan-time mutation. "after" counts revision-checked
// scan reads within one round starting at 0 (GET /root is not counted):
// after=0 fires before the first POST /nodes, forcing a 409 + retry.
// "repeat" re-arms the same mutation for that many additional rounds.
func (s *Server) handleChaos(w http.ResponseWriter, r *http.Request) {
	var req chaosRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if req.Op != "put" && req.Op != "delete" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": `op must be "put" or "delete"`})
		return
	}
	if req.Key == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "key required"})
		return
	}
	s.mu.Lock()
	s.chaos = &chaosStep{
		after:      req.After,
		op:         req.Op,
		req:        writeRequest{Key: req.Key, Value: req.Value, Version: req.Version},
		repeatLeft: req.Repeat,
	}
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]string{"status": "armed"})
}

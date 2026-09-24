// Package server wires store + merkle tree to an HTTP API using only the
// standard library net/http mux.
package server

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/example/merklekv/internal/hlc"
	"github.com/example/merklekv/internal/merkle"
	"github.com/example/merklekv/internal/store"
	"github.com/example/merklekv/internal/sync"
)

// maxBodyBytes caps request bodies (values, entry batches).
const maxBodyBytes = 64 << 20 // 64 MiB

// Server holds one replica's HTTP surface.
type Server struct {
	Store  *store.Store
	Params merkle.Params
	Logger *log.Logger

	mux       *http.ServeMux
	treeCache *treeCache
}

// New constructs the server and its routes.
func New(st *store.Store, params merkle.Params, logger *log.Logger) *Server {
	if logger == nil {
		logger = log.New(log.Writer(), "", log.LstdFlags)
	}
	s := &Server{
		Store:     st,
		Params:    params,
		Logger:    logger,
		treeCache: newTreeCache(16),
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /v1/config", s.handleConfig)

	mux.HandleFunc("GET /v1/kv/{key}", s.handleKVGet)
	mux.HandleFunc("PUT /v1/kv/{key}", s.handleKVPut)
	mux.HandleFunc("DELETE /v1/kv/{key}", s.handleKVDelete)

	mux.HandleFunc("POST /v1/snapshots", s.handleSnapshotCreate)
	mux.HandleFunc("POST /v1/snapshots/{id}/release", s.handleSnapshotRelease)
	mux.HandleFunc("POST /v1/nodes", s.handleNodes)
	mux.HandleFunc("POST /v1/entries", s.handleEntries)
	mux.HandleFunc("POST /v1/apply", s.handleApply)
	mux.HandleFunc("POST /v1/sync", s.handleSync)

	mux.HandleFunc("POST /v1/admin/seed", s.handleSeed)
	mux.HandleFunc("GET /v1/debug/state", s.handleDebugState)
	mux.HandleFunc("GET /v1/debug/tree", s.handleDebugTree)

	s.mux = mux
	return s
}

// Handler returns the wrapped root handler (logging middleware).
func (s *Server) Handler() http.Handler { return logRequests(s.Logger, s.mux) }

// ---- wire types -----------------------------------------------------------

type entryDTO struct {
	Key     string     `json:"key"`
	Value   []byte     `json:"value,omitempty"`
	Ver     versionDTO `json:"ver"`
	Origin  string     `json:"origin"`
	Deleted bool       `json:"deleted,omitempty"`
}

type versionDTO struct {
	Wall uint64 `json:"wall"`
	Log  uint64 `json:"log"`
}

func toDTO(e store.Entry) entryDTO {
	return entryDTO{
		Key: e.Key, Value: e.Value,
		Ver:     versionDTO{e.Ver.Wall, e.Ver.Log},
		Origin:  e.Origin,
		Deleted: e.Deleted,
	}
}

func fromDTO(d entryDTO) store.Entry {
	return store.Entry{
		Key: d.Key, Value: d.Value,
		Ver:     hlc.Timestamp{Wall: d.Ver.Wall, Log: d.Ver.Log},
		Origin:  d.Origin,
		Deleted: d.Deleted,
	}
}

type snapshotResp struct {
	SnapshotID string `json:"snapshot_id"`
	Epoch      int64  `json:"epoch"`
	RootHash   string `json:"root_hash"`
	Records    int    `json:"records"`
	Bytes      int64  `json:"bytes"`
	WireBytes  int64  `json:"wire_bytes"`
}

type nodesReq struct {
	SnapshotID string   `json:"snapshot_id"`
	Paths      []string `json:"paths"`
}

type nodeResp struct {
	Path     string   `json:"path"`
	Level    int      `json:"level"`
	Leaf     bool     `json:"leaf"`
	Hash     string   `json:"hash"`
	Children []string `json:"children,omitempty"`
	Bucket   int      `json:"bucket,omitempty"`
	Count    int      `json:"count,omitempty"`
	Error    string   `json:"error,omitempty"`
}

type entriesReq struct {
	SnapshotID string `json:"snapshot_id"`
	Buckets    []int  `json:"buckets"`
}

type bucketEntries struct {
	Bucket  int        `json:"bucket"`
	Entries []entryDTO `json:"entries"`
}

type applyReq struct {
	ExpectedEpoch int64      `json:"expected_epoch"`
	Entries       []entryDTO `json:"entries"`
}

type applyResp struct {
	Applied   int    `json:"applied"`
	Unchanged int    `json:"unchanged"`
	Epoch     int64  `json:"epoch"`
	RootHash  string `json:"root_hash"`
	Changed   bool   `json:"changed"`
}

type syncReq struct {
	Peer      string `json:"peer"`
	MaxRounds int    `json:"max_rounds,omitempty"`
}

type seedReq struct {
	Entries []entryDTO `json:"entries"`
}

// ---- handlers -------------------------------------------------------------

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "replica": s.Store.ReplicaID()})
}

func (s *Server) handleConfig(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"replica_id": s.Store.ReplicaID(),
		"epoch":      s.Store.Epoch(),
		"fanout":     s.Params.Fanout,
		"depth":      s.Params.Depth,
		"buckets":    s.Params.NumBuckets(),
	})
}

func (s *Server) handleKVGet(w http.ResponseWriter, r *http.Request) {
	key := r.PathValue("key")
	e, ok := s.Store.Get(key)
	if !ok {
		writeError(w, http.StatusNotFound, "not_found", "key absent or deleted")
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("X-Key", key)
	w.Header().Set("X-Origin", e.Origin)
	w.Header().Set("X-Version-Wall", strconv.FormatUint(e.Ver.Wall, 10))
	w.Header().Set("X-Version-Log", strconv.FormatUint(e.Ver.Log, 10))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(e.Value)
}

func (s *Server) handleKVPut(w http.ResponseWriter, r *http.Request) {
	key := r.PathValue("key")
	body := http.MaxBytesReader(w, r.Body, maxBodyBytes)
	value, err := io.ReadAll(body)
	if err != nil {
		writeError(w, http.StatusRequestEntityTooLarge, "body_too_large", err.Error())
		return
	}
	e := s.Store.Put(key, value)
	writeJSON(w, http.StatusOK, map[string]any{"entry": toDTO(e), "epoch": s.Store.Epoch()})
}

func (s *Server) handleKVDelete(w http.ResponseWriter, r *http.Request) {
	key := r.PathValue("key")
	e, existed := s.Store.Delete(key)
	status := http.StatusOK
	if !existed {
		status = http.StatusCreated // tombstone recorded for a previously unseen key
	}
	writeJSON(w, status, map[string]any{"entry": toDTO(e), "epoch": s.Store.Epoch()})
}

func (s *Server) handleSnapshotCreate(w http.ResponseWriter, r *http.Request) {
	snap := s.Store.Snapshot()
	tree, err := s.treeFor(snap)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "tree_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, snapshotResp{
		SnapshotID: snap.ID, Epoch: snap.Epoch, RootHash: tree.RootHash(),
		Records: snap.Records, Bytes: snap.Bytes, WireBytes: snap.WireBytes,
	})
}

func (s *Server) handleSnapshotRelease(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	s.Store.ReleaseSnapshot(id)
	s.treeCache.evict(id)
	writeJSON(w, http.StatusOK, map[string]string{"status": "released", "snapshot_id": id})
}

func (s *Server) handleNodes(w http.ResponseWriter, r *http.Request) {
	var req nodesReq
	if !decodeJSON(w, r, &req) {
		return
	}
	snap, ok := s.lookupSnapshot(w, req.SnapshotID)
	if !ok {
		return
	}
	tree, err := s.treeFor(snap)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "tree_error", err.Error())
		return
	}
	out := make([]nodeResp, 0, len(req.Paths))
	for _, p := range req.Paths {
		n, err := tree.Node(p)
		if err != nil {
			out = append(out, nodeResp{Path: p, Error: err.Error()})
			continue
		}
		out = append(out, nodeResp{
			Path: n.Path, Level: n.Level, Leaf: n.Leaf, Hash: n.Hash,
			Children: n.Children, Bucket: n.Bucket, Count: n.Count,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"nodes": out, "epoch": snap.Epoch})
}

func (s *Server) handleEntries(w http.ResponseWriter, r *http.Request) {
	var req entriesReq
	if !decodeJSON(w, r, &req) {
		return
	}
	snap, ok := s.lookupSnapshot(w, req.SnapshotID)
	if !ok {
		return
	}
	tree, err := s.treeFor(snap)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "tree_error", err.Error())
		return
	}
	buckets := make([]bucketEntries, 0, len(req.Buckets))
	for _, idx := range req.Buckets {
		if idx < 0 || idx >= s.Params.NumBuckets() {
			writeError(w, http.StatusBadRequest, "bad_bucket",
				fmt.Sprintf("bucket %d out of range [0,%d)", idx, s.Params.NumBuckets()))
			return
		}
		entries := tree.BucketEntries(idx)
		dtos := make([]entryDTO, 0, len(entries))
		for _, e := range entries {
			dtos = append(dtos, toDTO(e))
		}
		buckets = append(buckets, bucketEntries{Bucket: idx, Entries: dtos})
	}
	writeJSON(w, http.StatusOK, map[string]any{"buckets": buckets, "epoch": snap.Epoch})
}

func (s *Server) handleApply(w http.ResponseWriter, r *http.Request) {
	var req applyReq
	if !decodeJSON(w, r, &req) {
		return
	}
	entries := make([]store.Entry, len(req.Entries))
	for i, d := range req.Entries {
		entries[i] = fromDTO(d)
	}
	res, ok := s.Store.ApplyEntries(req.ExpectedEpoch, entries)
	if !ok {
		// Root moved during the scan: the client must restart the round.
		writeJSON(w, http.StatusConflict, map[string]any{
			"error": "epoch_moved", "epoch": res.Epoch,
		})
		return
	}
	// Post-merge root is read from a fresh snapshot of the new epoch.
	snap := s.Store.Snapshot()
	tree, err := s.treeFor(snap)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "tree_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, applyResp{
		Applied: res.Applied, Unchanged: res.Unchanged,
		Epoch: res.Epoch, RootHash: tree.RootHash(), Changed: res.Applied > 0,
	})
}

func (s *Server) handleSync(w http.ResponseWriter, r *http.Request) {
	var req syncReq
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.Peer == "" {
		writeError(w, http.StatusBadRequest, "missing_peer", "body needs \"peer\" base URL")
		return
	}
	cli, err := sync.NewClient(sync.Config{
		Local:     s.Store,
		Params:    s.Params,
		Peer:      strings.TrimRight(req.Peer, "/"),
		MaxRounds: req.MaxRounds,
	})
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_config", err.Error())
		return
	}
	result, err := cli.Run(r.Context())
	if err != nil {
		writeError(w, http.StatusConflict, "sync_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *Server) handleSeed(w http.ResponseWriter, r *http.Request) {
	var req seedReq
	if !decodeJSON(w, r, &req) {
		return
	}
	entries := make([]store.Entry, len(req.Entries))
	for i, d := range req.Entries {
		entries[i] = fromDTO(d)
	}
	s.Store.Seed(entries)
	s.treeCache.evictAll()
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "seeded", "records": len(entries), "epoch": s.Store.Epoch(),
	})
}

func (s *Server) handleDebugState(w http.ResponseWriter, r *http.Request) {
	all := s.Store.AllEntries()
	dtos := make([]entryDTO, len(all))
	for i, e := range all {
		dtos[i] = toDTO(e)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"replica_id": s.Store.ReplicaID(),
		"epoch":      s.Store.Epoch(),
		"entries":    dtos,
	})
}

func (s *Server) handleDebugTree(w http.ResponseWriter, r *http.Request) {
	var snap *store.Snapshot
	if id := r.URL.Query().Get("snapshot_id"); id != "" {
		sn, ok := s.Store.GetSnapshot(id)
		if !ok {
			writeError(w, http.StatusNotFound, "snapshot_not_found", "snapshot expired or unknown: "+id)
			return
		}
		snap = sn
	} else {
		snap = s.Store.Snapshot()
	}
	tree, err := s.treeFor(snap)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "tree_error", err.Error())
		return
	}
	type nodeOut struct {
		Path   string `json:"path"`
		Level  int    `json:"level"`
		Hash   string `json:"hash"`
		Leaf   bool   `json:"leaf"`
		Bucket int    `json:"bucket,omitempty"`
		Count  int    `json:"count,omitempty"`
	}
	var nodes []nodeOut
	for level := 0; level <= s.Params.Depth; level++ {
		count := 1
		for i := 0; i < s.Params.Depth-level; i++ {
			count *= s.Params.Fanout
		}
		for i := 0; i < count; i++ {
			var digits []int
			if level > 0 {
				digits = digitsFor(level, i, s.Params)
			}
			n, err := tree.Node(merkle.JoinPath(digits))
			if err != nil {
				writeError(w, http.StatusInternalServerError, "tree_error", err.Error())
				return
			}
			nodes = append(nodes, nodeOut{
				Path: n.Path, Level: n.Level, Hash: n.Hash, Leaf: n.Leaf,
				Bucket: n.Bucket, Count: n.Count,
			})
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"snapshot_id": snap.ID, "epoch": snap.Epoch,
		"root_hash": tree.RootHash(), "nodes": nodes,
	})
}

// ---- helpers ---------------------------------------------------------------

func (s *Server) lookupSnapshot(w http.ResponseWriter, id string) (*store.Snapshot, bool) {
	snap, ok := s.Store.GetSnapshot(id)
	if !ok {
		writeError(w, http.StatusNotFound, "snapshot_not_found",
			"snapshot expired or unknown; restart the sync round with a new one")
		return nil, false
	}
	return snap, true
}

func (s *Server) treeFor(snap *store.Snapshot) (*merkle.Tree, error) {
	return s.treeCache.get(snap.ID, func() *merkle.Tree {
		return merkle.Build(s.Params, snap)
	}), nil
}

func digitsFor(level, idx int, p merkle.Params) []int {
	// Rebuild base-fanout digits for node idx at the given level.
	digits := make([]int, level)
	for i := level - 1; i >= 0; i-- {
		digits[i] = idx % p.Fanout
		idx /= p.Fanout
	}
	return digits
}

// ---- JSON plumbing ---------------------------------------------------------

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(dst); err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", err.Error())
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(body)
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]string{"error": code, "message": msg})
}

// ---- logging middleware ----------------------------------------------------

type statusWriter struct {
	http.ResponseWriter
	status int
	n      int
}

func (sw *statusWriter) WriteHeader(code int) {
	sw.status = code
	sw.ResponseWriter.WriteHeader(code)
}

func (sw *statusWriter) Write(b []byte) (int, error) {
	if sw.status == 0 {
		sw.status = http.StatusOK
	}
	n, err := sw.ResponseWriter.Write(b)
	sw.n += n
	return n, err
}

func logRequests(logger *log.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w}
		next.ServeHTTP(sw, r)
		logger.Printf("%s %s -> %d (%d bytes) %s", r.Method, r.URL.Path,
			sw.status, sw.n, time.Since(start).Round(time.Microsecond))
	})
}

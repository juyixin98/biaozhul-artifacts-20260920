// Command consistent-hash exposes a weighted consistent hash ring over HTTP
// using only the Go standard library. It keeps an append-only list of
// immutable ring revisions so migration plans can be computed between any two
// configurations.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"sync"
)

// maxSnapshots bounds retained immutable revisions. Revision 0 (the empty
// ring) is never pruned.
const maxSnapshots = 100

// maxBodyBytes caps request body size.
const maxBodyBytes = 1 << 20

type snapshot struct {
	revision int
	ring     *Ring
}

type server struct {
	mu        sync.RWMutex
	snapshots []snapshot // ascending revision; snapshots[0] is revision 0
}

func newServer() *server {
	empty, _ := New(nil, DefaultBaseVNodes)
	return &server{snapshots: []snapshot{{revision: 0, ring: empty}}}
}

func (s *server) latest() snapshot { return s.snapshots[len(s.snapshots)-1] }

func (s *server) getSnapshot(rev int) (snapshot, bool) {
	for _, sn := range s.snapshots {
		if sn.revision == rev {
			return sn, true
		}
	}
	return snapshot{}, false
}

// resolveRevision maps -1 to the newest revision.
func (s *server) resolveRevision(rev int) (snapshot, bool) {
	if rev == -1 {
		return s.latest(), true
	}
	return s.getSnapshot(rev)
}

func (s *server) appendSnapshot(r *Ring) snapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	sn := snapshot{revision: s.latest().revision + 1, ring: r}
	s.snapshots = append(s.snapshots, sn)
	for len(s.snapshots) > maxSnapshots {
		// Keep index 0 (revision 0); drop the oldest revision after it.
		s.snapshots = append(s.snapshots[:1], s.snapshots[2:]...)
	}
	return sn
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]string{"error": code, "message": msg})
}

type nodeReq struct {
	ID     string `json:"id"`
	Weight *int   `json:"weight"`
}

type replaceReq struct {
	BaseVNodes int       `json:"base_vnodes"`
	Nodes      []nodeReq `json:"nodes"`
}

type batchReq struct {
	Keys     []string `json:"keys"`
	Revision int      `json:"revision"` // omitted/0 -> latest
}

func main() {
	addr := ":8080"
	srv := newServer()
	log.Printf("consistent-hash listening on %s", addr)
	log.Fatal(http.ListenAndServe(addr, logRequests(srv.routes())))
}

func (s *server) routes() *http.ServeMux {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /v1/ring", s.handleGetRing)
	mux.HandleFunc("GET /v1/revisions", s.handleRevisions)
	mux.HandleFunc("POST /v1/nodes", s.handleAddNode)
	mux.HandleFunc("DELETE /v1/nodes/{id}", s.handleDeleteNode)
	mux.HandleFunc("POST /v1/ring/replace", s.handleReplace)
	mux.HandleFunc("GET /v1/route", s.handleRoute)
	mux.HandleFunc("POST /v1/route/batch", s.handleRouteBatch)
	mux.HandleFunc("GET /v1/plans", s.handlePlans)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		writeError(w, http.StatusNotFound, "not_found", "unknown path: "+r.URL.Path)
	})
	return mux
}

func logRequests(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h.ServeHTTP(w, r)
		log.Printf("%s %s", r.Method, r.URL.RequestURI())
	})
}

type nodeJSON struct {
	ID           string `json:"id"`
	Weight       int    `json:"weight"`
	VirtualNodes int    `json:"virtual_nodes"`
}

func ringPayload(sn snapshot, includeVNodes bool) map[string]any {
	nodes := sn.ring.Nodes()
	out := make([]nodeJSON, len(nodes))
	for i, n := range nodes {
		out[i] = nodeJSON{ID: n.ID, Weight: n.Weight, VirtualNodes: n.Weight * sn.ring.BaseVNodes()}
	}
	p := map[string]any{
		"revision":     sn.revision,
		"base_vnodes":  sn.ring.BaseVNodes(),
		"node_count":   len(nodes),
		"vnodes_total": len(sn.ring.VNodes()),
		"nodes":        out,
	}
	if includeVNodes {
		p["vnodes"] = sn.ring.VNodes()
	}
	return p
}

func optionalRevision(r *http.Request) (int, error) {
	v := r.URL.Query().Get("revision")
	if v == "" {
		return -1, nil
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < 0 {
		return 0, errors.New("revision must be a non-negative integer")
	}
	return n, nil
}

func (s *server) handleGetRing(w http.ResponseWriter, r *http.Request) {
	rev, err := optionalRevision(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	s.mu.RLock()
	sn, ok := s.resolveRevision(rev)
	s.mu.RUnlock()
	if !ok {
		writeError(w, http.StatusNotFound, "unknown_revision", "revision not found: "+strconv.Itoa(rev))
		return
	}
	v := r.URL.Query().Get("include_vnodes")
	include := v == "1" || v == "true"
	writeJSON(w, http.StatusOK, ringPayload(sn, include))
}

func (s *server) handleRevisions(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]map[string]any, len(s.snapshots))
	for i, sn := range s.snapshots {
		out[i] = ringPayload(sn, false)
	}
	writeJSON(w, http.StatusOK, map[string]any{"revisions": out})
}

// mutate applies fn to the latest node set and commits a new revision. The
// whole read-modify-write runs under the write lock so concurrent mutations
// cannot interleave (lost updates). A returned errNoChange is an idempotent
// success without a new revision.
func (s *server) mutate(w http.ResponseWriter, fn func([]Node) ([]Node, error)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	sn := s.latest()

	next, err := fn(sn.ring.Nodes())
	if errors.Is(err, errNoChange) {
		writeJSON(w, http.StatusOK, ringPayload(sn, false))
		return
	}
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	newRing, err := New(next, sn.ring.BaseVNodes())
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_ring", err.Error())
		return
	}
	sn = snapshot{revision: sn.revision + 1, ring: newRing}
	s.snapshots = append(s.snapshots, sn)
	for len(s.snapshots) > maxSnapshots {
		// Keep index 0 (revision 0); drop the oldest revision after it.
		s.snapshots = append(s.snapshots[:1], s.snapshots[2:]...)
	}
	writeJSON(w, http.StatusCreated, ringPayload(sn, false))
}

var errNoChange = errors.New("no change")

func (s *server) handleAddNode(w http.ResponseWriter, r *http.Request) {
	var req nodeReq
	if err := decodeBody(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	if req.ID == "" {
		writeError(w, http.StatusBadRequest, "bad_request", "'id' is required")
		return
	}
	weight := 1
	if req.Weight != nil {
		weight = *req.Weight
	}
	s.mutate(w, func(nodes []Node) ([]Node, error) {
		for _, n := range nodes {
			if n.ID == req.ID {
				if n.Weight == weight {
					return nil, errNoChange
				}
				return nil, fmt.Errorf("node already exists: %s (change weights via POST /v1/ring/replace)", req.ID)
			}
		}
		return append(nodes, Node{ID: req.ID, Weight: weight}), nil
	})
}

func (s *server) handleDeleteNode(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	s.mutate(w, func(nodes []Node) ([]Node, error) {
		out := make([]Node, 0, len(nodes))
		found := false
		for _, n := range nodes {
			if n.ID == id {
				found = true
				continue
			}
			out = append(out, n)
		}
		if !found {
			return nil, fmt.Errorf("node not found: %s", id)
		}
		return out, nil
	})
}

func (s *server) handleReplace(w http.ResponseWriter, r *http.Request) {
	var req replaceReq
	if err := decodeBody(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	s.mu.RLock()
	base := s.latest().ring.BaseVNodes()
	s.mu.RUnlock()
	if req.BaseVNodes > 0 {
		base = req.BaseVNodes
	}
	nodes := make([]Node, 0, len(req.Nodes))
	for _, n := range req.Nodes {
		w := 1
		if n.Weight != nil {
			w = *n.Weight
		}
		nodes = append(nodes, Node{ID: n.ID, Weight: w})
	}
	newRing, err := New(nodes, base)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_ring", err.Error())
		return
	}
	committed := s.appendSnapshot(newRing)
	writeJSON(w, http.StatusCreated, ringPayload(committed, false))
}

type routeResp struct {
	Key      string `json:"key"`
	Hash     uint64 `json:"hash"`
	HashHex  string `json:"hash_hex"`
	Node     string `json:"node"`
	Revision int    `json:"revision"`
}

func (s *server) handleRoute(w http.ResponseWriter, r *http.Request) {
	key := r.URL.Query().Get("key")
	if key == "" {
		writeError(w, http.StatusBadRequest, "bad_request", "query parameter 'key' is required")
		return
	}
	rev, err := optionalRevision(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	s.mu.RLock()
	sn, ok := s.resolveRevision(rev)
	s.mu.RUnlock()
	if !ok {
		writeError(w, http.StatusNotFound, "unknown_revision", "revision not found")
		return
	}
	node, err := sn.ring.Route(key)
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "empty_ring", "ring has no nodes; add a node first")
		return
	}
	h := HashKey(key)
	writeJSON(w, http.StatusOK, routeResp{
		Key: key, Hash: h, HashHex: "0x" + hashHex(h), Node: node, Revision: sn.revision,
	})
}

type batchItem struct {
	Key     string `json:"key"`
	Hash    uint64 `json:"hash"`
	HashHex string `json:"hash_hex"`
	Node    string `json:"node"`
}

func (s *server) handleRouteBatch(w http.ResponseWriter, r *http.Request) {
	var req batchReq
	if err := decodeBody(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	if len(req.Keys) == 0 {
		writeError(w, http.StatusBadRequest, "bad_request", "'keys' must contain at least one key")
		return
	}
	var sn snapshot
	var ok bool
	s.mu.RLock()
	if req.Revision == 0 {
		sn, ok = s.latest(), true
	} else {
		sn, ok = s.getSnapshot(req.Revision)
	}
	s.mu.RUnlock()
	if !ok {
		writeError(w, http.StatusNotFound, "unknown_revision", "revision not found: "+strconv.Itoa(req.Revision))
		return
	}
	items := make([]batchItem, 0, len(req.Keys))
	for _, k := range req.Keys {
		node, err := sn.ring.Route(k)
		if err != nil {
			writeError(w, http.StatusServiceUnavailable, "empty_ring", "ring has no nodes; add a node first")
			return
		}
		h := HashKey(k)
		items = append(items, batchItem{Key: k, Hash: h, HashHex: "0x" + hashHex(h), Node: node})
	}
	writeJSON(w, http.StatusOK, map[string]any{"revision": sn.revision, "routes": items})
}

func (s *server) handlePlans(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	s.mu.RLock()
	defer s.mu.RUnlock()
	latest := s.latest().revision
	from, to := latest-1, latest

	if v := q.Get("from"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			writeError(w, http.StatusBadRequest, "bad_request", "'from' must be a non-negative integer")
			return
		}
		from = n
	}
	if v := q.Get("to"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			writeError(w, http.StatusBadRequest, "bad_request", "'to' must be a non-negative integer")
			return
		}
		to = n
	}
	if from > to {
		writeError(w, http.StatusBadRequest, "bad_request", "'from' must be <= 'to'")
		return
	}
	oldSN, ok1 := s.getSnapshot(from)
	newSN, ok2 := s.getSnapshot(to)
	if !ok1 || !ok2 {
		writeError(w, http.StatusNotFound, "unknown_revision",
			fmt.Sprintf("revision %d or %d not found (old revisions prune after %d stored)", from, to, maxSnapshots))
		return
	}
	writeJSON(w, http.StatusOK, BuildPlan(from, to, oldSN.ring, newSN.ring))
}

func decodeBody(w http.ResponseWriter, r *http.Request, v any) error {
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return fmt.Errorf("invalid JSON body: %w", err)
	}
	if err := dec.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return errors.New("invalid JSON body: unexpected trailing data")
	}
	return nil
}

func hashHex(h uint64) string {
	const hexd = "0123456789abcdef"
	var b [16]byte
	for i := 15; i >= 0; i-- {
		b[i] = hexd[h&0xf]
		h >>= 4
	}
	return string(b[:])
}

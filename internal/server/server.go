// Package server exposes an OR-Set replica over HTTP using only net/http.
//
// Endpoints (all JSON):
//
//	GET  /health                     liveness + replica id
//	GET  /                           endpoint listing
//	POST /elements        {"element": "x"}           add (returns minted tag)
//	DELETE /elements/{e}                                         remove
//	GET  /elements                                               current set
//	GET  /state                     full CRDT state (state-based pull)
//	POST /state                      merge a pulled state (state-based push)
//	POST /ops                        apply/replicate a single Op
//	POST /sync        {"peers": [...], "push": true}  exchange state with peers
//	GET  /gc/eligible?peer=&peer=   tags safe to GC across all listed peers
//	POST /gc                        coordinate a full GC round over peers
//	POST /gc/purge    {"tags": {...}}              admin: force purge
//	GET  /debug/stats               structural counters
package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strings"
	"time"

	"orset/internal/gc"
	"orset/internal/orset"
)

// maxBody caps request bodies to keep the server cheap to fuzz.
const maxBody = 4 << 20 // 4 MiB

// Server holds one replica and its HTTP machinery.
type Server struct {
	Set    *orset.ORSet
	Logger *log.Logger
	HTTP   *http.Client // client used for /sync and /gc peer calls
}

// New wires a replica into an http.Handler.
func New(set *orset.ORSet, logger *log.Logger) *Server {
	if logger == nil {
		logger = log.New(io.Discard, "", 0)
	}
	return &Server{
		Set:    set,
		Logger: logger,
		HTTP:   &http.Client{Timeout: 10 * time.Second},
	}
}

// Handler returns the configured router (Go 1.22 method+wildcard patterns).
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("GET /", s.handleIndex)
	mux.HandleFunc("POST /elements", s.handleAdd)
	mux.HandleFunc("DELETE /elements/{element}", s.handleRemove)
	mux.HandleFunc("GET /elements", s.handleElements)
	mux.HandleFunc("GET /state", s.handleGetState)
	mux.HandleFunc("POST /state", s.handleMergeState)
	mux.HandleFunc("POST /ops", s.handleOps)
	mux.HandleFunc("POST /sync", s.handleSync)
	mux.HandleFunc("GET /gc/eligible", s.handleGCEligible)
	mux.HandleFunc("POST /gc", s.handleGCRun)
	mux.HandleFunc("POST /gc/purge", s.handleGCPurge)
	mux.HandleFunc("GET /debug/stats", s.handleStats)
	return s.logging(mux)
}

// ListenAndServe starts serving on addr (blocking).
func (s *Server) ListenAndServe(addr string) error {
	srv := &http.Server{
		Addr:              addr,
		Handler:           s.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	return srv.ListenAndServe()
}

// ---------- request / response helpers ----------

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		// Headers already sent; best-effort logging happens at caller level.
		return
	}
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// decodeJSON reads a size-limited JSON body into v.
func decodeJSON(w http.ResponseWriter, r *http.Request, v any) bool {
	body := http.MaxBytesReader(w, r.Body, maxBody)
	dec := json.NewDecoder(body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return false
	}
	if dec.More() {
		writeErr(w, http.StatusBadRequest, "invalid JSON: multiple values")
		return false
	}
	return true
}

func (s *Server) logging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(sw, r)
		s.Logger.Printf("%s %s %q -> %d", r.RemoteAddr, r.Method, r.URL.RequestURI(), sw.status)
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}

// ---------- basic CRDT endpoints ----------

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{
		"status":    "ok",
		"replicaId": s.Set.ReplicaID(),
	})
}

func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeErr(w, http.StatusNotFound, "no such endpoint; see GET /")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"service": "orset replica",
		"endpoints": []string{
			"POST   /elements          body {\"element\": \"x\"}",
			"DELETE /elements/{e}      observed-remove of e",
			"GET    /elements          current members",
			"GET    /state            full CRDT state",
			"POST   /state            merge body {add,tombstones}",
			"POST   /ops              apply one {type,element,tags}",
			"POST   /sync             body {\"peers\":[...],\"push\":bool,\"pull\":bool}",
			"GET    /gc/eligible?peer=...&peer=...",
			"POST   /gc               body {\"peers\":[...]}",
			"POST   /gc/purge         body {\"tags\":{e:[tag]}}",
			"GET    /debug/stats",
		},
	})
}

type addReq struct {
	Element string `json:"element"`
}

func (s *Server) handleAdd(w http.ResponseWriter, r *http.Request) {
	var req addReq
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.Element == "" {
		writeErr(w, http.StatusBadRequest, "element must be a non-empty string")
		return
	}
	tag := s.Set.Add(req.Element)
	writeJSON(w, http.StatusOK, map[string]any{
		"element": req.Element,
		"tag":     tag,
		"op":      orset.Op{Type: "add", Element: req.Element, Tags: []string{tag}},
	})
}

func (s *Server) handleRemove(w http.ResponseWriter, r *http.Request) {
	element := r.PathValue("element")
	if element == "" {
		writeErr(w, http.StatusBadRequest, "missing element")
		return
	}
	tags, op := s.Set.Remove(element)
	code := http.StatusOK
	if len(tags) == 0 {
		code = http.StatusNoContent // observed nothing: remove was a no-op
	}
	writeJSON(w, code, map[string]any{"element": element, "observedTags": tags, "op": op})
}

func (s *Server) handleElements(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"elements": s.Set.Values()})
}

func (s *Server) handleGetState(w http.ResponseWriter, r *http.Request) {
	st := s.Set.Snapshot()
	writeJSON(w, http.StatusOK, map[string]any{
		"replicaId": s.Set.ReplicaID(),
		"state":     st,
	})
}

func (s *Server) handleMergeState(w http.ResponseWriter, r *http.Request) {
	var req struct {
		State orset.State `json:"state"`
	}
	if !decodeJSON(w, r, &req) {
		return
	}
	before := s.Set.Stats()
	s.Set.Merge(req.State)
	after := s.Set.Stats()
	writeJSON(w, http.StatusOK, map[string]any{"merged": true, "before": before, "after": after})
}

func (s *Server) handleOps(w http.ResponseWriter, r *http.Request) {
	var op orset.Op
	if !decodeJSON(w, r, &op) {
		return
	}
	if err := s.Set.ApplyOp(op); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"applied": true, "elements": s.Set.Values()})
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.Set.Stats())
}

// ---------- sync ----------

type syncReq struct {
	Peers []string `json:"peers"`
	Pull  *bool    `json:"pull"` // default true
	Push  *bool    `json:"push"` // default true
}

type peerResult struct {
	Peer   string `json:"peer"`
	Pulled bool   `json:"pulled"`
	Pushed bool   `json:"pushed"`
	Error  string `json:"error,omitempty"`
}

func (s *Server) handleSync(w http.ResponseWriter, r *http.Request) {
	var req syncReq
	if !decodeJSON(w, r, &req) {
		return
	}
	pull, push := true, true
	if req.Pull != nil {
		pull = *req.Pull
	}
	if req.Push != nil {
		push = *req.Push
	}

	results := make([]peerResult, 0, len(req.Peers))
	ok := true
	for _, p := range req.Peers {
		res := peerResult{Peer: p}
		if pull {
			if st, err := s.fetchState(r.Context(), p); err != nil {
				res.Error = err.Error()
			} else {
				s.Set.Merge(st)
				res.Pulled = true
			}
		}
		// Push uses a fresh post-merge snapshot, so a pull+push round with
		// both sides converges the pair in either direction.
		if push && res.Error == "" {
			if err := s.pushState(r.Context(), p, s.Set.Snapshot()); err != nil {
				res.Error = err.Error()
			} else {
				res.Pushed = true
			}
		}
		if res.Error != "" {
			ok = false
		}
		results = append(results, res)
	}
	status := http.StatusOK
	if !ok {
		status = http.StatusBadGateway
	}
	writeJSON(w, status, map[string]any{"ok": ok, "results": results, "elements": s.Set.Values()})
}

// fetchState GETs {peer}/state.
func (s *Server) fetchState(ctx context.Context, peer string) (orset.State, error) {
	u := strings.TrimRight(peer, "/") + "/state"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return orset.State{}, err
	}
	resp, err := s.HTTP.Do(req)
	if err != nil {
		return orset.State{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return orset.State{}, fmt.Errorf("%s returned %d: %s", u, resp.StatusCode, strings.TrimSpace(string(b)))
	}
	var out struct {
		State orset.State `json:"state"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxBody)).Decode(&out); err != nil {
		return orset.State{}, fmt.Errorf("%s bad JSON: %w", u, err)
	}
	return out.State, nil
}

// pushState POSTs state to {peer}/state.
func (s *Server) pushState(ctx context.Context, peer string, st orset.State) error {
	body, err := json.Marshal(map[string]any{"state": st})
	if err != nil {
		return err
	}
	u := strings.TrimRight(peer, "/") + "/state"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := s.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return fmt.Errorf("%s returned %d: %s", u, resp.StatusCode, strings.TrimSpace(string(b)))
	}
	return nil
}

// ---------- GC endpoints ----------

// httpClient adapts server-to-server HTTP calls to gc.Client.
type httpClient struct {
	base string
	http *http.Client
}

// NewGCClient returns a gc.Client that drives the /state and /gc/purge
// endpoints of another replica over HTTP. It is exported for the orset-gc
// command line tool.
func NewGCClient(baseURL string, hc *http.Client) gc.Client {
	if hc == nil {
		hc = &http.Client{Timeout: 10 * time.Second}
	}
	return &httpClient{base: strings.TrimRight(baseURL, "/"), http: hc}
}

func (c *httpClient) State(ctx context.Context) (orset.State, error) {
	u := strings.TrimRight(c.base, "/") + "/state"
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	resp, err := c.http.Do(req)
	if err != nil {
		return orset.State{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return orset.State{}, fmt.Errorf("%s: status %d", u, resp.StatusCode)
	}
	var out struct {
		State orset.State `json:"state"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxBody)).Decode(&out); err != nil {
		return orset.State{}, err
	}
	return out.State, nil
}

func (c *httpClient) Purge(ctx context.Context, tags map[string][]string) (int, error) {
	body, _ := json.Marshal(map[string]any{"tags": tags})
	u := strings.TrimRight(c.base, "/") + "/gc/purge"
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, u, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return 0, fmt.Errorf("%s: status %d %s", u, resp.StatusCode, strings.TrimSpace(string(b)))
	}
	var out struct {
		Purged int `json:"purged"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return 0, err
	}
	return out.Purged, nil
}

type purgeReq struct {
	Tags map[string][]string `json:"tags"`
}

func (s *Server) handleGCPurge(w http.ResponseWriter, r *http.Request) {
	var req purgeReq
	if !decodeJSON(w, r, &req) {
		return
	}
	n := s.Set.GCTags(req.Tags)
	writeJSON(w, http.StatusOK, map[string]any{"purged": n, "stats": s.Set.Stats()})
}

// peersFromQuery collects repeated ?peer= params.
func peersFromQuery(r *http.Request) []string {
	q := r.URL.Query()
	out := make([]string, 0, len(q["peer"]))
	for _, p := range q["peer"] {
		pu, err := url.Parse(p)
		if err != nil || (pu.Scheme != "http" && pu.Scheme != "https") || pu.Host == "" {
			continue
		}
		out = append(out, strings.TrimRight(p, "/"))
	}
	return out
}

// handleGCEligible fetches every peer's state plus the local one and reports
// which tags are safe to GC — a dry run of the coordinated decision.
func (s *Server) handleGCEligible(w http.ResponseWriter, r *http.Request) {
	peers := peersFromQuery(r)
	states := make([]orset.State, 0, len(peers)+1)
	states = append(states, s.Set.Snapshot())
	for _, p := range peers {
		st, err := s.fetchState(r.Context(), p)
		if err != nil {
			writeErr(w, http.StatusBadGateway, "peer "+p+": "+err.Error())
			return
		}
		states = append(states, st)
	}
	elig := gc.Eligible(states)
	count := 0
	for _, ts := range elig {
		count += len(ts)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"surveyed":      len(states),
		"eligibleTags":  elig,
		"eligibleCount": count,
	})
}

type gcReq struct {
	Peers []string `json:"peers"`
}

// handleGCRun performs a complete coordinated GC round, INCLUDING this
// replica: it fetches all peer states, intersects with local state, purges on
// every peer and locally. Any state-fetch failure aborts before any purge.
func (s *Server) handleGCRun(w http.ResponseWriter, r *http.Request) {
	var req gcReq
	if !decodeJSON(w, r, &req) {
		return
	}
	targets := make([]string, 0, len(req.Peers)+1)
	targets = append(targets, s.Set.ReplicaID()) // placeholder for the local slot
	targets = append(targets, req.Peers...)

	clients := make([]gc.Client, 0, len(targets))
	// local client operates directly on the replica, no HTTP self-call.
	clients = append(clients, localClient{set: s.Set})
	for _, p := range req.Peers {
		clients = append(clients, NewGCClient(p, s.HTTP))
	}

	rep, err := gc.Coordinate(r.Context(), clients, targets)
	if err != nil {
		writeErr(w, http.StatusBadGateway, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"eligible":      rep.Eligible,
		"purgedPerNode": rep.Purged,
		"totalPurged":   rep.TagCount,
		"stats":         s.Set.Stats(),
	})
}

// localClient implements gc.Client against the in-process replica.
type localClient struct{ set *orset.ORSet }

func (l localClient) State(context.Context) (orset.State, error) { return l.set.Snapshot(), nil }
func (l localClient) Purge(_ context.Context, tags map[string][]string) (int, error) {
	return l.set.GCTags(tags), nil
}

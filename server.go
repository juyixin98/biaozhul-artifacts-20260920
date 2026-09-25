package trmerge

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"path/filepath"
	"strings"
)

// Server wires the store and a HTTP JSON API. Pure backend, no frontend.
type Server struct {
	store    *Store
	workRoot string
}

// NewServer creates the API. workRoot defaults are handled by the caller.
func NewServer(store *Store, workRoot string) (*Server, error) {
	if err := store.EnsureSeparateFromWork(workRoot); err != nil {
		return nil, err
	}
	return &Server{store: store, workRoot: workRoot}, nil
}

// Handler builds the mux. Go 1.22 method+pattern routing.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.health)
	mux.HandleFunc("POST /v1/runs", s.createRun)
	mux.HandleFunc("GET /v1/runs", s.listRuns)
	mux.HandleFunc("GET /v1/runs/{id}", s.getRun)
	mux.HandleFunc("POST /v1/runs/{id}/events", s.postEvents)
	mux.HandleFunc("POST /v1/runs/{id}/finalize", s.finalizeRun)
	mux.HandleFunc("POST /v1/runs/{id}/cancel", s.cancelRun)
	mux.HandleFunc("GET /v1/runs/{id}/summary", s.getSummary)
	mux.HandleFunc("GET /v1/runs/{id}/events", s.getEvents)
	mux.HandleFunc("POST /v1/replay", s.replay)
	return mux
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type shardDef struct {
	ShardID string   `json:"shard_id"`
	TestIDs []string `json:"test_ids"`
}

type createRunReq struct {
	// Mode "events" (default): caller posts events. "execute": the service
	// runs the explicit commands supplied here.
	Mode     string        `json:"mode"`
	Shards   []shardDef    `json:"shards"`
	Commands []CommandSpec `json:"commands,omitempty"`
	WorkRoot string        `json:"work_root,omitempty"`
	// Finalize automatically when all execute-mode shards exit.
	AutoFinalize *bool `json:"auto_finalize,omitempty"`
}

type createRunResp struct {
	RunID    string   `json:"run_id"`
	Mode     string   `json:"mode"`
	Status   string   `json:"status"`
	WorkRoot string   `json:"work_root,omitempty"`
	Warnings []string `json:"warnings,omitempty"`
}

func (s *Server) createRun(w http.ResponseWriter, r *http.Request) {
	var req createRunReq
	if err := decodeBody(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if req.Mode == "" {
		req.Mode = "events"
	}

	seed := make([]ShardSeed, 0, len(req.Shards))
	for _, sd := range req.Shards {
		seed = append(seed, ShardSeed{ShardID: sd.ShardID, TestIDs: sd.TestIDs})
	}

	workRoot := s.workRoot
	if req.WorkRoot != "" {
		abs, err := filepath.Abs(filepath.Clean(req.WorkRoot))
		if err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
		if err := s.store.EnsureSeparateFromWork(abs); err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
		workRoot = abs
	}

	autoFinalize := false
	if req.AutoFinalize != nil {
		autoFinalize = *req.AutoFinalize
	}

	if req.Mode == "execute" {
		// Commands must be explicit. Manifest shards are optional but, when
		// both are given, command shard ids should be known to the manifest
		// (not strictly required — events can introduce extra shards).
		if len(req.Commands) == 0 {
			writeErr(w, http.StatusBadRequest, errors.New("execute mode requires explicit \"commands\""))
			return
		}
		if err := validateCommandsAgainstCache(s.store, req.Commands); err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
		if !autoFinalize {
			autoFinalize = true // execute runs finalize when all processes end
		}
	}

	meta, err := s.store.CreateRun(req.Mode, seed, workRoot, autoFinalize)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}

	resp := createRunResp{RunID: meta.ID, Mode: meta.Mode, WorkRoot: workRoot}

	if req.Mode == "execute" {
		// Per-run work directory is kept separate from the cache.
		runWork := filepath.Join(workRoot, safeName(meta.ID))
		rnr, err := NewRunner(s.store, meta.ID, runWork, req.Commands)
		if err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
		rnr.Start()
		active.Store(meta.ID, rnr)
		go func() {
			rnr.Wait(autoFinalize)
			active.Delete(meta.ID)
		}()
		resp.Status = "running"
	} else {
		resp.Status = "created"
	}
	writeJSON(w, http.StatusCreated, resp)
}

func validateCommandsAgainstCache(st *Store, cmds []CommandSpec) error {
	for _, c := range cmds {
		if c.WorkDir != "" {
			if err := st.EnsureSeparateFromWork(c.WorkDir); err != nil {
				return fmt.Errorf("command %s: %w", c.ShardID, err)
			}
		}
	}
	return nil
}

func (s *Server) listRuns(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"runs": s.store.List()})
}

func (s *Server) getRun(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	meta := s.store.Get(id)
	if meta == nil {
		writeErr(w, http.StatusNotFound, ErrNotFound)
		return
	}
	writeJSON(w, http.StatusOK, meta)
}

type eventsReq struct {
	Events []Event `json:"events"`
}

func (s *Server) postEvents(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if s.store.Get(id) == nil {
		writeErr(w, http.StatusNotFound, ErrNotFound)
		return
	}
	var req eventsReq
	if err := decodeBody(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if len(req.Events) == 0 {
		writeErr(w, http.StatusBadRequest, errors.New("\"events\" must be a non-empty array"))
		return
	}
	outcomes, err := s.store.Ingest(id, req.Events)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"results": outcomes})
}

func (s *Server) finalizeRun(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	applied, err := s.store.Finalize(id)
	if errors.Is(err, ErrNotFound) {
		writeErr(w, http.StatusNotFound, err)
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"finalized": true, "changed": applied})
}

func (s *Server) cancelRun(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if s.store.Get(id) == nil {
		writeErr(w, http.StatusNotFound, ErrNotFound)
		return
	}
	applied, err := s.store.RequestCancel(id)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	// Best-effort: also terminate running fixture processes.
	if rnr := active.Get(id); rnr != nil {
		rnr.Cancel()
	}
	writeJSON(w, http.StatusOK, map[string]any{"cancel_requested": true, "changed": applied})
}

func (s *Server) getSummary(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	sum, err := s.store.Summary(id)
	if errors.Is(err, ErrNotFound) {
		writeErr(w, http.StatusNotFound, err)
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, sum)
}

func (s *Server) getEvents(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	envs, err := s.store.ReadEvents(id)
	if errors.Is(err, ErrNotFound) {
		writeErr(w, http.StatusNotFound, err)
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": envs})
}

type replayReq struct {
	Shards  []shardDef `json:"shards"`
	Events  []Event    `json:"events"`
	Shuffle bool       `json:"shuffle"`
	Seed    int64      `json:"seed"`
	Repeats int        `json:"repeats"` // verify repeatability across shuffles
}

type replayResp struct {
	Summaries []Summary       `json:"summaries"`
	Results   []IngestOutcome `json:"results,omitempty"`
	Identical bool            `json:"identical"`
}

func (s *Server) replay(w http.ResponseWriter, r *http.Request) {
	var req replayReq
	if err := decodeBody(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	seed := make([]ShardSeed, 0, len(req.Shards))
	for _, sd := range req.Shards {
		seed = append(seed, ShardSeed{ShardID: sd.ShardID, TestIDs: sd.TestIDs})
	}
	if req.Repeats < 1 {
		req.Repeats = 1
	}
	if req.Repeats > 20 {
		req.Repeats = 20
	}
	resp := replayResp{}
	var canonical string
	for i := 0; i < req.Repeats; i++ {
		sum, results, err := Replay(seed, req.Events, req.Shuffle, req.Seed+int64(i))
		if err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
		resp.Summaries = append(resp.Summaries, sum)
		if i == 0 {
			resp.Results = results
		}
		b, _ := json.Marshal(sum)
		if i == 0 {
			canonical = string(b)
		} else if string(b) != canonical {
			resp.Identical = false
		}
	}
	resp.Identical = len(resp.Summaries) >= 1 && func() bool {
		b, _ := json.Marshal(resp.Summaries[0])
		for i := 1; i < len(resp.Summaries); i++ {
			c, _ := json.Marshal(resp.Summaries[i])
			if string(b) != string(c) {
				return false
			}
		}
		return true
	}()
	writeJSON(w, http.StatusOK, resp)
}

// --- helpers ---

func decodeBody(r *http.Request, v any) error {
	defer r.Body.Close()
	dec := json.NewDecoder(io.LimitReader(r.Body, 8<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return fmt.Errorf("invalid JSON body: %w", err)
	}
	if dec.More() {
		return errors.New("unexpected trailing JSON content")
	}
	return nil
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, err error) {
	writeJSON(w, code, map[string]string{"error": strings.TrimSpace(err.Error())})
}

// Package api implements the local JSON HTTP API of the provenance service.
// The service binds to 127.0.0.1 by default and performs no outbound network
// access of its own; it only executes tools explicitly declared in a
// project's build.json, from inside that project's directory.
package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"bis/internal/builder"
	"bis/internal/digest"
	"bis/internal/impact"
	"bis/internal/repro"
	"bis/internal/spec"
	"bis/internal/store"
	"bis/internal/verify"
)

// Service wires the store to the build/verify/impact/repro subsystems.
type Service struct {
	st      *store.Store
	timeout time.Duration
	now     func() time.Time
}

// NewService creates a Service over an open store.
func NewService(st *store.Store, timeout time.Duration) *Service {
	return &Service{st: st, timeout: timeout, now: time.Now}
}

type errResponse struct {
	Error string `json:"error"`
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, format string, args ...any) {
	writeJSON(w, code, errResponse{Error: fmt.Sprintf(format, args...)})
}

// Handler returns the HTTP mux.
func (s *Service) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /projects", s.listProjects)
	mux.HandleFunc("PUT /projects/{name}", s.createProject)
	mux.HandleFunc("POST /projects/{name}/build", s.buildProject)
	mux.HandleFunc("GET /projects/{name}/verify", s.verifyProject)
	mux.HandleFunc("POST /projects/{name}/reproduce", s.reproduceProject)
	mux.HandleFunc("GET /projects/{name}/impact", s.impactQuery)
	mux.HandleFunc("GET /projects/{name}/records", s.listRecords)
	mux.HandleFunc("GET /projects/{name}/records/{action}", s.getRecord)
	mux.HandleFunc("GET /projects/{name}/graph", s.getGraph)
	return mux
}

func (s *Service) loadSpec(w http.ResponseWriter, name string) *spec.Project {
	p, err := spec.Load(s.st.ProjectRoot(name))
	if err != nil {
		if errors.Is(err, spec.ErrCycle) {
			writeErr(w, http.StatusUnprocessableEntity, "%v", err)
			return nil
		}
		writeErr(w, http.StatusBadRequest, "load project %q: %v", name, err)
		return nil
	}
	return p
}

type createReq struct {
	SrcDir  string `json:"src_dir"`           // absolute path to a local fixture directory
	Replace bool   `json:"replace,omitempty"` // replace an existing project
}

func (s *Service) listProjects(w http.ResponseWriter, r *http.Request) {
	type item struct {
		Name    string `json:"name"`
		HasSpec bool   `json:"has_spec"`
		Records int    `json:"records"`
	}
	var out []item
	entries, err := readDirNames(s.st.ProjectsDir)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "%v", err)
		return
	}
	for _, name := range entries {
		it := item{Name: name}
		if p, err := spec.Load(s.st.ProjectRoot(name)); err == nil {
			it.HasSpec = true
			it.Records = len(s.st.LoadIndex(p.Name).Records)
		}
		out = append(out, it)
	}
	writeJSON(w, http.StatusOK, map[string]any{"projects": out})
}

func (s *Service) createProject(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	if !nameOK(name) {
		writeErr(w, http.StatusBadRequest, "invalid project name")
		return
	}
	var req createReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body: %v", err)
		return
	}
	if req.SrcDir == "" || !strings.HasPrefix(req.SrcDir, "/") {
		writeErr(w, http.StatusBadRequest, "src_dir must be an absolute path to a local fixture directory")
		return
	}
	dst := s.st.ProjectRoot(name)
	if _, err := spec.Load(dst); err == nil && !req.Replace {
		writeErr(w, http.StatusConflict, "project %q already exists; pass replace=true to overwrite", name)
		return
	}
	if err := importTree(req.SrcDir, dst, req.Replace); err != nil {
		writeErr(w, http.StatusBadRequest, "import project: %v", err)
		return
	}
	// Validate the imported spec before accepting the project.
	p, err := spec.Load(dst)
	if err != nil {
		_ = removeAll(dst)
		writeErr(w, http.StatusBadRequest, "imported build.json invalid: %v", err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"project": p.Name, "actions": len(p.Actions), "src_dir": req.SrcDir,
	})
}

func (s *Service) buildProject(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	p := s.loadSpec(w, name)
	if p == nil {
		return
	}
	b := builder.New(s.st, builder.WithTimeout(s.timeout))
	rep, err := b.Build(p)
	if rep == nil {
		writeErr(w, http.StatusUnprocessableEntity, "%v", err)
		return
	}
	code := http.StatusOK
	if err != nil {
		code = http.StatusUnprocessableEntity
	}
	writeJSON(w, code, rep)
}

func (s *Service) verifyProject(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	p := s.loadSpec(w, name)
	if p == nil {
		return
	}
	rep, err := verify.New(s.st).Verify(p)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "verify: %v", err)
		return
	}
	code := http.StatusOK
	if !rep.Complete {
		code = http.StatusConflict
	}
	writeJSON(w, code, rep)
}

func (s *Service) reproduceProject(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	p := s.loadSpec(w, name)
	if p == nil {
		return
	}
	rep, cleanup, err := repro.Run(s.st, p, s.timeout, s.now)
	defer cleanup()
	if err != nil {
		writeErr(w, http.StatusUnprocessableEntity, "reproduce: %v", err)
		return
	}
	code := http.StatusOK
	if !rep.Reproduced {
		code = http.StatusConflict
	}
	writeJSON(w, code, rep)
}

func (s *Service) impactQuery(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	p := s.loadSpec(w, name)
	if p == nil {
		return
	}
	q := r.URL.Query()
	path := q.Get("path")
	dg := q.Get("digest")
	if path == "" && dg == "" {
		writeErr(w, http.StatusBadRequest, "query requires path and/or digest")
		return
	}
	var want *digest.Digest
	if dg != "" {
		d, err := digest.Parse(dg)
		if err != nil {
			writeErr(w, http.StatusBadRequest, "invalid digest: %v", err)
			return
		}
		want = &d
	}
	rep := impact.Analyze(s.st, p, path, want)
	writeJSON(w, http.StatusOK, rep)
}

func (s *Service) listRecords(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	p := s.loadSpec(w, name)
	if p == nil {
		return
	}
	idx := s.st.LoadIndex(p.Name)
	writeJSON(w, http.StatusOK, idx)
}

func (s *Service) getRecord(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	action := r.PathValue("action")
	p := s.loadSpec(w, name)
	if p == nil {
		return
	}
	idx := s.st.LoadIndex(p.Name)
	ent, ok := idx.Records[action]
	if !ok {
		writeErr(w, http.StatusNotFound, "no record for action %q", action)
		return
	}
	rec, err := s.st.GetRecord(ent.RecordID)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "%v", err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"record_id": ent.RecordID, "record": rec})
}

func (s *Service) getGraph(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	p := s.loadSpec(w, name)
	if p == nil {
		return
	}
	idx := s.st.LoadIndex(p.Name)
	type node struct {
		ActionID  string   `json:"action_id"`
		Upstreams []string `json:"upstreams"`
		Outputs   []string `json:"outputs"`
		RecordID  string   `json:"record_id,omitempty"`
	}
	nodes := make([]node, 0, len(p.Actions))
	for i := range p.Actions {
		a := &p.Actions[i]
		n := node{ActionID: a.ID, Outputs: a.Outputs}
		for _, up := range a.Upstream {
			n.Upstreams = append(n.Upstreams, up)
		}
		if ent, ok := idx.Records[a.ID]; ok {
			n.RecordID = ent.RecordID.String()
		}
		nodes = append(nodes, n)
	}
	writeJSON(w, http.StatusOK, map[string]any{"project": p.Name, "nodes": nodes})
}

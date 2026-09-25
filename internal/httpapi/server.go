// Package httpapi exposes the build provenance service over a local JSON
// HTTP API. It binds to 127.0.0.1 by default; there is no cloud integration.
package httpapi

import (
	"encoding/json"
	"log"
	"net/http"

	"buildprovenance/internal/provenance"
	"buildprovenance/internal/service"
)

// Server wires the service to HTTP routes.
type Server struct {
	svc *service.Service
	mux *http.ServeMux
}

// NewServer builds the router. Requires Go 1.22+ method-pattern support.
func NewServer(svc *service.Service) *Server {
	s := &Server{svc: svc, mux: http.NewServeMux()}
	s.routes()
	return s
}

func (s *Server) Handler() http.Handler { return logRequests(s.mux) }

func (s *Server) routes() {
	m := s.mux
	m.HandleFunc("GET /v1/health", s.health)

	m.HandleFunc("POST /v1/sources", s.putSource)
	m.HandleFunc("GET /v1/sources", s.listSources)
	m.HandleFunc("GET /v1/sources/{path...}", s.getSource)

	m.HandleFunc("POST /v1/tools", s.registerTool)
	m.HandleFunc("GET /v1/tools", s.listTools)
	m.HandleFunc("GET /v1/tools/{name}", s.getTool)

	m.HandleFunc("POST /v1/actions", s.executeAction)

	m.HandleFunc("GET /v1/artifacts", s.listArtifacts)
	m.HandleFunc("GET /v1/artifacts/{id}", s.getArtifact)
	m.HandleFunc("GET /v1/artifacts/{id}/content", s.getArtifactContent)
	m.HandleFunc("POST /v1/artifacts/{id}/verify", s.verifyArtifact)
	m.HandleFunc("POST /v1/artifacts/{id}/reproduce", s.reproduceArtifact)
	m.HandleFunc("GET /v1/artifacts/{id}/provenance", s.provenance)

	m.HandleFunc("GET /v1/impact", s.impact)
	m.HandleFunc("GET /v1/records", s.listRecords)
	m.HandleFunc("GET /v1/records/{id}", s.getRecord)
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// ---- sources ----

type sourceRequest struct {
	Path    string `json:"path"`
	Content string `json:"content"` // raw string; fixture files are text in this project
}

func (s *Server) putSource(w http.ResponseWriter, r *http.Request) {
	var req sourceRequest
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "INVALID_BODY", err.Error())
		return
	}
	if req.Path == "" {
		writeError(w, http.StatusBadRequest, "INVALID_BODY", "path required")
		return
	}
	src, err := s.svc.RegisterSource(req.Path, []byte(req.Content))
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, src)
}

func (s *Server) listSources(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"sources": s.svc.ListSources()})
}

func (s *Server) getSource(w http.ResponseWriter, r *http.Request) {
	path := r.PathValue("path")
	src, err := s.svc.GetSource(path)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, src)
}

// ---- tools ----

func (s *Server) registerTool(w http.ResponseWriter, r *http.Request) {
	var def provenance.ToolDefinition
	if err := decodeBody(r, &def); err != nil {
		writeError(w, http.StatusBadRequest, "INVALID_BODY", err.Error())
		return
	}
	t, err := s.svc.RegisterTool(def)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, t)
}

func (s *Server) listTools(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"tools": s.svc.ListTools()})
}

func (s *Server) getTool(w http.ResponseWriter, r *http.Request) {
	t, err := s.svc.GetTool(r.PathValue("name"))
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, t)
}

// ---- actions ----

func (s *Server) executeAction(w http.ResponseWriter, r *http.Request) {
	var req provenance.ActionRequest
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "INVALID_BODY", err.Error())
		return
	}
	res, err := s.svc.ExecuteAction(r.Context(), req)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, res)
}

// ---- artifacts ----

func (s *Server) listArtifacts(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"artifacts": s.svc.ListArtifacts()})
}

func (s *Server) getArtifact(w http.ResponseWriter, r *http.Request) {
	a, err := s.svc.GetArtifact(r.PathValue("id"))
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, a)
}

func (s *Server) getArtifactContent(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	a, err := s.svc.GetArtifact(id)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	b, err := s.svc.ReadArtifactBytes(id)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("X-Content-Digest", string(a.Digest))
	w.Write(b)
}

func (s *Server) verifyArtifact(w http.ResponseWriter, r *http.Request) {
	rep, err := s.svc.Verify(r.PathValue("id"))
	if err != nil {
		writeServiceError(w, err)
		return
	}
	status := http.StatusOK
	if !rep.Complete {
		status = http.StatusUnprocessableEntity
	}
	writeJSON(w, status, rep)
}

func (s *Server) reproduceArtifact(w http.ResponseWriter, r *http.Request) {
	rep, err := s.svc.Reproduce(r.Context(), r.PathValue("id"))
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

func (s *Server) provenance(w http.ResponseWriter, r *http.Request) {
	tree, err := s.svc.ProvenanceTree(r.PathValue("id"))
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, tree)
}

// ---- impact / records ----

func (s *Server) impact(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Query().Get("source")
	if path == "" {
		writeError(w, http.StatusBadRequest, "MISSING_PARAM", "query param ?source=<path> required")
		return
	}
	rep, err := s.svc.Impact(path, r.URL.Query().Get("digest"))
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

func (s *Server) listRecords(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"records": s.svc.ListRecords(), "tail": s.svc.LogTail()})
}

func (s *Server) getRecord(w http.ResponseWriter, r *http.Request) {
	rec, err := s.svc.GetRecord(r.PathValue("id"))
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rec)
}

// ---- plumbing ----

func decodeBody(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	return dec.Decode(v)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		log.Printf("write response: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]string{"error": code, "message": msg})
}

func writeServiceError(w http.ResponseWriter, err error) {
	if isNotFound(err) {
		writeError(w, http.StatusNotFound, "NOT_FOUND", err.Error())
		return
	}
	// Execution/validation problems are client errors (declared command
	// failed, disallowed command, malformed request, conflicts...).
	writeError(w, http.StatusBadRequest, "REQUEST_FAILED", err.Error())
}

func isNotFound(err error) bool {
	return err == provenance.ErrNotFound || contains(err.Error(), "not found")
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (indexOf(s, sub) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

func logRequests(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		h.ServeHTTP(rw, r)
		log.Printf("%s %s -> %d", r.Method, r.URL.RequestURI(), rw.status)
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

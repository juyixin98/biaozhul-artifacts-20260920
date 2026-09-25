// HTTP API for the build service:
//
//	GET  /healthz
//	GET  /v1/fixtures                 list the operator allowlist
//	POST /v1/builds                   start a fixture run
//	GET  /v1/builds                   list jobs
//	GET  /v1/builds/{id}              get a job
//	GET  /v1/builds/{id}?wait=30s     long-poll until terminal or timeout
package buildsvc

import (
	"encoding/json"
	"log"
	"net/http"
	"strings"
	"time"
)

// HTTPServer exposes the build service over JSON.
type HTTPServer struct {
	svc    *Service
	logger *log.Logger
}

// NewHTTPServer builds the HTTP wrapper.
func NewHTTPServer(svc *Service, logger *log.Logger) *HTTPServer {
	if logger == nil {
		logger = log.Default()
	}
	return &HTTPServer{svc: svc, logger: logger}
}

func (h *HTTPServer) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", h.health)
	mux.HandleFunc("/v1/fixtures", h.listFixtures)
	mux.HandleFunc("/v1/builds", h.buildsRoot)
	mux.HandleFunc("/v1/builds/", h.buildItem)
	return withLogging(h.logger, mux)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]string{"error": msg, "code": code})
}

func (h *HTTPServer) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (h *HTTPServer) listFixtures(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeErr(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET")
		return
	}
	type fxView struct {
		Name        string   `json:"name"`
		Description string   `json:"description,omitempty"`
		Outputs     []string `json:"outputs"`
		Timeout     string   `json:"timeout,omitempty"`
	}
	out := make([]fxView, 0, len(h.svc.opts.Manifest.Fixtures))
	for _, f := range h.svc.opts.Manifest.Fixtures {
		v := fxView{Name: f.Name, Description: f.Description, Outputs: f.Outputs}
		if f.Timeout.Std() > 0 {
			v.Timeout = f.Timeout.Std().String()
		}
		out = append(out, v)
	}
	writeJSON(w, http.StatusOK, map[string]any{"fixtures": out})
}

type startBuildRequest struct {
	Fixture string            `json:"fixture"`
	Params  map[string]string `json:"params,omitempty"`
}

func (h *HTTPServer) buildsRoot(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		writeJSON(w, http.StatusOK, map[string]any{"jobs": h.svc.ListJobViews()})
	case http.MethodPost:
		var req startBuildRequest
		dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
		if err := dec.Decode(&req); err != nil {
			writeErr(w, http.StatusBadRequest, "bad_json", "invalid JSON body")
			return
		}
		if req.Fixture == "" {
			writeErr(w, http.StatusBadRequest, "missing_fixture", "fixture is required")
			return
		}
		j, err := h.svc.StartBuild(req.Fixture, req.Params)
		if err != nil {
			writeErr(w, http.StatusBadRequest, "unknown_fixture", err.Error())
			return
		}
		writeJSON(w, http.StatusAccepted, j.Snapshot())
	default:
		writeErr(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET or POST")
	}
}

func (h *HTTPServer) buildItem(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/v1/builds/")
	if id == "" || strings.Contains(id, "/") {
		writeErr(w, http.StatusNotFound, "not_found", "unknown job")
		return
	}
	j, ok := h.svc.GetJob(id)
	if !ok {
		writeErr(w, http.StatusNotFound, "not_found", "job not found")
		return
	}
	if r.Method != http.MethodGet {
		writeErr(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET")
		return
	}
	if wait := r.URL.Query().Get("wait"); wait != "" {
		d, err := time.ParseDuration(wait)
		if err != nil || d < 0 || d > 10*time.Minute {
			writeErr(w, http.StatusBadRequest, "bad_wait", "wait must be 0..10m duration, e.g. 30s")
			return
		}
		if d > 0 {
			ctx, cancel := contextWithTimeout(r.Context(), d)
			defer cancel()
			_ = j.Wait(ctx)
		}
	}
	writeJSON(w, http.StatusOK, j.Snapshot())
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (s *statusWriter) WriteHeader(c int) {
	s.status = c
	s.ResponseWriter.WriteHeader(c)
}

func withLogging(logger *log.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w}
		next.ServeHTTP(sw, r)
		if sw.status == 0 {
			sw.status = http.StatusOK
		}
		logger.Printf("%s %s -> %d %s", r.Method, r.URL.RequestURI(), sw.status, time.Since(start).Round(time.Millisecond))
	})
}

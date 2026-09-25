// Package server exposes the batch patch applier over a small local HTTP
// JSON API. The service never reaches the network beyond its own listener and
// keeps its staging cache separate from the work directories it patches.
package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sync"

	"patchd/internal/batch"
)

// Config configures the HTTP handler.
type Config struct {
	CacheDir string // staging directory; must not coincide with a workdir
	MaxBytes int64  // max request body size
}

// Server holds shared state.
type Server struct {
	cfg   Config
	mu    sync.Mutex
	locks map[string]*sync.Mutex // per-workdir serialization
}

// New constructs a server.
func New(cfg Config) (*Server, error) {
	if cfg.CacheDir == "" {
		return nil, errors.New("cache directory is required")
	}
	if cfg.MaxBytes == 0 {
		cfg.MaxBytes = 64 << 20
	}
	if err := os.MkdirAll(cfg.CacheDir, 0o700); err != nil {
		return nil, err
	}
	return &Server{cfg: cfg, locks: map[string]*sync.Mutex{}}, nil
}

// Handler returns the HTTP mux.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.health)
	mux.HandleFunc("/v1/apply", s.apply)
	return mux
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type applyRequest struct {
	Workdir string          `json:"workdir"`
	DryRun  bool            `json:"dry_run"`
	Patches []batch.Request `json:"patches"`
}

func (s *Server) apply(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use POST")
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, s.cfg.MaxBytes)
	body, err := io.ReadAll(r.Body)
	if err != nil {
		var mbErr *http.MaxBytesError
		if errors.As(err, &mbErr) {
			writeError(w, http.StatusRequestEntityTooLarge, "request_too_large",
				fmt.Sprintf("body exceeds limit of %d bytes", s.cfg.MaxBytes))
			return
		}
		writeError(w, http.StatusBadRequest, "unreadable_body", err.Error())
		return
	}
	var req applyRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", err.Error())
		return
	}
	if req.Workdir == "" {
		writeError(w, http.StatusBadRequest, "invalid_request", `"workdir" is required`)
		return
	}
	absWork, err := filepath.Abs(req.Workdir)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	fi, err := os.Stat(absWork)
	if err != nil {
		writeError(w, http.StatusBadRequest, "root_not_found", err.Error())
		return
	}
	if !fi.IsDir() {
		writeError(w, http.StatusBadRequest, "invalid_root", "workdir is not a directory")
		return
	}

	// Serialize batches touching the same work directory.
	lock := s.lockFor(absWork)
	lock.Lock()
	defer lock.Unlock()

	cacheDir := filepath.Join(s.cfg.CacheDir, safeDirName(absWork))
	plan, err := batch.PlanBatch(absWork, cacheDir, req.Patches)
	if err != nil {
		writeFailure(w, http.StatusUnprocessableEntity, err)
		return
	}
	if req.DryRun {
		writeJSON(w, http.StatusOK, map[string]any{
			"status":  "validated",
			"results": plan.Results,
		})
		return
	}
	if err := plan.Commit(); err != nil {
		writeFailure(w, http.StatusConflict, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":  "applied",
		"results": plan.Results,
	})
}

func (s *Server) lockFor(absWork string) *sync.Mutex {
	s.mu.Lock()
	defer s.mu.Unlock()
	if m, ok := s.locks[absWork]; ok {
		return m
	}
	m := &sync.Mutex{}
	s.locks[absWork] = m
	return m
}

func safeDirName(p string) string {
	out := make([]byte, 0, len(p))
	for i := 0; i < len(p); i++ {
		c := p[i]
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9', c == '-', c == '_':
			out = append(out, c)
		default:
			out = append(out, '_')
		}
	}
	return string(out)
}

type errBody struct {
	Status string         `json:"status"`
	Error  map[string]any `json:"error"`
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, errBody{Status: "failed", Error: map[string]any{"code": code, "message": msg}})
}

func writeFailure(w http.ResponseWriter, status int, err error) {
	body := errBody{Status: "failed", Error: map[string]any{"code": "batch_failed", "message": err.Error()}}
	var fe *batch.Failure
	if errors.As(err, &fe) {
		body.Error = map[string]any{
			"code":    fe.Code,
			"message": fe.Message,
			"index":   fe.Index,
		}
		if fe.Path != "" {
			body.Error["path"] = fe.Path
		}
		if fe.Hunk != 0 {
			body.Error["hunk"] = fe.Hunk
		}
		if fe.Line != 0 {
			body.Error["line"] = fe.Line
		}
	}
	writeJSON(w, status, body)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

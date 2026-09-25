// Package server exposes the patch service over a local JSON/HTTP API.
//
// Endpoints:
//
//	GET  /v1/healthz  - liveness probe
//	POST /v1/apply    - validate and atomically apply a batch of patches
//	POST /v1/test     - run exactly the test-fixture command the caller
//	                    supplies (no shell, no implicit commands)
//
// The service never talks to any cloud platform. All staging state lives in
// the cache directory given at startup, which must be separate from every
// work directory it serves.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"patchsvc/internal/apply"
)

const maxCapturedOutput = 1 << 20 // 1 MiB per stream

// Server is the HTTP service. CacheDir is the only place it writes outside
// the caller-provided work directories.
type Server struct {
	CacheDir string
}

// New prepares the cache directory and returns the server.
func New(cacheDir string) (*Server, error) {
	if cacheDir == "" {
		return nil, errors.New("cache directory must not be empty")
	}
	abs, err := filepath.Abs(cacheDir)
	if err != nil {
		return nil, fmt.Errorf("cache directory: %w", err)
	}
	if err := os.MkdirAll(abs, 0o755); err != nil {
		return nil, fmt.Errorf("cache directory: %w", err)
	}
	return &Server{CacheDir: abs}, nil
}

// Handler returns the HTTP handler with all routes registered.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/healthz", s.handleHealth)
	mux.HandleFunc("POST /v1/apply", s.handleApply)
	mux.HandleFunc("POST /v1/test", s.handleTest)
	return mux
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "cache_dir": s.CacheDir})
}

// --- /v1/apply ---

type applyRequest struct {
	Workdir string   `json:"workdir"`
	Patch   string   `json:"patch"`
	Patches []string `json:"patches"`
}

type changeInfo struct {
	Path   string `json:"path"`
	Action string `json:"action"`
}

type applyResponse struct {
	OK      bool         `json:"ok"`
	Changed []changeInfo `json:"changed,omitempty"`
	Error   string       `json:"error,omitempty"`
}

func (s *Server) handleApply(w http.ResponseWriter, r *http.Request) {
	var req applyRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, applyResponse{Error: "invalid JSON body: " + err.Error()})
		return
	}
	workdir, err := s.resolveWorkdir(req.Workdir)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, applyResponse{Error: err.Error()})
		return
	}

	var patches []string
	if req.Patch != "" {
		patches = append(patches, req.Patch)
	}
	patches = append(patches, req.Patches...)
	if len(patches) == 0 {
		writeJSON(w, http.StatusBadRequest, applyResponse{Error: "provide \"patch\" or \"patches\""})
		return
	}

	changes, err := apply.Plan(workdir, patches)
	if err != nil {
		// Validation failure: nothing on disk was touched.
		writeJSON(w, http.StatusUnprocessableEntity, applyResponse{Error: err.Error()})
		return
	}
	if err := apply.Publish(workdir, changes, s.CacheDir); err != nil {
		writeJSON(w, http.StatusInternalServerError, applyResponse{Error: err.Error()})
		return
	}

	resp := applyResponse{OK: true}
	for _, ch := range changes {
		resp.Changed = append(resp.Changed, changeInfo{Path: ch.Path, Action: string(ch.Kind)})
	}
	writeJSON(w, http.StatusOK, resp)
}

// --- /v1/test ---

type testRequest struct {
	Workdir        string   `json:"workdir"`
	Command        []string `json:"command"`
	TimeoutSeconds int      `json:"timeout_seconds"`
}

type testResponse struct {
	OK         bool   `json:"ok"`
	ExitCode   int    `json:"exit_code"`
	Stdout     string `json:"stdout"`
	Stderr     string `json:"stderr"`
	DurationMs int64  `json:"duration_ms"`
	TimedOut   bool   `json:"timed_out,omitempty"`
	Error      string `json:"error,omitempty"`
}

// handleTest runs exactly the argv the caller supplied, directly via
// exec.Command - never through a shell - and only that. The service invents
// no commands of its own.
func (s *Server) handleTest(w http.ResponseWriter, r *http.Request) {
	var req testRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, testResponse{Error: "invalid JSON body: " + err.Error()})
		return
	}
	workdir, err := s.resolveWorkdir(req.Workdir)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, testResponse{Error: err.Error()})
		return
	}
	if len(req.Command) == 0 || strings.TrimSpace(req.Command[0]) == "" {
		writeJSON(w, http.StatusBadRequest, testResponse{Error: "\"command\" must be a non-empty argv array"})
		return
	}
	timeout := time.Duration(req.TimeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = 60 * time.Second
	}
	if timeout > 10*time.Minute {
		timeout = 10 * time.Minute
	}

	ctx, cancel := context.WithTimeout(r.Context(), timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, req.Command[0], req.Command[1:]...)
	cmd.Dir = workdir
	cmd.Env = append(os.Environ(), "PATCHSVC_CACHE_DIR="+s.CacheDir)
	var stdout, stderr cappedBuffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	start := time.Now()
	runErr := cmd.Run()
	resp := testResponse{
		Stdout:     stdout.String(),
		Stderr:     stderr.String(),
		DurationMs: time.Since(start).Milliseconds(),
	}
	switch {
	case ctx.Err() == context.DeadlineExceeded:
		resp.TimedOut = true
		resp.ExitCode = -1
		resp.Error = fmt.Sprintf("command timed out after %s", timeout)
		writeJSON(w, http.StatusOK, resp)
	case runErr == nil:
		resp.OK = true
		resp.ExitCode = 0
		writeJSON(w, http.StatusOK, resp)
	default:
		var exitErr *exec.ExitError
		if errors.As(runErr, &exitErr) {
			resp.ExitCode = exitErr.ExitCode()
		} else {
			resp.ExitCode = -1
			resp.Error = runErr.Error()
		}
		writeJSON(w, http.StatusOK, resp)
	}
}

// resolveWorkdir validates the caller-supplied work directory: it must be an
// absolute path to an existing directory, and it must be separate from the
// cache directory (neither may contain the other).
func (s *Server) resolveWorkdir(workdir string) (string, error) {
	if workdir == "" {
		return "", errors.New("\"workdir\" is required")
	}
	if !filepath.IsAbs(workdir) {
		return "", fmt.Errorf("workdir %q must be an absolute path", workdir)
	}
	abs := filepath.Clean(workdir)
	info, err := os.Stat(abs)
	if err != nil {
		return "", fmt.Errorf("workdir: %w", err)
	}
	if !info.IsDir() {
		return "", fmt.Errorf("workdir %q is not a directory", abs)
	}
	if rel, err := filepath.Rel(abs, s.CacheDir); err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		return "", fmt.Errorf("cache directory %q must not be inside the work directory", s.CacheDir)
	}
	if rel, err := filepath.Rel(s.CacheDir, abs); err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		return "", fmt.Errorf("work directory %q must not be inside the cache directory", abs)
	}
	return abs, nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// cappedBuffer is a bytes.Buffer that stops growing after maxCapturedOutput
// bytes, discarding the rest.
type cappedBuffer struct {
	buf       []byte
	truncated bool
}

func (c *cappedBuffer) Write(p []byte) (int, error) {
	remaining := maxCapturedOutput - len(c.buf)
	if remaining > 0 {
		if len(p) > remaining {
			c.buf = append(c.buf, p[:remaining]...)
			c.truncated = true
		} else {
			c.buf = append(c.buf, p...)
		}
	} else {
		c.truncated = true
	}
	return len(p), nil
}

func (c *cappedBuffer) String() string {
	if c.truncated {
		return string(c.buf) + "\n...[truncated]"
	}
	return string(c.buf)
}

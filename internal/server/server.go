// Package server exposes the artifact delta-update service over a local JSON
// HTTP API. It never reaches out to any network service other than its own
// listener; all artifacts live in a local content-addressed cache and all
// targets live under a local working directory root.
package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"deltaupdate/internal/apply"
	"deltaupdate/internal/delta"
	"deltaupdate/internal/store"
)

// Config configures Server.
type Config struct {
	CacheDir string // content-addressed cache root (separate from WorkDir)
	WorkDir  string // root for target artifacts
	// MaxUploadBytes caps artifact upload and patch request bodies.
	MaxUploadBytes int64
}

// Server holds dependencies.
type Server struct {
	cfg Config
	st  *store.Store
	mux *http.ServeMux
}

const defaultMaxUpload = 512 << 20 // 512 MiB

// New constructs a server, creating cache/work directories as needed.
func New(cfg Config) (*Server, error) {
	if cfg.CacheDir == "" || cfg.WorkDir == "" {
		return nil, errors.New("server: CacheDir and WorkDir are required")
	}
	if filepath.Clean(cfg.CacheDir) == filepath.Clean(cfg.WorkDir) {
		return nil, errors.New("server: cache dir must be separate from work dir")
	}
	st, err := store.Open(cfg.CacheDir)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(cfg.WorkDir, 0o755); err != nil {
		return nil, fmt.Errorf("server: create work dir: %w", err)
	}
	if cfg.MaxUploadBytes <= 0 {
		cfg.MaxUploadBytes = defaultMaxUpload
	}
	s := &Server{cfg: cfg, st: st, mux: http.NewServeMux()}
	s.routes()
	return s, nil
}

// Handler returns the HTTP handler.
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("GET /v1/health", s.handleHealth)
	s.mux.HandleFunc("POST /v1/artifacts", s.handleUpload)
	s.mux.HandleFunc("GET /v1/artifacts/{hash}", s.handleGetArtifact)
	s.mux.HandleFunc("POST /v1/deltas", s.handleDelta)
	s.mux.HandleFunc("POST /v1/apply", s.handleApply)
	s.mux.HandleFunc("GET /v1/work/{path...}", s.handleWorkGet)
	s.mux.HandleFunc("PUT /v1/work/{path...}", s.handleWorkPut)
}

// ---- JSON helpers ----

type errBody struct {
	Error struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code, msg string) {
	var b errBody
	b.Error.Code = code
	b.Error.Message = msg
	writeJSON(w, status, b)
}

// classify maps domain errors to (http status, error code).
func classify(err error) (int, string) {
	switch {
	case errors.Is(err, apply.ErrWrongBase):
		return http.StatusConflict, "wrong_base"
	case errors.Is(err, apply.ErrCorruptPatch):
		return http.StatusUnprocessableEntity, "corrupt_patch"
	case errors.Is(err, apply.ErrNoSpace):
		return http.StatusInsufficientStorage, "insufficient_space"
	case errors.Is(err, apply.ErrLocked):
		return http.StatusConflict, "locked"
	case errors.Is(err, apply.ErrInjected):
		return http.StatusInternalServerError, "injected_failure"
	default:
		return http.StatusBadRequest, "bad_request"
	}
}

// safeWorkPath resolves a user-supplied relative path inside WorkDir,
// rejecting traversal escapes.
func (s *Server) safeWorkPath(rel string) (string, error) {
	rel = filepath.Clean("/" + strings.ReplaceAll(rel, "\\", "/"))
	// rel is now absolute with work root as '/'; strip leading slash.
	rel = strings.TrimPrefix(filepath.ToSlash(rel), "/")
	if rel == "" || rel == "." {
		return "", errors.New("empty path")
	}
	full := filepath.Join(s.cfg.WorkDir, filepath.FromSlash(rel))
	if !isInside(s.cfg.WorkDir, full) {
		return "", errors.New("path escapes work directory")
	}
	return full, nil
}

func isInside(base, target string) bool {
	rel, err := filepath.Rel(base, target)
	if err != nil {
		return false
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator)) && !filepath.IsAbs(rel)
}

// ---- handlers ----

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type uploadResp struct {
	Hash string `json:"hash"`
	Size int64  `json:"size"`
}

// POST /v1/artifacts  (raw application/octet-stream body)
func (s *Server) handleUpload(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, s.cfg.MaxUploadBytes)
	h, err := s.st.Put(r.Body, "")
	if err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			writeErr(w, http.StatusRequestEntityTooLarge, "too_large", "artifact exceeds upload limit")
			return
		}
		writeErr(w, http.StatusInternalServerError, "store_error", err.Error())
		return
	}
	size, _ := s.st.Stat(h)
	writeJSON(w, http.StatusCreated, uploadResp{Hash: h, Size: size})
}

// GET /v1/artifacts/{hash}
func (s *Server) handleGetArtifact(w http.ResponseWriter, r *http.Request) {
	hash := r.PathValue("hash")
	rc, err := s.st.OpenForRead(hash)
	if err != nil {
		writeErr(w, http.StatusNotFound, "not_found", "artifact not in cache")
		return
	}
	defer rc.Close()
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("X-Content-Sha256", hash)
	_, _ = io.Copy(w, rc)
}

type deltaReq struct {
	OldRef    string `json:"oldRef"`
	NewRef    string `json:"newRef"`
	BlockSize int    `json:"blockSize,omitempty"`
}

type deltaResp struct {
	Patch *delta.Patch `json:"patch"`
}

// POST /v1/deltas
func (s *Server) handleDelta(w http.ResponseWriter, r *http.Request) {
	var req deltaReq
	if err := decodeJSONLimit(w, r, &req, 4<<20); err != nil {
		writeErr(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	oldR, oldSize, err := s.st.Resolve(req.OldRef)
	if err != nil {
		writeErr(w, http.StatusNotFound, "old_not_found", err.Error())
		return
	}
	defer oldR.Close()
	newR, newSize, err := s.st.Resolve(req.NewRef)
	if err != nil {
		writeErr(w, http.StatusNotFound, "new_not_found", err.Error())
		return
	}
	defer newR.Close()

	bs := req.BlockSize
	if bs == 0 {
		bs = delta.DefaultBlockSize
	}
	p, err := delta.Generate(oldR, oldSize, newR, newSize, bs)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "delta_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, deltaResp{Patch: p})
}

type applyReq struct {
	Target string       `json:"target"` // relative path under WorkDir
	Patch  *delta.Patch `json:"patch"`
	// Fixture-only fault injection (in-process recoverable failures). Hard
	// crashes are exercised through the CLI, never over HTTP.
	FailStage string `json:"failStage,omitempty"`
	// SimFreeBytes, when non-nil, forces the space preflight to see the given
	// number of free bytes (fixture for space-shortage acceptance).
	SimFreeBytes *int64 `json:"simFreeBytes,omitempty"`
}

type applyResp struct {
	Applied   bool   `json:"applied"`
	OldDigest string `json:"oldDigest"`
	NewDigest string `json:"newDigest"`
}

// POST /v1/apply
func (s *Server) handleApply(w http.ResponseWriter, r *http.Request) {
	var req applyReq
	if err := decodeJSONLimit(w, r, &req, s.cfg.MaxUploadBytes); err != nil {
		writeErr(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	if req.Patch == nil {
		writeErr(w, http.StatusBadRequest, "bad_request", "missing patch")
		return
	}
	target, err := s.safeWorkPath(req.Target)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad_target", err.Error())
		return
	}

	// Open existing target read-only (if any).
	var oldR io.ReadSeeker
	if fi, statErr := os.Stat(target); statErr == nil {
		if fi.IsDir() {
			writeErr(w, http.StatusBadRequest, "bad_target", "target is a directory")
			return
		}
		f, oerr := os.Open(target)
		if oerr != nil {
			writeErr(w, http.StatusInternalServerError, "io_error", oerr.Error())
			return
		}
		defer f.Close()
		oldR = f
	} else if !os.IsNotExist(statErr) {
		writeErr(w, http.StatusInternalServerError, "io_error", statErr.Error())
		return
	}

	opts := apply.Options{
		Fault: apply.FaultPolicy{FailStage: req.FailStage},
	}
	if req.SimFreeBytes != nil {
		opts.Space = constSpace(*req.SimFreeBytes)
	}

	res, applyErr := apply.Apply(req.Patch, oldR, target, opts)
	if applyErr != nil && !apply.AlreadyApplied(applyErr) {
		status, code := classify(applyErr)
		writeErr(w, status, code, applyErr.Error())
		return
	}
	// AlreadyApplied is treated as success with applied=false.
	if res == nil {
		writeErr(w, http.StatusInternalServerError, "internal", "nil result")
		return
	}
	writeJSON(w, http.StatusOK, applyResp{
		Applied:   res.Applied,
		OldDigest: res.OldDigest,
		NewDigest: res.NewDigest,
	})
}

// GET /v1/work/{path...} — inspect a target artifact (digest, size).
func (s *Server) handleWorkGet(w http.ResponseWriter, r *http.Request) {
	target, err := s.safeWorkPath(r.PathValue("path"))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad_target", err.Error())
		return
	}
	f, err := os.Open(target)
	if err != nil {
		writeErr(w, http.StatusNotFound, "not_found", err.Error())
		return
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "io_error", err.Error())
		return
	}
	sum, err := delta.DigestReader(f)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "io_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"path":   r.PathValue("path"),
		"size":   fi.Size(),
		"sha256": sum,
	})
}

// PUT /v1/work/{path...} — seed a target artifact with raw bytes (fixtures/demo).
func (s *Server) handleWorkPut(w http.ResponseWriter, r *http.Request) {
	target, err := s.safeWorkPath(r.PathValue("path"))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad_target", err.Error())
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, s.cfg.MaxUploadBytes)
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		writeErr(w, http.StatusInternalServerError, "io_error", err.Error())
		return
	}
	tmp := target + ".seed.tmp"
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if err != nil {
		writeErr(w, http.StatusConflict, "temp_exists", "seed temp exists; retry")
		return
	}
	h := newHasher()
	if _, err := io.Copy(io.MultiWriter(f, h), r.Body); err != nil {
		f.Close()
		os.Remove(tmp)
		writeErr(w, http.StatusInternalServerError, "io_error", err.Error())
		return
	}
	f.Sync()
	f.Close()
	if err := os.Rename(tmp, target); err != nil {
		os.Remove(tmp)
		writeErr(w, http.StatusInternalServerError, "io_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"sha256": h.hex()})
}

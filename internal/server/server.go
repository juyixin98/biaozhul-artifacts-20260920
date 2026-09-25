// Package server exposes the local artifact cache and the fixture-command
// runner over a JSON HTTP API. It talks only to local disk; it never
// connects to any cloud service.
package server

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"sync"

	"localcache/api"
	"localcache/internal/builder"
	"localcache/internal/cas"
)

// Server wires the CAS, the action cache and the build executor into HTTP
// handlers.
type Server struct {
	store *cas.Store
	exec  *builder.Executor
	acDir string
	mux   *http.ServeMux

	inflight sync.Map // action digest -> *sync.Mutex: dedups concurrent identical builds
}

// New creates a Server. cacheRoot is the CAS root; the action cache lives
// in an "ac" subdirectory next to "objects".
func New(store *cas.Store, exec *builder.Executor, cacheRoot string) (*Server, error) {
	acDir := filepath.Join(cacheRoot, "ac")
	if err := os.MkdirAll(acDir, 0o755); err != nil {
		return nil, err
	}
	s := &Server{store: store, exec: exec, acDir: acDir, mux: http.NewServeMux()}
	s.mux.HandleFunc("GET /v1/health", s.handleHealth)
	s.mux.HandleFunc("PUT /v1/cas/{digest}", s.handlePut)
	s.mux.HandleFunc("GET /v1/cas/{digest}", s.handleGet)
	s.mux.HandleFunc("HEAD /v1/cas/{digest}", s.handleHead)
	s.mux.HandleFunc("POST /v1/builds", s.handleBuild)
	s.mux.HandleFunc("GET /v1/admin/stats", s.handleStats)
	s.mux.HandleFunc("GET /v1/admin/fsck", s.handleFsck)
	return s, nil
}

// Handler returns the root HTTP handler.
func (s *Server) Handler() http.Handler { return s.mux }

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, api.ErrorResponse{Error: msg})
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handlePut(w http.ResponseWriter, r *http.Request) {
	digest := r.PathValue("digest")
	if err := cas.ValidateDigest(digest); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if size, ok := s.store.Has(digest); ok {
		// Duplicate upload: drain the body so the connection stays usable,
		// and report the already-published object.
		io.Copy(io.Discard, http.MaxBytesReader(w, r.Body, s.store.MaxSize()+1))
		writeJSON(w, http.StatusOK, api.PutResponse{Digest: digest, Size: size, Dedup: true})
		return
	}
	size, err := s.store.Put(digest, http.MaxBytesReader(w, r.Body, s.store.MaxSize()+1))
	if err != nil {
		switch {
		case errors.Is(err, cas.ErrTooLarge):
			writeError(w, http.StatusRequestEntityTooLarge, err.Error())
		case errors.Is(err, cas.ErrDigestMismatch), errors.Is(err, cas.ErrInvalidDigest):
			writeError(w, http.StatusBadRequest, err.Error())
		default:
			writeError(w, http.StatusInternalServerError, err.Error())
		}
		return
	}
	writeJSON(w, http.StatusCreated, api.PutResponse{Digest: digest, Size: size})
}

func (s *Server) handleGet(w http.ResponseWriter, r *http.Request) {
	digest := r.PathValue("digest")
	rc, size, err := s.store.Get(digest)
	if err != nil {
		status := http.StatusInternalServerError
		switch {
		case errors.Is(err, cas.ErrNotFound):
			status = http.StatusNotFound
		case errors.Is(err, cas.ErrInvalidDigest):
			status = http.StatusBadRequest
		}
		writeError(w, status, err.Error())
		return
	}
	defer rc.Close()
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Length", strconv.FormatInt(size, 10))
	w.Header().Set("ETag", `"`+digest+`"`)
	w.WriteHeader(http.StatusOK)
	if _, err := io.Copy(w, rc); err != nil {
		// Client went away mid-download; nothing was published or mutated,
		// so there is nothing to roll back — just note it.
		log.Printf("download of %s interrupted: %v", digest, err)
	}
}

func (s *Server) handleHead(w http.ResponseWriter, r *http.Request) {
	digest := r.PathValue("digest")
	size, ok := s.store.Has(digest)
	if !ok {
		w.WriteHeader(http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Length", strconv.FormatInt(size, 10))
	w.Header().Set("ETag", `"`+digest+`"`)
	w.WriteHeader(http.StatusOK)
}

// ActionDigest computes the action-cache key for a build request: the
// SHA-256 of the canonical JSON of {argv, inputs, sorted outputs,
// timeout_ms}.
func ActionDigest(req *api.BuildRequest) (string, error) {
	if len(req.Argv) == 0 {
		return "", fmt.Errorf("argv must not be empty")
	}
	outputs := append([]string(nil), req.Outputs...)
	sort.Strings(outputs)
	key := struct {
		Argv      []string          `json:"argv"`
		Inputs    map[string]string `json:"inputs,omitempty"`
		Outputs   []string          `json:"outputs,omitempty"`
		TimeoutMs int               `json:"timeout_ms,omitempty"`
	}{req.Argv, req.Inputs, outputs, req.TimeoutMs}
	data, err := json.Marshal(key) // Go marshals map keys in sorted order
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:]), nil
}

func (s *Server) acPath(actionDigest string) string {
	return filepath.Join(s.acDir, actionDigest+".json")
}

// storeAC publishes an action-cache entry atomically (temp file + rename),
// the same discipline as CAS object uploads.
func (s *Server) storeAC(actionDigest string, res *api.BuildResult) error {
	data, err := json.Marshal(res)
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(s.acDir, ".tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, s.acPath(actionDigest))
}

func (s *Server) handleBuild(w http.ResponseWriter, r *http.Request) {
	var req api.BuildRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid build request: "+err.Error())
		return
	}
	actionDigest, err := ActionDigest(&req)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	// Serialize identical concurrent builds so the fixture command runs at
	// most once per action digest.
	mu, _ := s.inflight.LoadOrStore(actionDigest, &sync.Mutex{})
	m := mu.(*sync.Mutex)
	m.Lock()
	defer m.Unlock()

	if data, err := os.ReadFile(s.acPath(actionDigest)); err == nil {
		var res api.BuildResult
		if json.Unmarshal(data, &res) == nil && res.ActionDigest == actionDigest {
			res.CacheHit = true
			writeJSON(w, http.StatusOK, &res)
			return
		}
		log.Printf("ignoring unreadable action-cache entry %s; rebuilding", actionDigest)
	}

	res, err := s.exec.Run(r.Context(), &req)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	res.ActionDigest = actionDigest
	if err := s.storeAC(actionDigest, res); err != nil {
		writeError(w, http.StatusInternalServerError, "caching build result: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) handleStats(w http.ResponseWriter, _ *http.Request) {
	objects, bytes := s.store.Stats()
	var acEntries int64
	if entries, err := os.ReadDir(s.acDir); err == nil {
		for _, e := range entries {
			if !e.IsDir() {
				acEntries++
			}
		}
	}
	writeJSON(w, http.StatusOK, api.StatsResponse{
		Objects:       objects,
		Bytes:         bytes,
		ACEntries:     acEntries,
		MaxObjectSize: s.store.MaxSize(),
	})
}

func (s *Server) handleFsck(w http.ResponseWriter, _ *http.Request) {
	rep, err := s.store.Fsck()
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

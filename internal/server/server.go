// Package server exposes the build cache over HTTP.
//
//	POST /v1/builds        submit a build task; returns hit/miss + result
//	GET  /v1/builds/{key}  fetch one cache entry
//	GET  /v1/audit/{key}   show exactly which inputs produced the key
//	GET  /v1/entries       list all entries (audit trail)
//	GET  /healthz          liveness
package server

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"buildcache/internal/builder"
	"buildcache/internal/cachekey"
	"buildcache/internal/cas"
	"buildcache/internal/store"
	"buildcache/internal/toolchain"
)

type BuildRequest struct {
	TaskDir string            `json:"task_dir"`      // directory under the server workspace root
	Sources []string          `json:"sources"`       // declared source files (relative to task_dir)
	Command string            `json:"command"`       // sh -c command; must write the artifact to $OUT
	Env     map[string]string `json:"env,omitempty"` // declared environment, bound into the key
}

type artifactInfo struct {
	Digest string `json:"digest"`
	Size   int64  `json:"size"`
}

type buildResponse struct {
	Key         string        `json:"key"`
	Cache       string        `json:"cache"` // "hit" or "miss"
	Status      string        `json:"status"`
	Quarantined bool          `json:"quarantined,omitempty"`
	Artifact    *artifactInfo `json:"artifact,omitempty"`
	RunOutput   string        `json:"run_output,omitempty"`
	Error       string        `json:"error,omitempty"`
	Missing     []string      `json:"missing_sources,omitempty"`
}

type Server struct {
	store     *store.Store
	cas       *cas.Store
	workspace string // absolute root all task_dirs must live under
	tmpDir    string

	LeaseTTL   time.Duration
	WaitTime   time.Duration
	BuildDelay time.Duration // test hook: artificial build slowdown

	mux *http.ServeMux
}

func New(st *store.Store, c *cas.Store, workspace, tmpDir string) (*Server, error) {
	abs, err := filepath.Abs(workspace)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(tmpDir, 0o755); err != nil {
		return nil, err
	}
	s := &Server{
		store:     st,
		cas:       c,
		workspace: abs,
		tmpDir:    tmpDir,
		LeaseTTL:  10 * time.Minute,
		WaitTime:  5 * time.Minute,
	}
	s.mux = http.NewServeMux()
	s.mux.HandleFunc("POST /v1/builds", s.handleBuild)
	s.mux.HandleFunc("GET /v1/builds/", s.handleGetEntry)
	s.mux.HandleFunc("GET /v1/audit/", s.handleAudit)
	s.mux.HandleFunc("GET /v1/entries", s.handleList)
	s.mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	return s, nil
}

func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) handleBuild(w http.ResponseWriter, r *http.Request) {
	var req BuildRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}
	resp, code := s.build(r.Context(), req)
	writeJSON(w, code, resp)
}

// build computes the cache key and returns a cached artifact or runs the
// build exactly once per key (other callers wait for the publisher).
func (s *Server) build(ctx context.Context, req BuildRequest) (buildResponse, int) {
	if req.TaskDir == "" || req.Command == "" || len(req.Sources) == 0 {
		return buildResponse{Error: "task_dir, command and sources are required"}, http.StatusBadRequest
	}
	taskDir, err := s.resolveTaskDir(req.TaskDir)
	if err != nil {
		return buildResponse{Error: err.Error()}, http.StatusBadRequest
	}

	manifest, err := cachekey.BuildManifest(taskDir, req.Sources)
	if err != nil {
		return buildResponse{Error: err.Error()}, http.StatusBadRequest
	}
	var missing []string
	for _, e := range manifest {
		if e.State == cachekey.StateMissing {
			missing = append(missing, e.Path)
		}
	}

	toolName := ""
	if f := strings.Fields(req.Command); len(f) > 0 {
		toolName = f[0]
	}
	tc, err := toolchain.Resolve(ctx, toolName, taskDir)
	if err != nil {
		return buildResponse{Error: "toolchain: " + err.Error()}, http.StatusBadRequest
	}

	inputs := cachekey.Inputs{
		Platform:  toolchain.Platform(),
		Command:   req.Command,
		Env:       req.Env,
		Toolchain: tc,
		Sources:   manifest,
	}
	key := inputs.Key()
	inputsJSON, _ := json.Marshal(inputs)
	reqJSON, _ := json.Marshal(req)

	// A declared source that does not exist is a hard error — reported
	// distinctly from an empty file, which hashes normally.
	if len(missing) > 0 {
		return buildResponse{
			Key: key, Status: "failed", Cache: "miss",
			Error:   "declared sources do not exist (a missing file is not an empty file)",
			Missing: missing,
		}, http.StatusUnprocessableEntity
	}

	for attempt := 0; attempt < 3; attempt++ {
		// Fast path: existing successful entry with verified bytes.
		if ent, err := s.store.Get(key); err == nil && ent.Status == store.StatusSuccess {
			if s.cas.Verify(ent.ArtifactDigest, ent.ArtifactSize) == nil {
				return buildResponse{
					Key: key, Cache: "hit", Status: ent.Status,
					Artifact:  &artifactInfo{Digest: ent.ArtifactDigest, Size: ent.ArtifactSize},
					RunOutput: ent.RunOutput,
				}, http.StatusOK
			}
			// Corrupt or lost artifact: quarantine and rebuild.
			reason := s.quarantine(key, ent.ArtifactDigest)
			log.Printf("key %s: %s", key[:12], reason)
		}

		owner := newOwnerToken()
		acquired, err := s.store.TryAcquire(key, owner, string(reqJSON), string(inputsJSON), s.LeaseTTL)
		if err != nil {
			return buildResponse{Key: key, Error: err.Error()}, http.StatusInternalServerError
		}
		if acquired {
			return s.publish(ctx, key, owner, req, taskDir)
		}

		// Another client is publishing this key: wait for the outcome.
		wctx, cancel := context.WithTimeout(ctx, s.WaitTime)
		ent, err := s.store.WaitFor(wctx, key, 50*time.Millisecond)
		cancel()
		if err != nil {
			return buildResponse{Key: key, Error: "timed out waiting for publisher: " + err.Error()}, http.StatusServiceUnavailable
		}
		if ent == nil {
			continue // entry vanished; retry as publisher
		}
		switch ent.Status {
		case store.StatusSuccess:
			if s.cas.Verify(ent.ArtifactDigest, ent.ArtifactSize) == nil {
				return buildResponse{
					Key: key, Cache: "hit", Status: ent.Status,
					Artifact:  &artifactInfo{Digest: ent.ArtifactDigest, Size: ent.ArtifactSize},
					RunOutput: ent.RunOutput,
				}, http.StatusOK
			}
			s.quarantine(key, ent.ArtifactDigest)
			continue
		case store.StatusBuilding:
			continue // lease was stolen mid-wait; loop again
		default:
			// The publisher's build failed: report the failure honestly.
			// A failed build is never served as a cached success.
			return buildResponse{
				Key: key, Cache: "miss", Status: ent.Status, Error: ent.Error,
			}, http.StatusInternalServerError
		}
	}
	return buildResponse{Key: key, Error: "could not settle cache entry after retries"}, http.StatusServiceUnavailable
}

// publish runs the build as the single publisher for key and records the
// real outcome: success with the artifact in the CAS, or failure.
func (s *Server) publish(ctx context.Context, key, owner string, req BuildRequest, taskDir string) (buildResponse, int) {
	if s.BuildDelay > 0 {
		time.Sleep(s.BuildDelay)
	}
	outPath := filepath.Join(s.tmpDir, "out-"+key[:16]+"-"+owner[:8])
	defer os.Remove(outPath)

	runOutput, err := builder.Run(ctx, builder.Request{
		TaskDir: taskDir,
		Sources: req.Sources,
		Command: req.Command,
		Env:     req.Env,
	}, outPath)
	if err != nil {
		msg := err.Error()
		if perr := s.store.PublishFailure(key, owner, msg); perr != nil {
			log.Printf("publish failure record failed for %s: %v", key[:12], perr)
		}
		return buildResponse{Key: key, Cache: "miss", Status: store.StatusFailed, Error: msg},
			http.StatusInternalServerError
	}

	f, err := os.Open(outPath)
	if err != nil {
		s.store.PublishFailure(key, owner, err.Error())
		return buildResponse{Key: key, Cache: "miss", Status: store.StatusFailed, Error: err.Error()},
			http.StatusInternalServerError
	}
	digest, size, err := s.cas.Put(f)
	f.Close()
	if err != nil {
		s.store.PublishFailure(key, owner, err.Error())
		return buildResponse{Key: key, Cache: "miss", Status: store.StatusFailed, Error: err.Error()},
			http.StatusInternalServerError
	}
	if err := s.store.PublishSuccess(key, owner, digest, size, runOutput); err != nil {
		return buildResponse{Key: key, Cache: "miss", Status: store.StatusFailed, Error: err.Error()},
			http.StatusInternalServerError
	}
	return buildResponse{
		Key: key, Cache: "miss", Status: store.StatusSuccess,
		Artifact:  &artifactInfo{Digest: digest, Size: size},
		RunOutput: runOutput,
	}, http.StatusOK
}

// quarantine moves a corrupt artifact aside and flags the entry. Returns
// a human-readable reason.
func (s *Server) quarantine(key, digest string) string {
	where, err := s.cas.Quarantine(digest)
	var reason string
	switch {
	case err != nil:
		reason = "artifact corrupt; quarantine move failed: " + err.Error()
	case where == "":
		reason = "artifact missing from CAS (lost); entry quarantined"
	default:
		reason = "artifact failed digest verification; quarantined to " + where
	}
	if err := s.store.MarkQuarantined(key, reason); err != nil {
		log.Printf("mark quarantined %s: %v", key[:12], err)
	}
	return reason
}

func (s *Server) resolveTaskDir(taskDir string) (string, error) {
	if filepath.IsAbs(taskDir) {
		return "", fmt.Errorf("task_dir must be relative to the workspace root")
	}
	clean := filepath.Clean(taskDir)
	if clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("task_dir escapes the workspace root")
	}
	abs := filepath.Join(s.workspace, clean)
	fi, err := os.Stat(abs)
	if err != nil {
		return "", fmt.Errorf("task_dir: %v", err)
	}
	if !fi.IsDir() {
		return "", fmt.Errorf("task_dir is not a directory")
	}
	return abs, nil
}

func (s *Server) handleGetEntry(w http.ResponseWriter, r *http.Request) {
	key := strings.TrimPrefix(r.URL.Path, "/v1/builds/")
	ent, err := s.store.Get(key)
	if errors.Is(err, sql.ErrNoRows) || ent == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such key"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, ent)
}

func (s *Server) handleAudit(w http.ResponseWriter, r *http.Request) {
	key := strings.TrimPrefix(r.URL.Path, "/v1/audit/")
	ent, err := s.store.Get(key)
	if err != nil || ent == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such key"})
		return
	}
	// inputs_json is the exact canonical input set that produced the key.
	w.Header().Set("Content-Type", "application/json")
	fmt.Fprintf(w, `{"key":%q,"status":%q,"inputs":%s}`+"\n", ent.Key, ent.Status, ent.InputsJSON)
}

func (s *Server) handleList(w http.ResponseWriter, r *http.Request) {
	entries, err := s.store.List()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	if entries == nil {
		entries = []store.Entry{}
	}
	writeJSON(w, http.StatusOK, entries)
}

func newOwnerToken() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b[:])
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	enc.Encode(v)
}

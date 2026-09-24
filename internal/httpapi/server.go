// Package httpapi exposes the registry and its garbage collector over HTTP.
//
// Registry API (OCI Distribution-shaped, minimal):
//
//	POST   /v2/<name>/blobs/uploads/                 start a staged upload
//	PATCH  /v2/<name>/blobs/uploads/<id>            stream a chunk
//	PUT    /v2/<name>/blobs/uploads/<id>?digest=    verify+publish a blob
//	DELETE /v2/<name>/blobs/uploads/<id>            abort an upload
//	HEAD   /v2/<name>/blobs/<digest>                blob existence
//	GET    /v2/<name>/blobs/<digest>                download (acquires leases)
//	PUT    /v2/<name>/manifests/<reference>         put manifest (digest) or tag
//	GET    /v2/<name>/manifests/<reference>         fetch manifest
//	DELETE /v2/<name>/manifests/<reference>         delete a tag
//
// Administration API:
//
//	POST   /admin/gc/run        run GC now (?pause_after_mark=1 used by tests
//	                            to interleave a tag/pull; the pause waits for
//	                            ?resume=<token> on /admin/gc/resume)
//	GET    /admin/gc/report?id=<n>
//	POST   /admin/orphans/run  (?dry_run=1)
//	GET    /admin/orphans/report?id=<n>
package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"

	"layer-gc/internal/digest"
	"layer-gc/internal/gc"
	"layer-gc/internal/maintenance"
	"layer-gc/internal/registry"
	"layer-gc/internal/storage"
)

type Server struct {
	Reg      *registry.Service
	GC       *gc.Collector
	Cleaner  *maintenance.Cleaner
	Logger   *log.Logger
	LeaseTTL time.Duration

	mu        sync.Mutex
	pauseGate map[string]chan struct{}
}

func NewServer(reg *registry.Service, collector *gc.Collector, cleaner *maintenance.Cleaner, logger *log.Logger) *Server {
	return &Server{
		Reg:       reg,
		GC:        collector,
		Cleaner:   cleaner,
		Logger:    logger,
		LeaseTTL:  60 * time.Second,
		pauseGate: map[string]chan struct{}{},
	}
}

func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/v2/", s.handleV2)
	mux.HandleFunc("/admin/gc/run", s.handleGCRun)
	mux.HandleFunc("/admin/gc/resume", s.handleGCResume)
	mux.HandleFunc("/admin/gc/report", s.handleGCReport)
	mux.HandleFunc("/admin/orphans/run", s.handleOrphansRun)
	mux.HandleFunc("/admin/orphans/report", s.handleOrphansReport)
	return s.logging(mux)
}

// ---- registry --------------------------------------------------------------

func (s *Server) handleV2(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/v2/")
	parts := strings.SplitN(path, "/", 4)
	if len(parts) < 3 {
		http.Error(w, "malformed repository path", http.StatusBadRequest)
		return
	}
	// /v2/<name>/... — name may itself contain slashes; find the known suffix.
	switch {
	case strings.Contains(path, "/blobs/uploads/"):
		s.handleUploads(w, r)
	case strings.Contains(path, "/blobs/"):
		s.handleBlobs(w, r)
	case strings.Contains(path, "/manifests/"):
		s.handleManifests(w, r)
	default:
		http.NotFound(w, r)
	}
}

func splitBefore(path, marker string) (name, rest string, ok bool) {
	idx := strings.Index(path, marker)
	if idx < 0 {
		return "", "", false
	}
	return path[:idx], path[idx+len(marker):], true
}

func (s *Server) handleUploads(w http.ResponseWriter, r *http.Request) {
	name, rest, ok := splitBefore(r.URL.Path[len("/v2/"):], "/blobs/uploads/")
	if !ok {
		http.Error(w, "bad upload path", http.StatusBadRequest)
		return
	}
	_ = name
	ctx := r.Context()
	switch {
	case rest == "" && r.Method == http.MethodPost:
		id, tempPath, err := s.Reg.StartUpload(ctx)
		if err != nil {
			s.writeError(w, err)
			return
		}
		w.Header().Set("Location", fmt.Sprintf("/v2/%s/blobs/uploads/%s", name, id))
		w.Header().Set("Docker-Upload-Uuid", id)
		w.Header().Set("X-Upload-Temp-Path", tempPath)
		w.Header().Set("Range", "0-0")
		w.WriteHeader(http.StatusAccepted)
	case rest != "" && r.Method == http.MethodPatch:
		n, err := s.Reg.AppendUpload(ctx, rest, r.Body)
		if err != nil {
			s.writeError(w, err)
			return
		}
		w.Header().Set("Location", fmt.Sprintf("/v2/%s/blobs/uploads/%s", name, rest))
		w.Header().Set("Range", fmt.Sprintf("0-%d", n-1))
		w.WriteHeader(http.StatusAccepted)
	case rest != "" && r.Method == http.MethodPut:
		d := r.URL.Query().Get("digest")
		if err := s.Reg.CommitUpload(ctx, rest, d); err != nil {
			s.writeError(w, err)
			return
		}
		w.Header().Set("Docker-Content-Digest", d)
		w.WriteHeader(http.StatusCreated)
	case rest != "" && r.Method == http.MethodDelete:
		if err := s.Reg.AbortUpload(ctx, rest); err != nil {
			s.writeError(w, err)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (s *Server) handleBlobs(w http.ResponseWriter, r *http.Request) {
	_, rest, ok := splitBefore(r.URL.Path[len("/v2/"):], "/blobs/")
	if !ok {
		http.Error(w, "bad blob path", http.StatusBadRequest)
		return
	}
	ctx := r.Context()
	switch r.Method {
	case http.MethodHead:
		exists, err := s.Reg.HasBlob(ctx, rest)
		if err != nil {
			s.writeError(w, err)
			return
		}
		if !exists {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		rc, size, err := s.Reg.OpenBlob(ctx, rest)
		if err != nil {
			s.writeError(w, err)
			return
		}
		rc.Close()
		w.Header().Set("Docker-Content-Digest", rest)
		w.Header().Set("Content-Length", fmt.Sprintf("%d", size))
		w.WriteHeader(http.StatusOK)
	case http.MethodGet:
		// Acquire a read lease BEFORE streaming so GC cannot unlink the file
		// mid-pull.  The lease is released when the body is fully copied or
		// when the request ends; its TTL covers a crashed reader.
		ids, err := s.Reg.AcquireLease(ctx, r.RemoteAddr, s.LeaseTTL, []string{rest})
		if err != nil {
			s.writeError(w, err)
			return
		}
		rc, size, err := s.Reg.OpenBlob(ctx, rest)
		if err != nil {
			_ = s.Reg.ReleaseLease(context.Background(), ids)
			s.writeError(w, err)
			return
		}
		w.Header().Set("Docker-Content-Digest", rest)
		w.Header().Set("Content-Type", "application/octet-stream")
		w.Header().Set("Content-Length", fmt.Sprintf("%d", size))
		w.Header().Set("X-Lease-Ids", leaseIDHeader(ids))
		w.WriteHeader(http.StatusOK)
		_, copyErr := io.Copy(w, rc)
		rc.Close()
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		relErr := s.Reg.ReleaseLease(context.Background(), ids)
		if copyErr != nil {
			s.Logger.Printf("blob %s stream interrupted: %v (lease retained until ttl on release error: %v)", rest, copyErr, relErr)
		}
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func leaseIDHeader(ids []int64) string {
	parts := make([]string, len(ids))
	for i, id := range ids {
		parts[i] = fmt.Sprintf("%d", id)
	}
	return strings.Join(parts, ",")
}

func (s *Server) handleManifests(w http.ResponseWriter, r *http.Request) {
	name, reference, ok := splitBefore(r.URL.Path[len("/v2/"):], "/manifests/")
	if !ok {
		http.Error(w, "bad manifest path", http.StatusBadRequest)
		return
	}
	ctx := r.Context()
	switch r.Method {
	case http.MethodPut:
		raw, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		var m registry.Manifest
		if err := json.Unmarshal(raw, &m); err != nil {
			http.Error(w, "invalid manifest json: "+err.Error(), http.StatusBadRequest)
			return
		}
		md, err := s.Reg.PutManifest(ctx, m, raw)
		if err != nil {
			s.writeError(w, err)
			return
		}
		// A textual reference is a tag: publish the tag as part of the same
		// request so the manifest's layers become roots immediately.
		if !strings.HasPrefix(reference, digest.Prefix) {
			if err := s.Reg.Tag(ctx, name, reference, md); err != nil {
				s.writeError(w, err)
				return
			}
		}
		w.Header().Set("Docker-Content-Digest", md)
		w.WriteHeader(http.StatusCreated)
	case http.MethodGet:
		var md string
		if strings.HasPrefix(reference, digest.Prefix) {
			md = reference
		} else {
			d, err := s.Reg.ResolveTag(ctx, name, reference)
			if err != nil {
				s.writeError(w, err)
				return
			}
			md = d
		}
		m, raw, err := s.Reg.GetManifest(ctx, md)
		if err != nil {
			s.writeError(w, err)
			return
		}
		// Pin referenced layers for the pull window too.
		layers, _ := s.Reg.ManifestLayers(ctx, md)
		var leaseIDs []int64
		if len(layers) > 0 {
			ids, lerr := s.Reg.AcquireLease(ctx, r.RemoteAddr, s.LeaseTTL, layers)
			if lerr == nil {
				leaseIDs = ids
			}
		}
		w.Header().Set("Docker-Content-Digest", md)
		w.Header().Set("Content-Type", mediaTypeOf(m))
		if len(leaseIDs) > 0 {
			w.Header().Set("X-Lease-Ids", leaseIDHeader(leaseIDs))
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(raw)
		// Best-effort immediate release; TTL is the crash backstop.
		_ = s.Reg.ReleaseLease(context.Background(), leaseIDs)
	case http.MethodDelete:
		if strings.HasPrefix(reference, digest.Prefix) {
			if err := s.Reg.DeleteManifest(ctx, reference); err != nil {
				s.writeError(w, err)
				return
			}
			w.WriteHeader(http.StatusNoContent)
			return
		}
		if err := s.Reg.Untag(ctx, name, reference); err != nil {
			s.writeError(w, err)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func mediaTypeOf(m *registry.Manifest) string {
	if m.MediaType != "" {
		return m.MediaType
	}
	return "application/vnd.oci.image.manifest.v1+json"
}

// ---- admin: GC -------------------------------------------------------------

func (s *Server) handleGCRun(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST required", http.StatusMethodNotAllowed)
		return
	}
	// Optional deterministic pause for acceptance tests: GC blocks after the
	// mark snapshot until ?resume hits /admin/gc/resume with the same
	// pause_token.  The client supplies the token so it can wait on the gate
	// before the blocking HTTP call returns.
	hooks := gc.Hooks{}
	var token string
	if r.URL.Query().Has("pause_after_mark") {
		token = r.URL.Query().Get("pause_token")
		if token == "" {
			http.Error(w, "pause_after_mark requires pause_token", http.StatusBadRequest)
			return
		}
		gate := make(chan struct{})
		s.mu.Lock()
		if _, exists := s.pauseGate[token]; exists {
			s.mu.Unlock()
			http.Error(w, "pause_token already in use", http.StatusConflict)
			return
		}
		s.pauseGate[token] = gate
		s.mu.Unlock()
		hooks.AfterMark = func(int64) error {
			select {
			case <-gate:
				return nil
			case <-r.Context().Done():
				return r.Context().Err()
			case <-time.After(30 * time.Second):
				return errors.New("gc pause timed out")
			}
		}
	}

	// Run with the requested hooks in a short-lived collector view.
	coll := *s.GC
	coll.Hooks = hooks
	rep, err := coll.Run(r.Context())
	if err != nil {
		s.mu.Lock()
		delete(s.pauseGate, token)
		s.mu.Unlock()
		http.Error(w, "gc failed: "+err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeJSON(w, http.StatusOK, reportJSON(rep))
}

// WaitGateMu runs fn under the gate lock, exposing a snapshot of open pause
// gates.  It is a test-support hook for deterministic interleaving.
func (s *Server) WaitGateMu(fn func(map[string]chan struct{})) {
	s.mu.Lock()
	defer s.mu.Unlock()
	fn(s.pauseGate)
}

func (s *Server) handleGCResume(w http.ResponseWriter, r *http.Request) {
	token := r.URL.Query().Get("token")
	s.mu.Lock()
	gate := s.pauseGate[token]
	delete(s.pauseGate, token)
	s.mu.Unlock()
	if gate == nil {
		http.Error(w, "unknown pause token", http.StatusNotFound)
		return
	}
	close(gate)
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) handleGCReport(w http.ResponseWriter, r *http.Request) {
	id, err := queryInt64(r, "id")
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	rep, err := s.GC.GetReport(r.Context(), id)
	if err != nil {
		http.Error(w, err.Error(), http.StatusNotFound)
		return
	}
	s.writeJSON(w, http.StatusOK, reportJSON(rep))
}

// ---- admin: orphans --------------------------------------------------------

func (s *Server) handleOrphansRun(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST required", http.StatusMethodNotAllowed)
		return
	}
	rep, err := s.Cleaner.CleanOrphans(r.Context(), r.URL.Query().Has("dry_run"))
	if err != nil {
		http.Error(w, "orphan cleanup failed: "+err.Error(), http.StatusInternalServerError)
		return
	}
	items := make([]map[string]any, 0, len(rep.Items))
	for _, it := range rep.Items {
		items = append(items, map[string]any{
			"path": it.Path, "reason": it.Reason, "size_bytes": it.Size, "removed": it.Removed,
		})
	}
	s.writeJSON(w, http.StatusOK, map[string]any{
		"run_id": rep.RunID, "started_at": rep.StartedAt, "finished_at": rep.FinishedAt,
		"removed_count": rep.Removed, "reclaimed_bytes": rep.ReclaimedBytes, "items": items,
	})
}

func (s *Server) handleOrphansReport(w http.ResponseWriter, r *http.Request) {
	// Reuse the run data shape via a direct query through the cleaner-less DB
	// would be needed; expose only the last run through the run endpoint in
	// this minimal build — report returns last run by calling a DB read here.
	http.Error(w, "use POST /admin/orphans/run response (last run)", http.StatusNotImplemented)
}

// ---- helpers ---------------------------------------------------------------

func reportJSON(rep *gc.Report) map[string]any {
	items := make([]map[string]any, 0, len(rep.Items))
	for _, it := range rep.Items {
		items = append(items, map[string]any{
			"blob_digest": it.Digest, "decision": it.Decision, "reason": it.Reason,
			"size_bytes": it.Size, "marked_at_snapshot": it.Marked,
		})
	}
	return map[string]any{
		"run_id": rep.RunID, "status": rep.Status,
		"started_at": rep.StartedAt, "finished_at": rep.FinishedAt,
		"snapshot_xmin": rep.SnapshotXmin,
		"marked_count":  rep.Marked, "candidate_count": rep.Candidates,
		"deleted_count": rep.Deleted, "retained_count": rep.Retained,
		"recovered_previous_crashed_run": rep.RecoveredCrash,
		"items":                          items,
	}
}

func (s *Server) writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *Server) writeError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, registry.ErrNotFound), errors.Is(err, storage.ErrNotFound):
		http.Error(w, err.Error(), http.StatusNotFound)
	case errors.Is(err, registry.ErrDigestMismatch), errors.Is(err, digest.ErrInvalid):
		http.Error(w, err.Error(), http.StatusBadRequest)
	case errors.Is(err, registry.ErrLayerMissing):
		http.Error(w, err.Error(), http.StatusPreconditionFailed)
	default:
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}

func queryInt64(r *http.Request, key string) (int64, error) {
	v := r.URL.Query().Get(key)
	if v == "" {
		return 0, fmt.Errorf("missing %s query parameter", key)
	}
	var n int64
	if _, err := fmt.Sscanf(v, "%d", &n); err != nil {
		return 0, fmt.Errorf("bad %s: %w", key, err)
	}
	return n, nil
}

func (s *Server) logging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &statusRecorder{ResponseWriter: w, status: 200}
		next.ServeHTTP(rw, r)
		s.Logger.Printf("%s %s -> %d (%s)", r.Method, r.URL.RequestURI(), rw.status, time.Since(start))
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

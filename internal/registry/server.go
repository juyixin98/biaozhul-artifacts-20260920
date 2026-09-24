// Package registry exposes the content-addressable registry over HTTP:
// blob upload (staged, digest-verified, atomic publish), manifest put/get,
// tags, read leases, and the GC/admin endpoints.
package registry

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"layerregistry/internal/gc"
	"layerregistry/internal/storage"
	"layerregistry/internal/store"
)

// Config configures the HTTP server.
type Config struct {
	LeaseTTL      time.Duration
	BlobGrace     time.Duration
	OrphanMaxAge  time.Duration
	FaultsEnabled bool // turns on test-only X-Gc-* / X-Read-Delay fault hooks
}

// Server bundles dependencies.
type Server struct {
	st  *store.Store
	fs  *storage.Store
	cfg Config
	col *gc.Collector
	mux *http.ServeMux
}

func NewServer(st *store.Store, fs *storage.Store, cfg Config) *Server {
	s := &Server{st: st, fs: fs, cfg: cfg}
	s.col = gc.New(st, fs, gc.Config{BlobGrace: cfg.BlobGrace})
	s.routes()
	return s
}

// Collector exposes the GC collector (used by admin handlers + tests).
func (s *Server) Collector() *gc.Collector { return s.col }

func (s *Server) Handler() http.Handler { return s.mux }

// newID returns a random 128-bit hex identifier.
func newID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

// splitPath splits "/v2/<repo>/blobs/..." into repo and remainder. The repo
// is everything between /v2/ and the first of the well-known trailing
// segments; here the router hands us paths already cut at the action, so the
// repo is simply the segment after /v2/.
func splitPath(path string) (repo, rest string) {
	p := strings.TrimPrefix(path, "/v2/")
	i := strings.IndexByte(p, '/')
	if i < 0 {
		return p, ""
	}
	return p[:i], p[i+1:]
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]any{"errors": []map[string]string{{"code": code, "message": msg}}})
}

func mapStoreError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, store.ErrNotFound):
		writeErr(w, http.StatusNotFound, "NOT_FOUND", err.Error())
	case errors.Is(err, store.ErrLeased):
		writeErr(w, http.StatusConflict, "LEASED", err.Error())
	case errors.Is(err, store.ErrReferenced):
		writeErr(w, http.StatusConflict, "REFERENCED", err.Error())
	default:
		writeErr(w, http.StatusInternalServerError, "INTERNAL", err.Error())
	}
}

// parseContentRange parses a "bytes start-end/total" range (total may be '*').
func parseContentRange(cr string) (start, end int64, ok bool) {
	cr = strings.TrimPrefix(cr, "bytes ")

	dash := strings.IndexByte(cr, '-')
	slash := strings.IndexByte(cr, '/')
	if dash < 0 || slash < 0 || dash > slash {
		return 0, 0, false
	}
	st, err1 := strconv.ParseInt(cr[:dash], 10, 64)
	en, err2 := strconv.ParseInt(cr[dash+1:slash], 10, 64)
	if err1 != nil || err2 != nil || en < st {
		return 0, 0, false
	}
	return st, en, true
}

// routes wires every endpoint.
func (s *Server) routes() {
	m := http.NewServeMux()

	m.HandleFunc("GET /v2/", s.apiCheck)
	m.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

	// Blob uploads.
	m.HandleFunc("POST /v2/{repo}/blobs/uploads/", s.handlePostUpload)
	m.HandleFunc("HEAD /v2/{repo}/blobs/uploads/{id}", s.getUploadStatus)
	m.HandleFunc("PATCH /v2/{repo}/blobs/uploads/{id}", s.patchUpload)
	m.HandleFunc("PUT /v2/{repo}/blobs/uploads/{id}", s.finalizeUpload)

	// Blobs.
	m.HandleFunc("HEAD /v2/{repo}/blobs/{digest}", s.headBlob)
	m.HandleFunc("GET /v2/{repo}/blobs/{digest}", s.getBlob)
	m.HandleFunc("DELETE /v2/{repo}/blobs/{digest}", s.deleteBlob)

	// Manifests + tags ({ref...} so a single pattern serves tags and digests).
	m.HandleFunc("PUT /v2/{repo}/manifests/{ref...}", s.putManifest)
	m.HandleFunc("HEAD /v2/{repo}/manifests/{ref...}", s.headManifest)
	m.HandleFunc("GET /v2/{repo}/manifests/{ref...}", s.getManifest)
	m.HandleFunc("DELETE /v2/{repo}/manifests/{ref...}", s.deleteManifest)
	m.HandleFunc("GET /v2/{repo}/tags/list", s.listTags)

	// Leases (all namespaced under repo to avoid wildcard collisions).
	m.HandleFunc("POST /v2/{repo}/leases", s.createLease)
	m.HandleFunc("POST /v2/{repo}/leases/{id}", s.renewLease)
	m.HandleFunc("DELETE /v2/{repo}/leases/{id}", s.deleteLease)

	// Admin / GC.
	m.HandleFunc("POST /admin/gc", s.runGC)
	m.HandleFunc("GET /admin/gc/{id}", s.getGCRun)
	m.HandleFunc("GET /admin/gc", s.listGCRuns)
	m.HandleFunc("POST /admin/orphans", s.cleanupOrphans)
	m.HandleFunc("GET /admin/blobs", s.listBlobs)
	m.HandleFunc("GET /admin/audit", s.audit)
	m.HandleFunc("POST /admin/recover", s.recover)

	s.mux = m
}

func (s *Server) apiCheck(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/v2/" || r.URL.Path == "/v2" {
		w.WriteHeader(http.StatusOK)
		return
	}
	writeErr(w, http.StatusNotFound, "NOT_FOUND", "no such endpoint")
}

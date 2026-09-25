// Package cacheapi exposes the artifact cache over HTTP.
//
// Routing (JSON unless a body is the object bytes):
//
//	GET    /healthz
//	GET    /v1/blobs                       list objects
//	GET    /v1/stats                       cache statistics
//	PUT    /v1/blobs/{algo:hex}            upload (body=object)
//	POST   /v1/blobs/{algo:hex}            upload alias (same semantics)
//	HEAD   /v1/blobs/{algo:hex}            metadata only
//	GET    /v1/blobs/{algo:hex}            download (supports Range)
//	POST   /v1/blobs/{algo:hex}/verify     rehash; quarantine if bad
//	DELETE /v1/blobs/{algo:hex}            evict
//
// Uploads are staged under tmp/ and only become reachable after their digest
// verifies, so GET can never observe a partial object.
package cacheapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"modelcache/digest"
	"modelcache/store"
)

// Server wires the store to HTTP handlers.
type Server struct {
	store  *store.Store
	logger *log.Logger
}

// NewServer builds a Server.
func NewServer(st *store.Store, logger *log.Logger) *Server {
	if logger == nil {
		logger = log.Default()
	}
	return &Server{store: st, logger: logger}
}

// Handler returns the root http.Handler with request logging.
func (s *Server) Handler() http.Handler {
	return s.withLogging(s.route())
}

func (s *Server) route() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealth)
	mux.HandleFunc("/v1/blobs", s.handleBlobs)
	mux.HandleFunc("/v1/blobs/", s.handleBlob)
	mux.HandleFunc("/v1/stats", s.handleStats)
	return mux
}

// apiError is the JSON error envelope for all failures.
type apiError struct {
	Error  string         `json:"error"`
	Code   string         `json:"code"`
	Detail string         `json:"detail,omitempty"`
	Extra  map[string]any `json:"extra,omitempty"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, code, msg string, detail any) {
	e := apiError{Error: msg, Code: code}
	if detail != nil {
		switch d := detail.(type) {
		case string:
			e.Detail = d
		case error:
			e.Detail = d.Error()
		default:
			e.Detail = fmt.Sprint(d)
		}
	}
	writeJSON(w, status, e)
}

// classify maps store errors onto (HTTP status, machine code).
func classify(err error) (int, string) {
	switch {
	case errors.Is(err, store.ErrNotFound):
		return http.StatusNotFound, "not_found"
	case errors.Is(err, store.ErrTooLarge):
		return http.StatusRequestEntityTooLarge, "object_too_large"
	case errors.Is(err, store.ErrDigestMismatch):
		return http.StatusBadRequest, "digest_mismatch"
	case errors.Is(err, store.ErrSizeMismatch):
		return http.StatusBadRequest, "size_mismatch"
	case errors.Is(err, store.ErrCorrupt):
		return http.StatusInternalServerError, "corrupt"
	default:
		return http.StatusInternalServerError, "internal"
	}
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	st, err := s.store.Stats(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", "stats failed", err)
		return
	}
	writeJSON(w, http.StatusOK, st)
}

func (s *Server) handleBlobs(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		blobs, err := s.store.List(r.Context())
		if err != nil {
			writeError(w, http.StatusInternalServerError, "internal", "list failed", err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"blobs": blobs})
	default:
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET", nil)
	}
}

// handleBlob dispatches /v1/blobs/{digest}[/{action}].
func (s *Server) handleBlob(w http.ResponseWriter, r *http.Request) {
	rest := strings.TrimPrefix(r.URL.Path, "/v1/blobs/")
	if rest == "" {
		writeError(w, http.StatusBadRequest, "bad_request", "empty digest", nil)
		return
	}
	parts := strings.Split(rest, "/")
	if len(parts) > 2 {
		writeError(w, http.StatusBadRequest, "bad_request", "unexpected path suffix", nil)
		return
	}
	dgstStr := parts[0]
	dgst, err := digest.Parse(dgstStr)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_digest", "invalid digest", err)
		return
	}
	action := ""
	if len(parts) == 2 {
		action = parts[1]
	}

	switch {
	case action == "verify":
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use POST", nil)
			return
		}
		s.handleVerify(w, r, dgst)
	case action == "":
		switch r.Method {
		case http.MethodPut, http.MethodPost:
			s.handlePut(w, r, dgst)
		case http.MethodHead:
			s.handleHead(w, r, dgst)
		case http.MethodGet:
			s.handleGet(w, r, dgst)
		case http.MethodDelete:
			s.handleDelete(w, r, dgst)
		default:
			writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use PUT/HEAD/GET/DELETE", nil)
		}
	default:
		writeError(w, http.StatusNotFound, "not_found", "unknown action "+action, nil)
	}
}

func (s *Server) handlePut(w http.ResponseWriter, r *http.Request, dgst digest.Digest) {
	var declared int64 = -1
	if cl := r.Header.Get("Content-Length"); cl != "" {
		n, err := strconv.ParseInt(cl, 10, 64)
		if err != nil || n < 0 {
			writeError(w, http.StatusBadRequest, "bad_request", "invalid Content-Length", nil)
			return
		}
		declared = n
		if n > s.store.MaxObjectSize() {
			writeError(w, http.StatusRequestEntityTooLarge, "object_too_large",
				fmt.Sprintf("declared size %d exceeds limit %d", n, s.store.MaxObjectSize()), nil)
			// Drain up to the cap so the connection can be reused; body is
			// bounded either by Content-Length or by the store's limit reader.
			drain(r.Body, s.store.MaxObjectSize())
			return
		}
	}

	size, existed, err := s.store.Put(r.Context(), dgst, declared, r.Body)
	if err != nil {
		status, code := classify(err)
		writeError(w, status, code, "upload rejected", err)
		return
	}
	w.Header().Set("ETag", etagFor(dgst))
	writeJSON(w, http.StatusOK, map[string]any{
		"digest":  dgst.String(),
		"size":    size,
		"existed": existed,
	})
}

func (s *Server) handleHead(w http.ResponseWriter, r *http.Request, dgst digest.Digest) {
	f, fi, err := s.store.Get(r.Context(), dgst)
	if err != nil {
		status, code := classify(err)
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-Error-Code", code)
		w.WriteHeader(status)
		return
	}
	defer f.Close()
	setBlobHeaders(w, dgst, fi.Size())
	w.WriteHeader(http.StatusOK)
}

func (s *Server) handleGet(w http.ResponseWriter, r *http.Request, dgst digest.Digest) {
	f, fi, err := s.store.Get(r.Context(), dgst)
	if err != nil {
		status, code := classify(err)
		writeError(w, status, code, "download failed", err)
		return
	}
	defer f.Close()

	// Operators can force a full re-hash with ?verify=1 to diagnose a bad
	// cache. A corrupt blob is quarantined and the request fails.
	if r.URL.Query().Get("verify") == "1" {
		if err := verifyOpenFile(f, dgst); err != nil {
			// Quarantine the corrupt published blob through the store.
			if _, verr := s.store.Verify(r.Context(), dgst); verr != nil {
				s.logger.Printf("verify=1: quarantine failed: %v", verr)
			}
			status, code := classify(err)
			writeError(w, status, code, "object failed verification", err)
			return
		}
		if _, err := f.Seek(0, 0); err != nil {
			writeError(w, http.StatusInternalServerError, "internal", "seek failed", err)
			return
		}
	}

	setBlobHeaders(w, dgst, fi.Size())
	// ServeContent implements Range, If-Range, If-None-Match and 304s against
	// ETag/Last-Modified. It also handles interrupted clients correctly
	// (ServeContent just returns; partial objects are never written to cache).
	http.ServeContent(w, r, dgst.Hex(), time.Time{}, f)
}

func (s *Server) handleVerify(w http.ResponseWriter, r *http.Request, dgst digest.Digest) {
	size, err := s.store.Verify(r.Context(), dgst)
	if err != nil {
		status, code := classify(err)
		writeJSON(w, status, apiError{
			Error:  "verification failed",
			Code:   code,
			Detail: err.Error(),
			Extra: map[string]any{
				"quarantined":   errors.Is(err, store.ErrCorrupt),
				"size":          size,
				"quarantineDir": s.store.Root() + "/quarantine",
			},
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"digest": dgst.String(),
		"size":   size,
		"ok":     true,
	})
}

func (s *Server) handleDelete(w http.ResponseWriter, r *http.Request, dgst digest.Digest) {
	if err := s.store.Delete(r.Context(), dgst); err != nil {
		status, code := classify(err)
		writeError(w, status, code, "delete failed", err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"deleted": dgst.String()})
}

// verifyOpenFile re-hashes an already-open blob.
func verifyOpenFile(f io.Reader, dgst digest.Digest) error {
	d, _, err := digest.FromReader(f)
	if err != nil {
		return err
	}
	if d.Hex() != dgst.Hex() {
		return &store.CorruptionError{Digest: dgst}
	}
	return nil
}

func setBlobHeaders(w http.ResponseWriter, dgst digest.Digest, size int64) {
	h := w.Header()
	h.Set("ETag", etagFor(dgst))
	h.Set("Content-Type", "application/octet-stream")
	h.Set("X-Content-Digest", dgst.String())
	h.Set("X-Content-Length", strconv.FormatInt(size, 10))
	h.Set("Accept-Ranges", "bytes")
}

func etagFor(dgst digest.Digest) string { return `"` + dgst.Hex() + `"` }

// drain reads up to n bytes from rc, discarding them.
func drain(rc interface{ Read([]byte) (int, error) }, n int64) {
	buf := make([]byte, 32*1024)
	for n > 0 {
		if int64(len(buf)) > n {
			buf = buf[:n]
		}
		got, err := rc.Read(buf)
		n -= int64(got)
		if err != nil {
			return
		}
	}
}

// --- logging middleware ---

type statusRecorder struct {
	http.ResponseWriter
	status int
	bytes  int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func (r *statusRecorder) Write(b []byte) (int, error) {
	if r.status == 0 {
		r.status = http.StatusOK
	}
	n, err := r.ResponseWriter.Write(b)
	r.bytes += n
	return n, err
}

func (s *Server) withLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w}
		next.ServeHTTP(rec, r)
		s.logger.Printf("%s %s -> %d (%d bytes) %s",
			r.Method, r.URL.RequestURI(), rec.status, rec.bytes, time.Since(start).Round(time.Millisecond))
	})
}

// ListenAndServe runs the HTTP server with sane timeouts and graceful
// shutdown when ctx is cancelled.
func ListenAndServe(ctx context.Context, addr string, h http.Handler, logger *log.Logger) error {
	srv := &http.Server{
		Addr:              addr,
		Handler:           h,
		ReadHeaderTimeout: 15 * time.Second,
		IdleTimeout:       120 * time.Second,
		// No WriteTimeout: large model artifact uploads/downloads may be slow.
	}
	errs := make(chan error, 1)
	go func() {
		logger.Printf("cache server listening on %s", addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errs <- err
		}
		close(errs)
	}()
	select {
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := srv.Shutdown(shutdownCtx); err != nil {
			return err
		}
		return nil
	case err := <-errs:
		return err
	}
}

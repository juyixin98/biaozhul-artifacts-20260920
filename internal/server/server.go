// Package server implements the local HTTP service that serves immutable
// artifacts with full RFC 9110 byte-range semantics.
//
// All external dependencies (artifact storage) are in-process fakes; the
// server never touches a production system.
package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/example/rangeserver/internal/artifact"
	"github.com/example/rangeserver/internal/rangespec"
)

// DefaultMaxRanges bounds how many byte-range-specs a single request may
// carry. Requests above the limit are rejected so a client cannot force
// quadratic multipart assembly work.
const DefaultMaxRanges = 5

// decision labels surfaced in the X-Range-Decision response header; they
// make the server's choice observable in tests without parsing bodies.
const (
	decisionFull               = "full"
	decisionSingle             = "single-partial"
	decisionMultipart          = "multipart"
	decisionUnsatisfiable      = "unsatisfiable-416"
	decisionIgnoredMalformed   = "ignored-malformed"
	decisionIgnoredIfRange     = "ignored-if-range"
	decisionRejectedRangeLimit = "rejected-range-limit"
)

// Server holds the HTTP handlers and their configuration.
type Server struct {
	registry   *artifact.Registry
	maxRanges  int
	offersGzip bool
	logger     *log.Logger
	mux        *http.ServeMux
}

// Option configures a Server.
type Option func(*Server)

// WithMaxRanges overrides DefaultMaxRanges.
func WithMaxRanges(n int) Option {
	return func(s *Server) { s.maxRanges = n }
}

// WithoutGzip disables the gzip representation.
func WithoutGzip() Option {
	return func(s *Server) { s.offersGzip = false }
}

// WithLogger sets the logger (defaults to the package logger).
func WithLogger(l *log.Logger) Option {
	return func(s *Server) { s.logger = l }
}

// New constructs the server with its routes.
func New(reg *artifact.Registry, opts ...Option) *Server {
	s := &Server{
		registry:   reg,
		maxRanges:  DefaultMaxRanges,
		offersGzip: true,
		logger:     log.Default(),
	}
	for _, opt := range opts {
		opt(s)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /artifacts", s.handleList)
	mux.HandleFunc("GET /artifacts/{id}", s.handleArtifact)
	mux.HandleFunc("HEAD /artifacts/{id}", s.handleArtifact)
	mux.HandleFunc("POST /artifacts/{id}", s.handleUpload)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	s.mux = mux
	return s
}

// Handler exposes the configured router.
func (s *Server) Handler() http.Handler { return s.mux }

// ServeHTTP lets Server be used directly as an http.Handler.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

func (s *Server) handleList(w http.ResponseWriter, r *http.Request) {
	ids := s.registry.IDs()
	type item struct {
		ID         string `json:"id"`
		ETag       string `json:"etag"`
		Size       int    `json:"size"`
		SHA256     string `json:"sha256"`
		ModifiedAt string `json:"last_modified"`
	}
	items := make([]item, 0, len(ids))
	for _, id := range ids {
		a, err := s.registry.Get(r.Context(), id)
		if err != nil {
			continue
		}
		rep, _ := a.Representation(artifact.RepIdentity)
		items = append(items, item{
			ID:         a.ID(),
			ETag:       a.ETag(),
			Size:       len(rep),
			SHA256:     a.ContentHash(),
			ModifiedAt: a.LastModified().Format(http.TimeFormat),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"artifacts": items})
}

func (s *Server) handleUpload(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		writeError(w, http.StatusBadRequest, "missing artifact id")
		return
	}
	// Lab tooling only: cap the in-memory body at 16 MiB.
	r.Body = http.MaxBytesReader(w, r.Body, 16<<20)
	body := make([]byte, 0, 4096)
	buf := make([]byte, 32*1024)
	for {
		n, err := r.Body.Read(buf)
		body = append(body, buf[:n]...)
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			writeError(w, http.StatusBadRequest, "failed reading body: "+err.Error())
			return
		}
	}
	a := artifact.New(id, body)
	s.registry.Put(a)
	writeJSON(w, http.StatusCreated, map[string]any{
		"id":     a.ID(),
		"etag":   a.ETag(),
		"size":   len(body),
		"sha256": a.ContentHash(),
	})
}

func (s *Server) handleArtifact(w http.ResponseWriter, r *http.Request) {
	a, err := s.registry.Get(r.Context(), r.PathValue("id"))
	if errors.Is(err, artifact.ErrUnknownArtifact) {
		writeError(w, http.StatusNotFound, "unknown artifact: "+r.PathValue("id"))
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	encoding, ok := rangespec.SelectEncoding(r.Header.Get("Accept-Encoding"), s.offersGzip)
	if !ok {
		w.Header().Set("Vary", "Accept-Encoding")
		writeError(w, http.StatusNotAcceptable, "no acceptable representation offered")
		return
	}
	rep, err := a.Representation(encoding)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	repETag, err := a.ETagFor(encoding)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	h := w.Header()
	h.Set("ETag", repETag)
	h.Set("Last-Modified", a.LastModified().Format(http.TimeFormat))
	h.Set("Accept-Ranges", rangespec.RangeUnitBytes)
	h.Set("Content-Type", "application/octet-stream")
	if s.offersGzip {
		h.Add("Vary", "Accept-Encoding")
	}
	if encoding == artifact.RepGzip {
		h.Set("Content-Encoding", "gzip")
	}

	rangeHeader := strings.TrimSpace(r.Header.Get("Range"))
	if rangeHeader == "" {
		s.serveFull(w, r, rep, decisionFull)
		return
	}

	specs, perr := rangespec.ParseHeader(rangeHeader)
	if perr != nil {
		// Malformed Range: ignore it entirely and send 200.
		s.serveFull(w, r, rep, decisionIgnoredMalformed)
		return
	}
	if cerr := rangespec.CheckCount(specs, s.maxRanges); cerr != nil {
		h.Set("X-Range-Decision", decisionRejectedRangeLimit)
		writeError(w, http.StatusBadRequest, cerr.Error())
		return
	}

	if ifr := r.Header.Get("If-Range"); ifr != "" {
		if !rangespec.IfRange(ifr, repETag, a.LastModified()) {
			// Stale validator: ignore Range, send the whole representation.
			s.serveFull(w, r, rep, decisionIgnoredIfRange)
			return
		}
	}

	ranges, rerr := rangespec.ResolveAll(specs, int64(len(rep)))
	if rerr != nil {
		var unsatisfiable *rangespec.UnsatisfiableError
		if errors.As(rerr, &unsatisfiable) {
			h.Set("X-Range-Decision", decisionUnsatisfiable)
			h.Set("Content-Range", rangespec.UnsatisfiableContentRange(int64(len(rep))))
			writeError(w, http.StatusRequestedRangeNotSatisfiable,
				"requested range not satisfiable")
			return
		}
		writeError(w, http.StatusInternalServerError, rerr.Error())
		return
	}

	if len(ranges) == 1 {
		rg := ranges[0]
		h.Set("X-Range-Decision", decisionSingle)
		h.Set("Content-Range", rangespec.ContentRange(rg, int64(len(rep))))
		h.Set("Content-Length", fmt.Sprintf("%d", rg.Length()))
		w.WriteHeader(http.StatusPartialContent)
		if r.Method == http.MethodHead {
			return
		}
		_, _ = w.Write(rep[rg.First : rg.Last+1])
		return
	}

	parts := make([]rangespec.Part, 0, len(ranges))
	for _, rg := range ranges {
		parts = append(parts, rangespec.Part{Range: rg, ContentType: "application/octet-stream"})
	}
	nonce := fmt.Sprintf("%s-%d", a.ID(), time.Now().UnixNano())
	asm := rangespec.NewMultipartAssembler(rangespec.Boundary(nonce), rep, parts)
	body := asm.Body()
	h.Set("X-Range-Decision", decisionMultipart)
	h.Set("Content-Type", asm.ContentType())
	h.Set("Content-Length", fmt.Sprintf("%d", len(body)))
	w.WriteHeader(http.StatusPartialContent)
	if r.Method == http.MethodHead {
		return
	}
	_, _ = w.Write(body)
}

func (s *Server) serveFull(w http.ResponseWriter, r *http.Request, rep []byte, decision string) {
	h := w.Header()
	h.Set("X-Range-Decision", decision)
	h.Set("Content-Length", fmt.Sprintf("%d", len(rep)))
	w.WriteHeader(http.StatusOK)
	if r.Method == http.MethodHead {
		return
	}
	_, _ = w.Write(rep)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"error": msg, "status": status})
}

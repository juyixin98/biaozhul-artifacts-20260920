// Package server implements the local HTTP service that serves immutable
// binary artifacts with full Range / If-Range semantics.
package server

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"httprange/internal/artifact"
	"httprange/internal/clock"
	"httprange/internal/mpart"
	"httprange/internal/rangespec"
)

// DefaultMaxRanges bounds the number of members accepted in one Range header.
const DefaultMaxRanges = 5

// Server wires the artifact store, clock and range policy into an http.Handler.
type Server struct {
	store       *artifact.Store
	clk         clock.Clock
	logger      *log.Logger
	gzipEnabled bool
	maxRanges   int
	gzip        *gzipCache
}

// Option customizes a Server at construction.
type Option func(*Server)

// WithLogger sets the structured event sink (defaults to a discarding logger).
func WithLogger(l *log.Logger) Option {
	return func(s *Server) { s.logger = l }
}

// WithGzip turns on the gzip selected representation (off by default).
func WithGzip() Option {
	return func(s *Server) { s.gzipEnabled = true }
}

// WithMaxRanges overrides the per-request range member cap.
func WithMaxRanges(n int) Option {
	return func(s *Server) {
		if n > 0 {
			s.maxRanges = n
		}
	}
}

// New builds the service handler.
func New(store *artifact.Store, clk clock.Clock, opts ...Option) *Server {
	s := &Server{
		store:     store,
		clk:       clk,
		logger:    log.New(io.Discard, "", 0),
		maxRanges: DefaultMaxRanges,
		gzip:      newGzipCache(),
	}
	for _, o := range opts {
		o(s)
	}
	return s
}

// Routes returns the HTTP mux used by the service.
func (s *Server) Routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.handleIndex)
	mux.HandleFunc("/artifacts/", s.handleArtifact)
	return mux
}

func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeError(w, http.StatusNotFound, "no such route")
		return
	}
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	ids := s.store.IDs()
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(struct {
		Artifacts []string `json:"artifacts"`
	}{Artifacts: ids})
}

func (s *Server) handleArtifact(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}

	id := strings.TrimPrefix(r.URL.Path, "/artifacts/")
	if id == "" || strings.Contains(id, "/") {
		writeError(w, http.StatusNotFound, "artifact not found")
		return
	}
	art, err := s.store.Get(id)
	if err != nil {
		writeError(w, http.StatusNotFound, "artifact not found")
		return
	}

	rep, err := s.selectRepresentation(art, r.Header.Get("Accept-Encoding"))
	if err != nil {
		s.logger.Printf("representation error for %q: %v", id, err)
		writeError(w, http.StatusInternalServerError, "internal error")
		return
	}

	h := w.Header()
	h.Set("ETag", rep.etag)
	h.Set("Last-Modified", art.LastModified().UTC().Format(http.TimeFormat))
	h.Set("Accept-Ranges", "bytes")
	h.Set("X-Range-Member-Limit", strconv.Itoa(s.maxRanges))
	if rep.encoding != "" {
		h.Set("Content-Encoding", rep.encoding)
		h.Add("Vary", "Accept-Encoding")
	}
	h.Set("Content-Type", rep.contentType)

	// Conditional GET: If-None-Match takes precedence over If-Modified-Since.
	if status, ok := checkNotModified(r, rep, art.LastModified()); ok {
		h.Del("Content-Length")
		w.WriteHeader(status)
		return
	}

	rangeHeader := r.Header.Get("Range")
	if rangeHeader == "" {
		s.serveFull(w, r, rep)
		return
	}

	raw, perr := rangespec.Parse(rangeHeader)
	if perr != nil {
		// RFC 9110: an invalid Range header is ignored; answer the full rep.
		s.logger.Printf("ignoring malformed Range %q: %v", rangeHeader, perr)
		s.serveFull(w, r, rep)
		return
	}

	if !ifRangeAllowsRange(r.Header.Get("If-Range"), rep, art.LastModified()) {
		s.logger.Printf("If-Range mismatch for %q; serving full representation", id)
		s.serveFull(w, r, rep)
		return
	}

	if len(raw) > s.maxRanges {
		// RFC 9110 §14.1: a server MAY ignore a Range header it does not
		// support or wish to handle. A huge range set is an
		// amplification/DoS vector (multipart framing per member), so when
		// the cap is exceeded we ignore the header and return the complete
		// representation with 200, as for an unsupported range.
		s.logger.Printf("range set of %d exceeds limit %d for %q; ignoring",
			len(raw), s.maxRanges, id)
		s.serveFull(w, r, rep)
		return
	}

	resolved, rerr := rangespec.Resolve(raw, rep.size())
	if rerr != nil {
		h.Set("Content-Range", rangespec.UnsatisfiableContentRange(rep.size()))
		writeError(w, http.StatusRequestedRangeNotSatisfiable,
			"requested range not satisfiable")
		return
	}

	s.serveRanges(w, r, art, rep, resolved)
}

// serveFull answers 200 with the complete selected representation.
func (s *Server) serveFull(w http.ResponseWriter, r *http.Request, rep representation) {
	w.Header().Set("Content-Length", fmt.Sprintf("%d", rep.size()))
	w.WriteHeader(http.StatusOK)
	if r.Method == http.MethodHead {
		return
	}
	_, _ = w.Write(rep.data)
}

// serveRanges answers 206 for one or many resolved ranges.
func (s *Server) serveRanges(w http.ResponseWriter, r *http.Request, art *artifact.Artifact,
	rep representation, resolved []rangespec.Resolved) {
	if len(resolved) == 1 {
		rs := resolved[0]
		w.Header().Set("Content-Range", rs.ContentRange(rep.size()))
		w.Header().Set("Content-Length", fmt.Sprintf("%d", rs.Length()))
		w.WriteHeader(http.StatusPartialContent)
		if r.Method == http.MethodHead {
			return
		}
		// Slice the selected representation: offsets index rep.data, which
		// may be the gzip encoding rather than the artifact's identity bytes.
		_, _ = w.Write(rep.data[rs.Start : rs.End+1])
		return
	}

	// Multiple ranges: the ranges index the (possibly encoded) representation,
	// so slice rep.data rather than the artifact's identity bytes.
	parts := make([]mpart.Part, len(resolved))
	for i, rs := range resolved {
		payload := make([]byte, rs.Length())
		copy(payload, rep.data[rs.Start:rs.End+1])
		parts[i] = mpart.Part{
			ContentType: rep.contentType,
			Range:       rs,
			Total:       rep.size(),
			Payload:     payload,
		}
	}
	boundary := mpart.NewBoundary()
	w.Header().Set("Content-Type", mpart.MediaType(boundary))
	w.Header().Set("Content-Length", fmt.Sprintf("%d", mpart.Size(parts, boundary)))
	w.WriteHeader(http.StatusPartialContent)
	if r.Method == http.MethodHead {
		return
	}
	if err := mpart.Write(w, parts, boundary); err != nil {
		s.logger.Printf("multipart write error for %q: %v", art.ID(), err)
	}
}

// checkNotModified evaluates If-None-Match / If-Modified-Since on a GET/HEAD.
// Returns 304 when the client's cache is current.
func checkNotModified(r *http.Request, rep representation, lastModified time.Time) (int, bool) {
	if inm := r.Header.Get("If-None-Match"); inm != "" {
		if noneMatch(inm, rep.etag) {
			return http.StatusNotModified, true
		}
		return 0, false
	}
	if ims := r.Header.Get("If-Modified-Since"); ims != "" {
		t, err := http.ParseTime(ims)
		if err != nil {
			return 0, false
		}
		if !lastModified.UTC().Truncate(time.Second).After(t) {
			return http.StatusNotModified, true
		}
	}
	return 0, false
}

// noneMatch evaluates an If-None-Match list; "*" matches any representation.
func noneMatch(field, current string) bool {
	for _, token := range strings.Split(field, ",") {
		tag := trimOWS(token)
		if tag == "*" {
			return true
		}
		if tag == "" || isWeakETag(tag) {
			continue
		}
		if tag == current {
			return true
		}
	}
	return false
}

func writeError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(status)
	_, _ = fmt.Fprintln(w, msg)
}

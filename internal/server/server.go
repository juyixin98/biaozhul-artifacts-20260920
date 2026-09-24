// Package server exposes the build cache over HTTP.
package server

import (
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"strings"
	"time"

	"buildcache/internal/builder"
	"buildcache/internal/store"
)

// Server holds HTTP handlers.
type Server struct {
	b   *builder.Builder
	mux *http.ServeMux
}

// New wires routes.
func New(b *builder.Builder) *Server {
	s := &Server{b: b, mux: http.NewServeMux()}
	s.mux.HandleFunc("GET /healthz", s.handleHealth)
	s.mux.HandleFunc("POST /audit/key", s.handleAuditKey)
	s.mux.HandleFunc("POST /builds", s.handleBuild)
	s.mux.HandleFunc("GET /builds/{key}", s.handleGet)
	s.mux.HandleFunc("GET /builds/{key}/artifact", s.handleArtifact)
	s.mux.HandleFunc("GET /builds/{key}/quarantine", s.handleQuarantine)
	return s
}

// Handler returns the root handler.
func (s *Server) Handler() http.Handler {
	return logRequests(s.mux)
}

func logRequests(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		h.ServeHTTP(w, r)
		log.Printf("%s %s %s", r.Method, r.URL.Path, time.Since(start))
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]any{"error": map[string]string{"code": code, "message": msg}})
}

const maxBodyBytes = 16 << 20 // 16 MiB

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// auditRequest is the decode shape: inline sources come in as base64 strings.
type auditRequest struct {
	builder.Request
	WaitSeconds int `json:"wait_seconds,omitempty"`
}

func decodeBuildBody(w http.ResponseWriter, r *http.Request) (*auditRequest, bool) {
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
	var raw struct {
		Toolchain      string            `json:"toolchain"`
		VersionCommand []string          `json:"version_command"`
		Command        []string          `json:"command"`
		Args           []string          `json:"args"`
		Target         map[string]string `json:"target"`
		Env            map[string]string `json:"env"`
		Sources        []struct {
			Path     string  `json:"path"`
			Optional bool    `json:"optional"`
			Inline   *string `json:"inline_b64"`
		} `json:"sources"`
		Artifact       string `json:"artifact"`
		TimeoutSeconds int    `json:"timeout_seconds"`
		WaitSeconds    int    `json:"wait_seconds"`
	}
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&raw); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid_json", err.Error())
		return nil, false
	}
	req := &auditRequest{WaitSeconds: raw.WaitSeconds}
	req.ToolchainName = raw.Toolchain
	req.VersionCommand = raw.VersionCommand
	req.CommandTemplate = raw.Command
	req.Args = raw.Args
	req.Target = raw.Target
	req.Env = raw.Env
	req.Artifact = raw.Artifact
	req.TimeoutSeconds = raw.TimeoutSeconds
	for _, sc := range raw.Sources {
		var data []byte
		if sc.Inline != nil {
			// Field present: an explicit empty string means a present,
			// zero-byte file; absent field means "read from source root".
			d, err := decodeBase64(*sc.Inline)
			if err != nil {
				writeErr(w, http.StatusBadRequest, "invalid_inline_b64",
					"source "+sc.Path+": "+err.Error())
				return nil, false
			}
			data = d
		}
		req.Sources = append(req.Sources, builder.SourceInput{
			Path: sc.Path, Optional: sc.Optional, Inline: data,
		})
	}
	return req, true
}

func (s *Server) handleAuditKey(w http.ResponseWriter, r *http.Request) {
	req, ok := decodeBuildBody(w, r)
	if !ok {
		return
	}
	m, raw, err := s.b.ComputeMaterial(r.Context(), &req.Request)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "key_material", err.Error())
		return
	}
	_, cacheKey, err := builder.KeyOnly(m)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "key", err.Error())
		return
	}
	// Echo manifest with digest so callers can audit missing-vs-empty.
	writeJSON(w, http.StatusOK, map[string]any{
		"key":                cacheKey,
		"canonical_material": json.RawMessage(raw),
		"source_manifest":    m.Sources,
	})
}

func (s *Server) handleBuild(w http.ResponseWriter, r *http.Request) {
	req, ok := decodeBuildBody(w, r)
	if !ok {
		return
	}
	wait := 30 * time.Second
	if req.WaitSeconds > 0 {
		if req.WaitSeconds > 600 {
			req.WaitSeconds = 600
		}
		wait = time.Duration(req.WaitSeconds) * time.Second
	}
	out, err := s.b.Build(r.Context(), &req.Request, wait)
	if err != nil {
		var br *builder.BadRequestError
		switch {
		case errors.As(err, &br):
			writeErr(w, http.StatusBadRequest, "bad_request", br.Msg)
		case errors.Is(err, builder.ErrConflict):
			writeErr(w, http.StatusConflict, "build_in_progress",
				"another publisher is building this key; poll GET /builds/{key}")
		default:
			writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		}
		return
	}
	status := http.StatusOK
	if !out.Hit {
		status = http.StatusCreated // freshly built and published
	}
	writeJSON(w, status, out)
}

func validKey(k string) bool {
	if !strings.HasPrefix(k, "bck1-") {
		return false
	}
	hex := strings.TrimPrefix(k, "bck1-")
	if len(hex) != 64 {
		return false
	}
	for _, c := range hex {
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return false
		}
	}
	return true
}

func (s *Server) handleGet(w http.ResponseWriter, r *http.Request) {
	k := r.PathValue("key")
	if !validKey(k) {
		writeErr(w, http.StatusBadRequest, "bad_key", "key must be bck1-<64 lowercase hex>")
		return
	}
	e, err := s.b.Inspect(r.Context(), k)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	if e == nil {
		writeErr(w, http.StatusNotFound, "not_found", "no entry for key")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"key": e.Key, "status": e.Status, "attempt": e.Attempt,
		"artifact_sha": e.ArtifactSHA, "artifact_size": e.ArtifactSize,
		"exit_code": e.ExitCode, "stdout": e.Stdout, "stderr": e.Stderr,
		"error":      e.ErrMessage,
		"created_at": e.CreatedAt.Format(time.RFC3339Nano),
		"updated_at": e.UpdatedAt.Format(time.RFC3339Nano),
	})
}

func (s *Server) handleQuarantine(w http.ResponseWriter, r *http.Request) {
	k := r.PathValue("key")
	if !validKey(k) {
		writeErr(w, http.StatusBadRequest, "bad_key", "invalid key shape")
		return
	}
	evs, err := s.b.Events(r.Context(), k)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	if evs == nil {
		evs = []store.QuarantineEvent{}
	}
	out := make([]map[string]any, 0, len(evs))
	for _, q := range evs {
		out = append(out, map[string]any{
			"id": q.ID, "key": q.Key, "reason": q.Reason, "detail": q.Detail,
			"expected_sha": q.OldSHA, "observed_sha": q.ObservedSHA,
			"created_at": q.CreatedAt.Format(time.RFC3339Nano),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": out})
}

func (s *Server) handleArtifact(w http.ResponseWriter, r *http.Request) {
	k := r.PathValue("key")
	if !validKey(k) {
		writeErr(w, http.StatusBadRequest, "bad_key", "invalid key shape")
		return
	}
	rc, e, q, err := s.b.ArtifactReader(r.Context(), k)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeErr(w, http.StatusNotFound, "not_found", "no entry for key")
			return
		}
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	if q != nil {
		writeJSON(w, http.StatusGone, map[string]any{
			"error": map[string]string{
				"code":    "quarantined",
				"message": "artifact failed verification and was isolated; rebuild to repopulate",
			},
			"reason":       q.Reason,
			"detail":       q.Detail,
			"expected_sha": q.OldSHA,
			"observed_sha": q.ObservedSHA,
		})
		return
	}
	if rc == nil {
		writeErr(w, http.StatusConflict, "not_succeeded",
			"entry status is "+e.Status+", no artifact available")
		return
	}
	defer rc.Close()
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("X-Artifact-SHA256", e.ArtifactSHA)
	w.Header().Set("Content-Length", itoa(e.ArtifactSize))
	_, _ = io.Copy(w, rc)
}

func itoa(n int64) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}

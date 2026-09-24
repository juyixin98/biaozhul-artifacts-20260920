package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"

	"github.com/example/rollout/internal/crypto"
	"github.com/example/rollout/internal/store"
)

type bodyCtxKey struct{}

func readBody(r *http.Request) ([]byte, error) {
	if r.Body == nil {
		return []byte{}, nil
	}
	defer r.Body.Close()
	return io.ReadAll(r.Body)
}

func withBody(ctx context.Context, body []byte) context.Context {
	return context.WithValue(ctx, bodyCtxKey{}, body)
}

func bodyFrom(ctx context.Context) []byte {
	if v, ok := ctx.Value(bodyCtxKey{}).([]byte); ok {
		return v
	}
	return nil
}

// requestBody returns the body bytes, restoring it for handlers that the auth
// middleware may not have run (tests).
func requestBody(r *http.Request) ([]byte, error) {
	if b := bodyFrom(r.Context()); b != nil {
		return b, nil
	}
	return readBody(r)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// recordingWriter captures the handler's response for idempotent caching.
type recordingWriter struct {
	http.ResponseWriter
	status int
	buf    bytes.Buffer
}

func (rw *recordingWriter) WriteHeader(code int) {
	rw.status = code
	rw.ResponseWriter.WriteHeader(code)
}

func (rw *recordingWriter) Write(b []byte) (int, error) {
	if rw.status == 0 {
		rw.status = http.StatusOK
	}
	// Don't cache internal errors; still pass through.
	if rw.status < 500 {
		rw.buf.Write(b)
	}
	return rw.ResponseWriter.Write(b)
}

// idempotency caches the first response per Idempotency-Key. A retry of the
// same signed envelope after a successful application is handled separately by
// the generation check; this layer makes retries of the SAME request safe by
// returning the stored response verbatim.
type idemStore interface {
	GetIdempotentResponse(ctx context.Context, digest string) (store.StoredResponse, bool, error)
	PutIdempotentResponse(ctx context.Context, digest string, resp store.StoredResponse) error
}

func idempotency(s idemStore) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			key := r.Header.Get(crypto.IdempotencyHeader)
			if key == "" {
				next.ServeHTTP(w, r)
				return
			}
			digest := crypto.HashKey(r.Method + ":" + r.URL.Path + ":" + key)
			if cached, ok, _ := s.GetIdempotentResponse(r.Context(), digest); ok {
				w.Header().Set("Content-Type", "application/json")
				w.Header().Set("Idempotent-Replay", "true")
				w.WriteHeader(cached.Status)
				_, _ = w.Write(cached.Body)
				return
			}
			rw := &recordingWriter{ResponseWriter: w}
			next.ServeHTTP(rw, r)
			if rw.status > 0 && rw.status < 500 && rw.buf.Len() > 0 {
				_ = s.PutIdempotentResponse(r.Context(), digest, store.StoredResponse{
					Status: rw.status,
					Body:   rw.buf.Bytes(),
				})
			}
		})
	}
}

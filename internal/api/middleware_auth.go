package api

import (
	"context"
	"net/http"
	"time"

	"github.com/example/rollout/internal/crypto"
)

type ctxKey string

const releaseIDKey ctxKey = "releaseID"

// nonceStore is the anti-replay store the auth middleware needs.
type nonceStore interface {
	ConsumeNonce(ctx context.Context, digest string, expiresAtUnix int64) (bool, error)
}

// hmacAuth enforces real HMAC-SHA256 signatures on mutating requests:
//
//	X-Rollout-Signature: kid="...",ts=<unix>,sig=<hex HMAC of METHOD\nPATH\nTS\nSHA256(body)>
//
// Replay protection binds the one-time use to (timestamp, request) so a
// captured request cannot be resent within or beyond its time window.
func hmacAuth(keyID string, secret []byte, store nonceStore) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method == http.MethodGet || r.Method == http.MethodHead || r.Method == http.MethodOptions {
				next.ServeHTTP(w, r)
				return
			}

			header := r.Header.Get(crypto.HMACHeader)
			if header == "" {
				writeError(w, http.StatusUnauthorized, "missing "+crypto.HMACHeader+" header")
				return
			}

			// Read the body fully so it can be both verified and re-used by handlers.
			body, err := readBody(r)
			if err != nil {
				writeError(w, http.StatusBadRequest, "unreadable body: "+err.Error())
				return
			}
			ph, err := crypto.Verify(header, r.Method, r.URL.Path, time.Now(), body, keyID, secret)
			if err != nil {
				writeError(w, http.StatusUnauthorized, err.Error())
				return
			}

			// One-time nonce: the random per-request nonce may be used exactly
			// once, so a captured request cannot be replayed.
			nonceDigest := crypto.HashKey(ph.Nonce)
			ok, err := store.ConsumeNonce(r.Context(), nonceDigest, ph.TS+int64(crypto.MaxSkew.Seconds()))
			if err != nil {
				writeError(w, http.StatusInternalServerError, "nonce store error: "+err.Error())
				return
			}
			if !ok {
				writeError(w, http.StatusUnauthorized, "replay detected: signed request already used")
				return
			}

			// Hand the buffered body back to downstream handlers.
			r = r.WithContext(withBody(r.Context(), body))
			next.ServeHTTP(w, r)
		})
	}
}

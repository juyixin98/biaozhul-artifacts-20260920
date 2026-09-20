// Package auth authenticates API callers with static API keys.
// Keys are stored only as SHA-256 hashes; no external identity provider
// is contacted.
package auth

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"

	"sircc/internal/store"
)

type ctxKey string

const userCtxKey ctxKey = "sircc.user"

// Middleware extracts a bearer/API key, hashes it, and loads the user.
func Middleware(pool *pgxpool.Pool) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			key := extractKey(r)
			if key == "" {
				writeUnauthorized(w, "missing API key")
				return
			}
			sum := sha256.Sum256([]byte(key))
			hash := hex.EncodeToString(sum[:])
			user, err := store.New(pool).UserByAPIKeyHash(r.Context(), hash)
			if err != nil {
				writeUnauthorized(w, "invalid API key")
				return
			}
			ctx := context.WithValue(r.Context(), userCtxKey, &user)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

func extractKey(r *http.Request) string {
	if h := r.Header.Get("Authorization"); strings.HasPrefix(h, "Bearer ") {
		return strings.TrimPrefix(h, "Bearer ")
	}
	return r.Header.Get("X-API-Key")
}

// User returns the authenticated user, or nil.
func User(ctx context.Context) *store.User {
	u, _ := ctx.Value(userCtxKey).(*store.User)
	return u
}

func writeUnauthorized(w http.ResponseWriter, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusUnauthorized)
	_, _ = w.Write([]byte(`{"error":"unauthorized","message":"` + msg + `"}`))
}

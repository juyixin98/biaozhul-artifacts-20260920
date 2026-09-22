// Package auth implements static API-key authentication and role-based access
// control. Keys are presented as `Authorization: Bearer <key>`. Only the
// SHA-256 of a key is stored.
package auth

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"net/http"
	"strings"

	"dams/internal/db"
	"dams/internal/platform/httpx"

	"github.com/jackc/pgx/v5/pgxpool"
)

type Role string

const (
	RoleAdmin     Role = "admin"
	RoleAnalyst   Role = "analyst"
	RoleAuditor   Role = "auditor"
	RoleCollector Role = "collector"
)

// Principal is the authenticated caller. Every principal is scoped to exactly
// one organization; analysts therefore can only ever touch their own org's
// alerts (the database and handlers both re-check org_id).
type Principal struct {
	APIKeyID int64
	OrgID    int64
	Name     string
	Role     Role
}

type ctxKey int

const principalKey ctxKey = 1

// HashKey returns the stored form of an API key: hex sha256 over a fixed
// domain-separation prefix plus the key, so it can never collide with the
// sha256 content hashes of events.
func HashKey(key string) string {
	sum := sha256.Sum256([]byte("dams-apikey-v1:" + key))
	return hex.EncodeToString(sum[:])
}

// Middleware authenticates the bearer token.
func Middleware(pool *pgxpool.Pool) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			key := bearer(r)
			if key == "" {
				httpx.Error(w, http.StatusUnauthorized, "missing bearer token", nil)
				return
			}
			row, err := db.New(pool).GetAPIKeyByHash(r.Context(), HashKey(key))
			if err != nil {
				httpx.Error(w, http.StatusUnauthorized, "invalid api key", nil)
				return
			}
			p := &Principal{
				APIKeyID: row.ID,
				OrgID:    row.OrgID,
				Name:     row.Name,
				Role:     Role(row.Role),
			}
			next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), principalKey, p)))
		})
	}
}

func bearer(r *http.Request) string {
	h := r.Header.Get("Authorization")
	if h == "" {
		return ""
	}
	if len(h) > 7 && strings.EqualFold(h[:7], "Bearer ") {
		return strings.TrimSpace(h[7:])
	}
	return ""
}

// FromContext returns the authenticated principal.
func FromContext(ctx context.Context) (*Principal, error) {
	p, ok := ctx.Value(principalKey).(*Principal)
	if !ok || p == nil {
		return nil, errors.New("unauthenticated")
	}
	return p, nil
}

// Must is for handlers already behind Middleware; it writes 401 on misuse.
func Must(w http.ResponseWriter, r *http.Request) *Principal {
	p, err := FromContext(r.Context())
	if err != nil {
		httpx.Error(w, http.StatusUnauthorized, "unauthenticated", nil)
		return nil
	}
	return p
}

// RequireRole authorizes the principal for one of the given roles.
func RequireRole(p *Principal, roles ...Role) bool {
	for _, r := range roles {
		if subtle.ConstantTimeCompare([]byte(p.Role), []byte(r)) == 1 {
			return true
		}
	}
	return false
}

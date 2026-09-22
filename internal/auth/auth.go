// Package auth implements static API-token authentication and role-based
// authorization scoped to organizations.
//
// Roles:
//   - admin: everything, across all organizations.
//   - analyst: read on granted orgs, import into accounts of granted orgs.
//   - viewer: read-only on granted orgs.
package auth

import (
	"context"
	"net/http"
	"strings"

	"costlens/internal/db"
	"github.com/jackc/pgx/v5/pgxpool"
)

type ctxKey int

const principalKey ctxKey = iota

type Principal struct {
	ID       int64
	Username string
	Role     string
	// GrantOrgs is the set of organization IDs the user may access.
	// A nil map for admin means "all organizations".
	GrantOrgs map[int64]bool
}

func (p *Principal) IsAdmin() bool { return p.Role == "admin" }

// CanAccessOrg reports read visibility of an organization.
func (p *Principal) CanAccessOrg(orgID int64) bool {
	if p.IsAdmin() {
		return true
	}
	return p.GrantOrgs[orgID]
}

// CanImportOrg reports import rights: admin always; analyst only for a
// granted organization.
func (p *Principal) CanImportOrg(orgID int64) bool {
	if p.IsAdmin() {
		return true
	}
	return p.Role == "analyst" && p.GrantOrgs[orgID]
}

// OrgIDs returns the concrete organization ID list for non-admin queries;
// nil means "no filter / all orgs" (admin).
func (p *Principal) OrgIDs() []int64 {
	if p.IsAdmin() {
		return nil
	}
	ids := make([]int64, 0, len(p.GrantOrgs))
	for id := range p.GrantOrgs {
		ids = append(ids, id)
	}
	return ids
}

func FromContext(ctx context.Context) *Principal {
	p, _ := ctx.Value(principalKey).(*Principal)
	return p
}

// Middleware rejects requests without a valid Bearer token.
func Middleware(pool *pgxpool.Pool) func(http.Handler) http.Handler {
	q := db.New(pool)
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			tok := bearerToken(r)
			if tok == "" {
				w.Header().Set("WWW-Authenticate", `Bearer realm="costlens"`)
				http.Error(w, `{"error":"missing bearer token"}`, http.StatusUnauthorized)
				return
			}
			u, err := q.GetUserByToken(r.Context(), tok)
			if err != nil {
				http.Error(w, `{"error":"invalid token"}`, http.StatusUnauthorized)
				return
			}
			p := &Principal{ID: u.ID, Username: u.Username, Role: u.Role}
			if !p.IsAdmin() {
				orgIDs, err := q.ListGrantedOrgIDs(r.Context(), u.ID)
				if err != nil {
					http.Error(w, `{"error":"authorization lookup failed"}`, http.StatusInternalServerError)
					return
				}
				p.GrantOrgs = make(map[int64]bool, len(orgIDs))
				for _, id := range orgIDs {
					p.GrantOrgs[id] = true
				}
			}
			next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), principalKey, p)))
		})
	}
}

func bearerToken(r *http.Request) string {
	h := r.Header.Get("Authorization")
	if h == "" {
		return ""
	}
	const prefix = "Bearer "
	if len(h) > len(prefix) && strings.EqualFold(h[:len(prefix)], prefix) {
		return strings.TrimSpace(h[len(prefix):])
	}
	return ""
}

// RequireAdmin wraps an admin-only handler.
func RequireAdmin(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		p := FromContext(r.Context())
		if p == nil || !p.IsAdmin() {
			http.Error(w, `{"error":"admin role required"}`, http.StatusForbidden)
			return
		}
		h(w, r)
	}
}

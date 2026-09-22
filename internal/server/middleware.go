package server

import (
	"context"
	"errors"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"

	"dams.local/dams/internal/auth"
)

type ctxKey int

const (
	ctxPrincipal ctxKey = iota
	ctxOrg
)

func recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				log.Printf("panic: %v %s", rec, r.URL.Path)
				writeError(w, http.StatusInternalServerError, "internal_error", "internal error", nil)
			}
		}()
		next.ServeHTTP(w, r)
	})
}

func requestLogger(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		ww := &statusWriter{ResponseWriter: w, status: 200}
		next.ServeHTTP(ww, r)
		log.Printf("%s %s -> %d (%s)", r.Method, r.URL.RequestURI(), ww.status, time.Since(start))
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (s *statusWriter) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

// authenticate validates the Authorization: Bearer token and stores the
// principal in the request context.
func (s *Server) authenticate(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := r.Header.Get("Authorization")
		if h == "" || !strings.HasPrefix(h, "Bearer ") {
			writeError(w, http.StatusUnauthorized, "unauthenticated",
				"missing bearer token", nil)
			return
		}
		token := strings.TrimPrefix(h, "Bearer ")
		p, err := auth.Authenticate(r.Context(), s.pool, token)
		if err != nil {
			if errors.Is(err, auth.ErrUnauthenticated) {
				writeError(w, http.StatusUnauthorized, "unauthenticated", "invalid token", nil)
				return
			}
			log.Printf("auth error: %v", err)
			writeError(w, http.StatusInternalServerError, "internal_error", "internal error", nil)
			return
		}
		ctx := context.WithValue(r.Context(), ctxPrincipal, p)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func principal(r *http.Request) auth.Principal {
	return r.Context().Value(ctxPrincipal).(auth.Principal)
}

// resolveOrgMiddleware loads the org referenced by {org} (slug) and enforces
// membership before the per-route role check or handler runs. Non-members
// cannot distinguish "org missing" from "forbidden" and always get 403 (or
// 404 only when the org genuinely does not exist, to avoid confirming
// existence it returns 404 for non-members too — see below).
func (s *Server) resolveOrgMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		slug := chi.URLParam(r, "org")
		p := principal(r)
		row, err := s.q.GetOrg(r.Context(), slug)
		if errors.Is(err, pgx.ErrNoRows) {
			writeError(w, http.StatusNotFound, "not_found", "organization not found", nil)
			return
		}
		if err != nil {
			writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
			return
		}
		org := orgContext{ID: row.ID, Slug: row.Slug, Name: row.Name, Timezone: row.Timezone}
		if _, member := p.RoleIn(org.ID); !member {
			writeError(w, http.StatusForbidden, "forbidden",
				"not a member of this organization", nil)
			return
		}
		ctx := context.WithValue(r.Context(), ctxOrg, org)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// requireRole allows only members of the route organization with one of the
// listed roles. resolveOrgMiddleware must run first.
func (s *Server) requireRole(roles ...auth.Role) func(http.Handler) http.Handler {
	allowed := make(map[auth.Role]bool, len(roles))
	for _, r := range roles {
		allowed[r] = true
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			org, ok := r.Context().Value(ctxOrg).(orgContext)
			if !ok {
				writeError(w, http.StatusInternalServerError, "internal_error", "org not resolved", nil)
				return
			}
			p := principal(r)
			role, member := p.RoleIn(org.ID)
			if !member {
				writeError(w, http.StatusForbidden, "forbidden",
					"not a member of this organization", nil)
				return
			}
			if !allowed[role] {
				writeError(w, http.StatusForbidden, "forbidden",
					"role "+string(role)+" may not perform this action", nil)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

type orgContext struct {
	ID       int64
	Slug     string
	Name     string
	Timezone string
}

// currentOrg returns the org resolved by resolveOrgMiddleware.
func currentOrg(r *http.Request) orgContext {
	return r.Context().Value(ctxOrg).(orgContext)
}

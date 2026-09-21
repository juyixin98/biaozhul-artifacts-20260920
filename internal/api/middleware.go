package api

import (
	"context"
	"net/http"
	"strings"

	"github.com/clearsettle/clearsettle/internal/domain"
	"github.com/clearsettle/clearsettle/internal/service"
)

type ctxKey string

const actorKey ctxKey = "actor"

func (s *Server) authenticate(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw := r.Header.Get("Authorization")
		if raw == "" {
			raw = r.Header.Get("X-Api-Key")
		}
		raw = strings.TrimPrefix(raw, "Bearer ")
		if strings.TrimSpace(raw) == "" {
			writeErr(w, domain.ErrForbidden)
			return
		}
		actor, err := s.svc.Authenticate(r.Context(), strings.TrimSpace(raw))
		if err != nil {
			writeErr(w, domain.ErrForbidden)
			return
		}
		ctx := context.WithValue(r.Context(), actorKey, actor)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func actorFrom(r *http.Request) service.Actor {
	return r.Context().Value(actorKey).(service.Actor)
}

func requireRole(roles ...string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			actor := actorFrom(r)
			for _, role := range roles {
				if actor.Role == role {
					next.ServeHTTP(w, r)
					return
				}
			}
			writeErr(w, domain.ErrForbidden)
		})
	}
}

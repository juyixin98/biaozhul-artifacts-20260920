package api

import (
	"net/http"

	"dams/internal/platform/auth"
	"dams/internal/platform/httpx"
)

func requireRole(roles ...auth.Role) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			p, err := auth.FromContext(r.Context())
			if err != nil {
				httpx.Error(w, http.StatusUnauthorized, "unauthenticated", nil)
				return
			}
			if !auth.RequireRole(p, roles...) {
				httpx.Error(w, http.StatusForbidden,
					"role "+string(p.Role)+" is not permitted to perform this action", nil)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

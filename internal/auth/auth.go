// Package auth implements the test authentication context. The tenant ID is
// established exclusively by the X-Tenant-ID header (the stand-in for a real
// identity provider); request bodies are never trusted for identity.
package auth

import (
	"context"
	"net/http"
	"regexp"
)

// HeaderTenant is the header carrying the authenticated tenant ID.
const HeaderTenant = "X-Tenant-ID"

var tenantPattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`)

type ctxKey struct{}

// ValidTenant reports whether id is an acceptable tenant identifier.
func ValidTenant(id string) bool {
	return tenantPattern.MatchString(id)
}

// WithTenant returns a context carrying the authenticated tenant ID.
func WithTenant(ctx context.Context, tenantID string) context.Context {
	return context.WithValue(ctx, ctxKey{}, tenantID)
}

// TenantFrom extracts the tenant ID from ctx.
func TenantFrom(ctx context.Context) (string, bool) {
	id, ok := ctx.Value(ctxKey{}).(string)
	return id, ok
}

// Middleware rejects unauthenticated requests and installs the tenant ID
// from the test auth header into the request context. Identity comes only
// from this header; handlers must reject any tenant field in the body.
func Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		tenant := r.Header.Get(HeaderTenant)
		if tenant == "" {
			http.Error(w, `{"error":{"code":"UNAUTHENTICATED","message":"missing `+HeaderTenant+` header"}}`,
				http.StatusUnauthorized)
			return
		}
		if !ValidTenant(tenant) {
			http.Error(w, `{"error":{"code":"INVALID_TENANT","message":"tenant ID fails validation"}}`,
				http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r.WithContext(WithTenant(r.Context(), tenant)))
	})
}

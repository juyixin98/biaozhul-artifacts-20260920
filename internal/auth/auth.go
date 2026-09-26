// Package auth provides the test authentication context. The tenant ID
// is established exclusively by the auth layer (X-Tenant-ID header,
// standing in for a real identity token); request bodies can never
// override it.
package auth

import (
	"context"
	"net/http"
	"strings"
)

// TenantHeader carries the authenticated tenant identity in this test setup.
const TenantHeader = "X-Tenant-ID"

type ctxKey struct{}

// WithTenant returns a context carrying the authenticated tenant ID.
func WithTenant(ctx context.Context, tenantID string) context.Context {
	return context.WithValue(ctx, ctxKey{}, tenantID)
}

// TenantFrom extracts the authenticated tenant ID from ctx.
func TenantFrom(ctx context.Context) (string, bool) {
	t, ok := ctx.Value(ctxKey{}).(string)
	return t, ok && t != ""
}

// Middleware authenticates the request via the tenant header and stores
// the tenant ID in the request context. Requests without a tenant are
// rejected with 401.
func Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		tenantID := strings.TrimSpace(r.Header.Get(TenantHeader))
		if tenantID == "" {
			http.Error(w, `{"error":"missing tenant identity"}`, http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r.WithContext(WithTenant(r.Context(), tenantID)))
	})
}

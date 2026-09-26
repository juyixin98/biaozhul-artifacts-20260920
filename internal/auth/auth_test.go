package auth_test

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"example.com/tenantiso/internal/auth"
)

func TestMiddlewareExtractsTenant(t *testing.T) {
	var got string
	h := auth.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got, _ = auth.TenantFrom(r.Context())
	}))
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set(auth.TenantHeader, "tenant-a")
	h.ServeHTTP(httptest.NewRecorder(), req)
	if got != "tenant-a" {
		t.Fatalf("tenant = %q", got)
	}
}

func TestMiddlewareRejectsMissingTenant(t *testing.T) {
	h := auth.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("handler must not run without tenant")
	}))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("code = %d", rec.Code)
	}
}

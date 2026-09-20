package handler_test

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/glebarez/sqlite"
	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"proofcycle/internal/handler"
	"proofcycle/internal/migrate"
	"proofcycle/internal/service"
	"proofcycle/internal/storage"
)

func newRouter(t *testing.T) *gin.Engine {
	t.Helper()
	gin.SetMode(gin.TestMode)
	dsn := fmt.Sprintf("file:%s?mode=memory&cache=shared", t.Name())
	db, err := gorm.Open(sqlite.Open(dsn), &gorm.Config{})
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	if err := migrate.Run(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	if err := migrate.SeedDemo(db); err != nil {
		t.Fatalf("seed: %v", err)
	}
	st, err := storage.New(t.TempDir(), 1<<20)
	if err != nil {
		t.Fatalf("storage: %v", err)
	}
	return handler.NewRouter(db, service.New(db, st))
}

func TestAuthRequired(t *testing.T) {
	r := newRouter(t)

	// 无认证头
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/jobs/1", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 without header, got %d", w.Code)
	}

	// 非法用户
	w = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodGet, "/jobs/1", nil)
	req.Header.Set("X-User-ID", "9999")
	r.ServeHTTP(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 for unknown user, got %d", w.Code)
	}

	// 健康检查不需要认证
	w = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodGet, "/healthz", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("healthz should be public, got %d", w.Code)
	}
}

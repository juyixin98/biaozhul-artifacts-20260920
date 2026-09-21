package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"sitevitals/internal/models"
	"sitevitals/internal/policy"
	"sitevitals/internal/store"
)

const policyNonHTTP = policy.ViolationNonHTTP

func apiDB(t *testing.T) *gorm.DB {
	t.Helper()
	dsn := os.Getenv("SV_TEST_DSN")
	if dsn == "" {
		dsn = "root:@tcp(127.0.0.1:3306)/sitevitals_b_api_test?charset=utf8mb4&parseTime=True&loc=UTC&multiStatements=true"
	}
	db, err := store.Open(dsn)
	if err != nil {
		t.Skipf("mysql not available: %v", err)
	}
	if err := store.AutoMigrate(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	for _, m := range models.AllModels() {
		stmt := &gorm.Statement{DB: db}
		if err := stmt.Parse(m); err != nil {
			t.Fatal(err)
		}
		_ = db.Exec("DELETE FROM " + stmt.Schema.Table).Error
	}
	t.Cleanup(func() { sqlDB, _ := db.DB(); _ = sqlDB.Close() })
	return db
}

func TestEnqueueRejectsNonAllowedAndNonHTTP(t *testing.T) {
	db := apiDB(t)
	repo, q := store.NewRepo(db), store.NewQueue(db)
	srv := httptest.NewServer(NewServer(repo, q).Router())
	defer srv.Close()

	// Register one site with one exact rule.
	ctx := context.Background()
	site := &models.Site{Name: "demo", SchemeHost: "http://127.0.0.1:8093", Enabled: true}
	if err := repo.CreateSite(ctx, site); err != nil {
		t.Fatal(err)
	}
	if err := repo.CreateAllowedURL(ctx, &models.AllowedURL{
		SiteID: site.ID, URLPattern: "http://127.0.0.1:8093/", Enabled: true,
	}); err != nil {
		t.Fatal(err)
	}

	post := func(body string) (int, map[string]any) {
		resp, err := http.Post(srv.URL+"/api/v1/jobs", "application/json", bytes.NewBufferString(body))
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var out map[string]any
		_ = json.NewDecoder(resp.Body).Decode(&out)
		return resp.StatusCode, out
	}

	// Non-HTTP scheme.
	code, body := post(`{"url":"file:///etc/passwd","viewport":"desktop"}`)
	if code != http.StatusForbidden {
		t.Fatalf("file:// code=%d body=%v want 403", code, body)
	}
	if body["code"] != policyNonHTTP {
		t.Fatalf("code=%v want %s", body["code"], policyNonHTTP)
	}

	// Whitelisted-origin but non-allowed path.
	code, body = post(`{"url":"http://127.0.0.1:8093/admin","viewport":"mobile"}`)
	if code != http.StatusForbidden {
		t.Fatalf("non-allowed path code=%d body=%v want 403", code, body)
	}

	// Foreign host.
	code, _ = post(`{"url":"http://evil.example.com/","viewport":"tablet"}`)
	if code != http.StatusForbidden {
		t.Fatalf("foreign host code=%d want 403", code)
	}

	// Bad viewport.
	code, _ = post(`{"url":"http://127.0.0.1:8093/","viewport":"watch"}`)
	if code != http.StatusBadRequest {
		t.Fatalf("bad viewport code=%d want 400", code)
	}

	// Allowed.
	code, body = post(`{"url":"http://127.0.0.1:8093/","viewport":"desktop"}`)
	if code != http.StatusCreated {
		t.Fatalf("allowed enqueue code=%d body=%v want 201", code, body)
	}
}

func TestSiteRegistrationRejectsNonHTTP(t *testing.T) {
	db := apiDB(t)
	repo, q := store.NewRepo(db), store.NewQueue(db)
	srv := httptest.NewServer(NewServer(repo, q).Router())
	defer srv.Close()

	resp, err := http.Post(srv.URL+"/api/v1/sites", "application/json",
		bytes.NewBufferString(`{"name":"bad","scheme_host":"ftp://x"}`))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("ftp site registration status=%d want 400", resp.StatusCode)
	}
}

func TestCompareOnlyUsesSuccessfulRuns(t *testing.T) {
	db := apiDB(t)
	repo, q := store.NewRepo(db), store.NewQueue(db)
	r := gin.New()
	s := NewServer(repo, q)
	r.GET("/compare", s.compare)
	srv := httptest.NewServer(r)
	defer srv.Close()

	const u = "http://127.0.0.1:8093/"
	// One failed run, one succeeded run: compare must report nothing to compare.
	mkRun := func(status string, attempt int) *models.Run {
		r := &models.Run{
			JobID: 1, Attempt: attempt, Viewport: models.ViewportDesktop, TargetURL: u,
			Status: status, LeaseHolder: "h", FencingToken: int64(attempt),
			StartedAt: time.Now(),
		}
		if err := db.Create(r).Error; err != nil {
			t.Fatal(err)
		}
		return r
	}
	mkRun(models.RunFailed, 1)
	ok1 := mkRun(models.RunSucceeded, 2)
	v := 100.0
	if err := db.Create(&models.Metric{RunID: ok1.ID, Name: "fcp_ms", Status: models.MetricCollected, ValueMS: &v}).Error; err != nil {
		t.Fatal(err)
	}

	resp, err := http.Get(srv.URL + "/compare?url=" + u + "&viewport=desktop")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("status=%d want 404 (only one successful run, failed run excluded)", resp.StatusCode)
	}
}

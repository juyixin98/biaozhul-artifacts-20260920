// Package integrationtest exercises DeskLens against a real PostgreSQL.
//
// It runs automatically when TEST_DATABASE_URL points at a Postgres instance
// (docker compose up -d postgres then: make test, which exports the URL), and
// skips otherwise. Every test gets its own freshly migrated schema under a
// unique search_path ("test schema per test") so runs are isolated and can be
// parallelized.
package integrationtest

import (
	"fmt"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jmoiron/sqlx"
	"github.com/labstack/echo/v4"

	"desklens/internal/aggregate"
	"desklens/internal/api"
	"desklens/internal/database"
	"desklens/internal/ingest"
	"desklens/internal/repo"
)

var schemaCounter int64

type Env struct {
	T      *testing.T
	Schema string
	DB     *sqlx.DB
	Repo   *repo.Repo
	Ingest *ingest.Service
	Agg    *aggregate.Service
	Server *httptest.Server
	Echo   *echo.Echo
}

const adminURL = "postgres://desklens:desklens@localhost:5439/desklens?sslmode=disable"

func testURL() string {
	if v := os.Getenv("TEST_DATABASE_URL"); v != "" {
		return v
	}
	return adminURL
}

// NewEnv provisions an isolated Postgres schema with migrations applied.
func NewEnv(t *testing.T) *Env {
	t.Helper()

	root, err := sqlx.Open("pgx", testURL())
	if err != nil {
		t.Skipf("postgres unavailable: %v", err)
	}
	if err := root.Ping(); err != nil {
		root.Close()
		t.Skipf("postgres unavailable: %v", err)
	}

	n := atomic.AddInt64(&schemaCounter, 1)
	dbName := fmt.Sprintf("desklens_it_%d_%d", os.Getpid()&0xffff, n)
	if _, err := root.Exec(`CREATE DATABASE ` + dbName); err != nil {
		root.Close()
		if strings.Contains(err.Error(), "permission denied to create database") {
			t.Skipf("test role lacks CREATEDB: %v", err)
		}
		t.Fatalf("create database: %v", err)
	}
	t.Cleanup(func() {
		// Terminate lingering pooled connections, then drop.
		root.Exec(
			`SELECT pg_terminate_backend(pid) FROM pg_stat_activity
			   WHERE datname = $1 AND pid <> pg_backend_pid()`, dbName)
		_, _ = root.Exec(`DROP DATABASE ` + dbName)
		root.Close()
	})

	// Connect directly to the fresh, isolated database.
	url := strings.Replace(testURL(), "/desklens?", "/"+dbName+"?", 1)
	if !strings.Contains(url, "/"+dbName+"?") {
		// Fallback for URLs without the default db name.
		url = testURL()
	}
	db, err := sqlx.Open("pgx", url)
	if err != nil {
		t.Fatalf("open test database: %v", err)
	}
	t.Cleanup(func() { db.Close() })

	if err := database.Migrate(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	r := repo.New(db)
	ing := ingest.NewService(r)
	agg := aggregate.NewService(r)

	e := echo.New()
	api.NewHandlers(r, ing, agg).Register(e)
	srv := httptest.NewServer(e)
	t.Cleanup(srv.Close)

	return &Env{
		T: t, Schema: dbName, DB: db, Repo: r,
		Ingest: ing, Agg: agg, Server: srv, Echo: e,
	}
}

// snap is a compact constructor for tests.
func snap(ws, emp int64, minute, app string, count int) repo.SnapshotIn {
	t, err := time.Parse(time.RFC3339, minute)
	if err != nil {
		panic(err)
	}
	return repo.SnapshotIn{
		WorkstationID: ws,
		EmployeeID:    emp,
		MinuteUTC:     t,
		AppName:       app,
		ActivityCount: count,
	}
}

func date(s string) time.Time {
	t, err := time.Parse("2006-01-02", s)
	if err != nil {
		panic(err)
	}
	return t.UTC()
}

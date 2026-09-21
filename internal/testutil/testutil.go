package testutil

import (
	"context"
	"io"
	"log"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"forensiccore/internal/api"
	"forensiccore/internal/config"
	"forensiccore/internal/database"
	"forensiccore/internal/jobs"
	"forensiccore/internal/service"

	"github.com/gin-gonic/gin"
	"github.com/glebarez/sqlite"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

// Env is a fully wired in-memory test environment.
type Env struct {
	DB           *gorm.DB
	EvidenceRoot string
	ChunkSize    int
	Service      *service.Service
	Runner       *jobs.Runner
	Cfg          *config.Config
	InvToken     string
	AnToken      string
}

// discardLogger keeps test output quiet.
func discardLogger() *log.Logger { return log.New(io.Discard, "", 0) }

// New creates an environment backed by a shared-cache in-memory SQLite
// database (so the runner's connections see the same data) and a temp
// evidence directory.
func New(t *testing.T, chunkSize int, hook jobs.Hook) *Env {
	t.Helper()
	gin.SetMode(gin.TestMode)

	dir := t.TempDir()
	// File-based SQLite with WAL gives each test an isolated database while
	// letting the runner's pooled connections observe committed progress.
	dsn := filepath.Join(dir, "forensiccore-test.db") + "?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)"
	db, err := gorm.Open(sqlite.Open(dsn), &gorm.Config{
		Logger: logger.Default.LogMode(logger.Silent),
	})
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	sqlDB, _ := db.DB()
	sqlDB.SetMaxOpenConns(1)
	sqlDB.SetMaxIdleConns(1)
	if err := database.Migrate(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	// Evidence lives in a subdirectory so it never collides with the DB file.
	evDir := filepath.Join(dir, "evidence")
	if err := os.MkdirAll(evDir, 0o755); err != nil {
		t.Fatal(err)
	}

	cfg := &config.Config{
		DBDriver:     "sqlite",
		EvidenceRoot: evDir,
		ChunkSize:    chunkSize,
		Principals: map[string]config.Principal{
			"inv-secret": {Name: "alice", Role: config.RoleInvestigator},
			"an-secret":  {Name: "bob", Role: config.RoleAnalyst}},
	}
	svc := &service.Service{DB: db, EvidenceRoot: evDir, ChunkSize: chunkSize}
	runner := jobs.NewRunner(db, evDir, chunkSize, 20*time.Millisecond, hook, discardLogger())
	return &Env{
		DB: db, EvidenceRoot: evDir, ChunkSize: chunkSize, Service: svc, Runner: runner, Cfg: cfg,
		InvToken: "inv-secret", AnToken: "an-secret",
	}
}

// RestartRunner simulates a process restart: it stops the current runner
// (which has a cancelled/used context), requeues jobs left "running", and
// starts a brand new runner over the same database and evidence directory.
func (e *Env) RestartRunner(t *testing.T, hook jobs.Hook) *jobs.Runner {
	t.Helper()
	e.Runner.Stop()
	runner := jobs.NewRunner(e.DB, e.EvidenceRoot, e.ChunkSize, 20*time.Millisecond, hook, discardLogger())
	if err := runner.ResetStale(context.Background()); err != nil {
		t.Fatalf("reset stale: %v", err)
	}
	runner.Start(context.Background())
	t.Cleanup(runner.Stop)
	e.Runner = runner
	return runner
}

// Router returns a gin engine wired like production.
func (e *Env) Router() *gin.Engine {
	srv := &api.Server{DB: e.DB, Service: e.Service, Runner: e.Runner, Cfg: e.Cfg}
	return srv.NewRouter()
}

// HTTPServer returns an httptest server registered for cleanup.
func (e *Env) HTTPServer(t *testing.T) *httptest.Server {
	return httptest.NewServer(e.Router())
}

package testkit

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"forensiccore/internal/api"
	"forensiccore/internal/cases"
	"forensiccore/internal/chain"
	"forensiccore/internal/config"
	"forensiccore/internal/database"
	"forensiccore/internal/evidence"
	"forensiccore/internal/report"
	"forensiccore/internal/review"
	"forensiccore/internal/securefile"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

// Tokens used in tests.
const (
	InvestigatorToken = "inv-secret-token"
	AnalystToken      = "analyst-secret-token"
)

// Env bundles a fully wired application for tests.
type Env struct {
	Root      string // temp root
	Whitelist string // allowed dir
	Outside   string // disallowed dir
	DBFile    string
	Cfg       config.Config
	Gorm      *gorm.DB
	Cases     *cases.Service
	Evidence  *evidence.Service
	Review    *review.Manager
	Chain     *chain.Appender
	Report    *report.Service
	Resolver  *securefile.Resolver
	Engine    *gin.Engine
}

// NewEnv wires a sqlite-backed app in a temp directory with two dirs:
// <root>/vault (whitelisted) and <root>/outside (not whitelisted).
func NewEnv(t *testing.T, chunkSize int) *Env {
	t.Helper()

	root := t.TempDir()
	vault := filepath.Join(root, "vault")
	outside := filepath.Join(root, "outside")
	if err := os.MkdirAll(vault, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(outside, 0o755); err != nil {
		t.Fatal(err)
	}
	dbFile := filepath.Join(root, "forensiccore-test.db")

	db, err := database.Open("sqlite", dbFile, false)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	t.Cleanup(func() {
		sqlDB, _ := db.DB()
		_ = sqlDB.Close()
	})

	events := chain.New(db)
	resolver := securefile.NewResolver([]string{vault})
	caseSvc := cases.New(db, events)
	evSvc := evidence.New(db, resolver, events, chunkSize)
	mgr := review.NewManager(db, evSvc, resolver, events, chunkSize)
	repSvc := report.New(db, events)

	cfg := config.Config{
		HTTPListen:        ":0",
		DBDriver:          "sqlite",
		DSN:               dbFile,
		WhitelistRoots:    []string{vault},
		ChunkSize:         chunkSize,
		InvestigatorToken: InvestigatorToken,
		AnalystToken:      AnalystToken,
		SamplesDir:        vault,
	}
	engine := api.NewServer(cfg, api.Services{
		Cases:    caseSvc,
		Evidence: evSvc,
		Review:   mgr,
		Chain:    events,
		Report:   repSvc,
	})

	return &Env{
		Root:      root,
		Whitelist: vault,
		Outside:   outside,
		DBFile:    dbFile,
		Gorm:      db,
		Cfg:       cfg,
		Cases:     caseSvc,
		Evidence:  evSvc,
		Review:    mgr,
		Chain:     events,
		Report:    repSvc,
		Resolver:  resolver,
		Engine:    engine,
	}
}

// Shutdown checkpoints active jobs.
func (e *Env) Shutdown(t *testing.T) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	e.Review.Shutdown(ctx)
}

// WriteFile writes data into the whitelisted vault and returns its path.
func (e *Env) WriteFile(t *testing.T, name string, data []byte) string {
	t.Helper()
	p := filepath.Join(e.Whitelist, name)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, data, 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

// WriteOutside writes data outside the whitelist.
func (e *Env) WriteOutside(t *testing.T, name string, data []byte) string {
	t.Helper()
	p := filepath.Join(e.Outside, name)
	if err := os.WriteFile(p, data, 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

// CreateCase creates a case directly through the service.
func (e *Env) CreateCase(t *testing.T, name string) string {
	t.Helper()
	kase, _, err := e.Cases.Create(context.Background(), cases.CreateInput{
		Name:  name,
		Actor: "role:investigator",
	})
	if err != nil {
		t.Fatalf("create case: %v", err)
	}
	return kase.ID
}

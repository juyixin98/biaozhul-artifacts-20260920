package servicetest

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/example/forensiccore/internal/config"
	"github.com/example/forensiccore/internal/service"
	"github.com/example/forensiccore/internal/store"
	"gorm.io/gorm"
)

// Env 是测试环境。
type Env struct {
	DB       *gorm.DB
	Svc      *service.Service
	Cfg      *config.Config
	Root     string // 白名单根目录（绝对路径）
	RootName string
}

// NewEnv 建立 sqlite 内存库 + 临时白名单根目录并启动服务。
func NewEnv(t *testing.T) *Env {
	t.Helper()
	dir := t.TempDir()

	db, err := store.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	t.Cleanup(func() {
		sqlDB, _ := db.DB()
		_ = sqlDB.Close()
	})

	cfg := &config.Config{
		DBDriver:      "sqlite",
		DSN:           ":memory:",
		ChunkSize:     4096,
		TokenTTLHours: 1,
		JWTSecret:     []byte("test-secret"),
		Roots: map[string]string{
			"evidence": dir,
		},
		Users: map[string]config.User{
			"investigator": {Password: "pw", Role: config.RoleInvestigator},
			"analyst":      {Password: "pw", Role: config.RoleAnalyst},
		},
	}
	svc, err := service.New(db, cfg)
	if err != nil {
		t.Fatalf("new service: %v", err)
	}
	if err := svc.Start(); err != nil {
		t.Fatalf("start service: %v", err)
	}
	t.Cleanup(svc.Close)

	return &Env{DB: db, Svc: svc, Cfg: cfg, Root: dir, RootName: "evidence"}
}

// WriteImage 在白名单根内写入确定性伪随机镜像，返回相对路径。
func (e *Env) WriteImage(t *testing.T, rel string, size int) string {
	t.Helper()
	full := filepath.Join(e.Root, rel)
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	f, err := os.Create(full)
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	defer f.Close()
	if err := WriteDeterministic(f, size); err != nil {
		t.Fatalf("write image: %v", err)
	}
	return rel
}

// CreateCase 登记并返回案件 ID。
func (e *Env) CreateCase(t *testing.T, number string) uint {
	t.Helper()
	c, err := e.Svc.CreateCase(service.CreateCaseInput{
		CaseNumber: number, Title: "t-" + number,
		Custodian: "custodian-A", Actor: "investigator",
	})
	if err != nil {
		t.Fatalf("create case: %v", err)
	}
	return c.ID
}

// Register 登记证据并返回结果。
func (e *Env) Register(t *testing.T, caseID uint, rel string) uint {
	t.Helper()
	ev, err := e.Svc.RegisterEvidence(service.RegisterEvidenceInput{
		CaseID: caseID, RootName: e.RootName, RelPath: rel,
		Name: rel, Actor: "investigator",
	})
	if err != nil {
		t.Fatalf("register evidence: %v", err)
	}
	return ev.ID
}

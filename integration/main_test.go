// Package integration 包含 ProofCycle 端到端集成测试。
//
// 默认连接本地 127.0.0.1:13306 的 MySQL（可用 docker run -p 13306:3306 mysql:8.4 启动），
// 可用环境变量 PROOFCYCLE_TEST_DSN 覆盖（指向服务器、不带库名的 root DSN）。
// 连不上数据库时自动 skip，便于在无 MySQL 的环境跑纯存储单元测试。
package integration

import (
	"bytes"
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"

	"proofcycle/internal/database"
	"proofcycle/internal/domain"
	"proofcycle/internal/seed"
	"proofcycle/internal/service"
	"proofcycle/internal/storage"
)

func rootDSN() string {
	if v := os.Getenv("PROOFCYCLE_TEST_DSN"); v != "" {
		return v
	}
	return "root:rootpw@tcp(127.0.0.1:13390)/?charset=utf8mb4&parseTime=true"
}

// testEnv 是每个测试独立的一套数据库 + 存储目录。
type testEnv struct {
	t      *testing.T
	dbName string
	db     *gorm.DB
	store  *storage.LocalStore
	svc    *service.Services
	dir    string
}

func newTestEnv(t *testing.T) *testEnv {
	t.Helper()
	admin, err := gorm.Open(mysql.Open(rootDSN()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Skipf("mysql unavailable, skip integration test: %v", err)
	}
	sqlDB, err := admin.DB()
	if err != nil {
		t.Skipf("mysql unavailable: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := sqlDB.PingContext(ctx); err != nil {
		t.Skipf("mysql ping failed: %v", err)
	}

	b := make([]byte, 6)
	_, _ = rand.Read(b)
	name := "proofcycle_it_" + hex.EncodeToString(b)
	if _, err := sqlDB.Exec("CREATE DATABASE " + name + " CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"); err != nil {
		t.Fatalf("create database: %v", err)
	}
	t.Cleanup(func() {
		_, _ = sqlDB.Exec("DROP DATABASE " + name)
		_ = sqlDB.Close()
	})

	dsn := strings.Replace(rootDSN(), ")/?", ")/"+name+"?", 1)
	// 上面的替换对自定义 DSN 不一定成立，统一用字符串切割重建：
	dsn = rebuildDSN(rootDSN(), name)
	db, err := gorm.Open(mysql.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatalf("open test db: %v", err)
	}
	if err := database.Migrate(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	if err := seed.Ensure(db); err != nil {
		t.Fatalf("seed: %v", err)
	}

	dir := filepath.Join(t.TempDir(), "files")
	store, err := storage.NewLocalStore(dir)
	if err != nil {
		t.Fatalf("store: %v", err)
	}
	svc := service.New(db, store, 1<<20) // 1 MiB 测试上限
	return &testEnv{
		t: t, dbName: name, db: db,
		store: store, svc: svc, dir: dir,
	}
}

// rebuildDSN 把一个不带库名的 DSN 改为指向 dbName。
func rebuildDSN(dsn, dbName string) string {
	at := strings.Index(dsn, "@tcp(")
	head := dsn[:at]
	rest := dsn[at+len("@tcp("):]
	closeParen := strings.Index(rest, ")")
	host := rest[:closeParen]
	tail := rest[closeParen+1:]
	tail = strings.TrimPrefix(tail, "/")
	return fmt.Sprintf("%s@tcp(%s)/%s?charset=utf8mb4&parseTime=true", head, host, dbName)
}

func (e *testEnv) rawSQL() *sql.DB {
	d, err := e.db.DB()
	if err != nil {
		e.t.Fatalf("raw sql: %v", err)
	}
	return d
}

// ---- 夹具 ----

func validPDF() []byte {
	return []byte(`%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj
trailer<</Root 1 0 R>>
%%EOF`)
}

func validPNG() []byte {
	img := image.NewRGBA(image.Rect(0, 0, 16, 16))
	img.Set(0, 0, color.RGBA{R: 255, A: 255})
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		panic(err)
	}
	return buf.Bytes()
}

// ---- 高层流程辅助 ----

func (e *testEnv) mustCreateJob(reviewerIDs []string) *domain.Job {
	t := e.t
	job, _, err := e.svc.Job.Create(service.CreateJobInput{
		Name:        "测试包装作业",
		Description: "集成测试",
		DesignerID:  seed.UserDesigner,
		PMID:        seed.UserPM,
		ReviewerIDs: reviewerIDs,
		ChecklistID: seed.DefaultChecklist,
	})
	if err != nil {
		t.Fatalf("create job: %v", err)
	}
	return job
}

// snapshotItemIDs 取当前版本快照项 ID（按顺序）。
func (e *testEnv) snapshotItemIDs(versionID string) []string {
	t := e.t
	var v domain.FileVersion
	if err := e.db.First(&v, "id = ?", versionID).Error; err != nil {
		t.Fatalf("load version: %v", err)
	}
	var items []domain.ChecklistSnapshotItem
	if err := e.db.Where("snapshot_id = ?", v.SnapshotID).Order("order_no").Find(&items).Error; err != nil {
		t.Fatalf("load snapshot items: %v", err)
	}
	ids := make([]string, len(items))
	for i, it := range items {
		ids[i] = it.ID
	}
	return ids
}

// allPassInputs 构造一份“全部通过”的意见输入。
func allPassInputs(ids []string) []service.ItemInput {
	in := make([]service.ItemInput, len(ids))
	for i, id := range ids {
		in[i] = service.ItemInput{SnapshotItemID: id, Result: domain.ResultPass}
	}
	return in
}

// Package testsupport 为集成测试提供一次性 MySQL 数据库：
// 每个测试库独立、用后删除。需要真实 MySQL：
//
//	AG_RUN_INTEGRATION=1
//	AG_TEST_ADMIN_DSN="root:rootpw_change_me@tcp(127.0.0.1:3306)/?parseTime=true&loc=UTC"
//
// 未设置 AG_RUN_INTEGRATION=1 时集成测试自动跳过。
package testsupport

import (
	"database/sql"
	"encoding/json"
	"os"
	"testing"
	"time"

	"activityguard/internal/config"
	"activityguard/internal/db"
	"activityguard/internal/detection"
	"activityguard/internal/models"
	"activityguard/internal/seed"
	"activityguard/internal/util"

	"gorm.io/datatypes"
	"gorm.io/driver/mysql"
	"gorm.io/gorm"
)

type Env struct {
	GDB  *gorm.DB
	Cfg  config.Config
	Name string
}

// AdminDSN 返回不指定库名的管理 DSN，用于建库/删库。
func AdminDSN() string {
	if v := os.Getenv("AG_TEST_ADMIN_DSN"); v != "" {
		return v
	}
	return "root:rootpw_change_me@tcp(127.0.0.1:3306)/?charset=utf8mb4&parseTime=true&loc=UTC"
}

// SkipUnlessEnabled 在未显式开启集成测试时跳过。
func SkipUnlessEnabled(t *testing.T) {
	t.Helper()
	if os.Getenv("AG_RUN_INTEGRATION") != "1" {
		t.Skip("skip mysql integration test; set AG_RUN_INTEGRATION=1 to run")
	}
}

// New 创建独立测试库并完成迁移与种子数据。
func New(t *testing.T) *Env {
	t.Helper()
	SkipUnlessEnabled(t)

	name := "agtest_" + util.NewID()
	admin, err := sql.Open("mysql", AdminDSN())
	if err != nil {
		t.Fatalf("open admin dsn: %v", err)
	}
	if err := admin.Ping(); err != nil {
		admin.Close()
		t.Fatalf("ping mysql: %v (is docker compose mysql up?)", err)
	}
	if _, err := admin.Exec("CREATE DATABASE `" + name + "` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"); err != nil {
		admin.Close()
		t.Fatalf("create database %s: %v", name, err)
	}
	admin.Close()
	t.Cleanup(func() {
		a, err := sql.Open("mysql", AdminDSN())
		if err != nil {
			return
		}
		defer a.Close()
		_, _ = a.Exec("DROP DATABASE IF EXISTS `" + name + "`")
	})

	cfg := config.Load()
	cfg.DBName = name
	// 测试连接复用管理凭据（测试库由本进程创建/删除）。
	cfg.DBUser = "root"
	cfg.DBPass = "rootpw_change_me"
	if u, p, ok := parseDSNCredentials(AdminDSN()); ok {
		cfg.DBUser, cfg.DBPass = u, p
	}
	cfg.DBHost = "127.0.0.1"
	cfg.DBPort = "3306"
	if h, p, ok := parseDSNAddr(AdminDSN()); ok {
		cfg.DBHost, cfg.DBPort = h, p
	}
	// 测试里需要构造跨越 DST 边界的历史事件，补算窗口放宽到 400 天。
	cfg.BackfillWindow = 400 * 24 * time.Hour
	cfg.SchedulerEnabled = false

	gdb, err := gorm.Open(mysql.Open(cfg.DSN()), &gorm.Config{})
	if err != nil {
		t.Fatalf("open test db: %v", err)
	}
	if err := db.Migrate(gdb); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	if err := seed.Run(gdb); err != nil {
		t.Fatalf("seed: %v", err)
	}
	t.Cleanup(func() {
		if sqlDB, err := gdb.DB(); err == nil {
			_ = sqlDB.Close()
		}
	})

	return &Env{GDB: gdb, Cfg: cfg, Name: name}
}

func (e *Env) Engine() *detection.Engine {
	return detection.New(e.GDB, e.Cfg)
}

// EmployeeByEmail 按邮箱查种子员工。
func (e *Env) EmployeeByEmail(t *testing.T, email string) models.Employee {
	t.Helper()
	var emp models.Employee
	if err := e.GDB.Where("email = ?", email).First(&emp).Error; err != nil {
		t.Fatalf("employee %s: %v", email, err)
	}
	return emp
}

func (e *Env) UserByUsername(t *testing.T, username string) models.User {
	t.Helper()
	var u models.User
	if err := e.GDB.Where("username = ?", username).First(&u).Error; err != nil {
		t.Fatalf("user %s: %v", username, err)
	}
	return u
}

// CountAlerts 返回某规则当前的告警数量。
func (e *Env) CountAlerts(t *testing.T, ruleKey string) int64 {
	t.Helper()
	var n int64
	q := e.GDB.Model(&models.Alert{})
	if ruleKey != "" {
		q = q.Where("rule_key = ?", ruleKey)
	}
	if err := q.Count(&n).Error; err != nil {
		t.Fatalf("count alerts: %v", err)
	}
	return n
}

// AlertsFor 返回某员工某规则的全部告警。
func (e *Env) AlertsFor(t *testing.T, empID, ruleKey string) []models.Alert {
	t.Helper()
	var out []models.Alert
	if err := e.GDB.Where("employee_id = ? AND rule_key = ?", empID, ruleKey).Find(&out).Error; err != nil {
		t.Fatalf("load alerts: %v", err)
	}
	return out
}

// InsertPendingEvent 直接落库一条“尚未检测”的事件（模拟进程在检测前崩溃）。
func (e *Env) InsertPendingEvent(t *testing.T, ev models.Event) {
	t.Helper()
	if ev.ID == "" {
		ev.ID = util.NewID()
	}
	if len(ev.Metadata) == 0 {
		ev.Metadata = []byte("{}")
	}
	if err := e.GDB.Create(&ev).Error; err != nil {
		t.Fatalf("insert event: %v", err)
	}
}

// NewEvent 构造一条事件（自动算 content_hash 与 received_at），DetectedAt 为 nil。
func NewEvent(eventID, eventType, empID string, occurredAt time.Time, meta map[string]any) models.Event {
	var raw []byte
	if meta != nil {
		raw, _ = json.Marshal(meta)
	} else {
		raw = []byte("{}")
	}
	return models.Event{
		ID:          util.NewID(),
		EventID:     eventID,
		EventType:   eventType,
		EmployeeID:  empID,
		OccurredAt:  occurredAt.UTC(),
		ReceivedAt:  time.Now().UTC(),
		Metadata:    datatypes.JSON(raw),
		ContentHash: util.ContentHash(eventType, empID, occurredAt.UTC().UnixNano(), raw),
	}
}

// parseDSNCredentials 从 go-sql-driver DSN 中提取 user:password。
func parseDSNCredentials(dsn string) (string, string, bool) {
	at := -1
	for i := 0; i < len(dsn); i++ {
		if dsn[i] == '@' {
			at = i
			break
		}
	}
	if at <= 0 {
		return "", "", false
	}
	cred := dsn[:at]
	for i := 0; i < len(cred); i++ {
		if cred[i] == ':' {
			return cred[:i], cred[i+1:], true
		}
	}
	return cred, "", true
}

// parseDSNAddr 从 DSN 中提取 tcp(host:port) 地址。
func parseDSNAddr(dsn string) (string, string, bool) {
	marker := "tcp("
	i := indexOfStr(dsn, marker)
	if i < 0 {
		return "", "", false
	}
	rest := dsn[i+len(marker):]
	j := indexOfStr(rest, ")")
	if j < 0 {
		return "", "", false
	}
	addr := rest[:j]
	for k := 0; k < len(addr); k++ {
		if addr[k] == ':' {
			return addr[:k], addr[k+1:], true
		}
	}
	return addr, "3306", true
}

func indexOfStr(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

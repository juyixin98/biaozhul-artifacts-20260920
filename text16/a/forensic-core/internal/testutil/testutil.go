// Package testutil 提供测试共享工具（内存 SQLite）。
package testutil

import (
	"fmt"
	"strings"
	"testing"

	"github.com/glebarez/sqlite"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"

	"forensiccore/internal/models"
)

// NewDB 为测试创建独立的内存 SQLite 数据库（表结构已迁移）。
// 生产环境使用 MySQL；SQLite 仅用于单元测试，链追加逻辑同时依赖
// 行锁（MySQL）与唯一约束（两者）防分叉。
func NewDB(t *testing.T) *gorm.DB {
	t.Helper()
	name := strings.Map(func(r rune) rune {
		if r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' {
			return r
		}
		return '_'
	}, t.Name())
	dsn := fmt.Sprintf("file:%s?mode=memory&cache=shared", name)
	db, err := gorm.Open(sqlite.Open(dsn), &gorm.Config{
		Logger: gormlogger.Default.LogMode(gormlogger.Silent),
	})
	if err != nil {
		t.Fatalf("open test db: %v", err)
	}
	sqlDB, err := db.DB()
	if err != nil {
		t.Fatalf("test db handle: %v", err)
	}
	sqlDB.SetMaxOpenConns(1) // 串行化连接，避免内存库锁冲突
	if err := models.AutoMigrate(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	t.Cleanup(func() { sqlDB.Close() })
	return db
}

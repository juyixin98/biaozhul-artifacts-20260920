package store

import (
	"fmt"

	"github.com/glebarez/sqlite"
	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"

	"github.com/example/forensiccore/internal/model"
)

// Open 建立数据库连接并执行表结构迁移。
// driver 支持 mysql 与 sqlite（sqlite 为纯 Go 实现，仅供本地/测试使用）。
func Open(driver, dsn string) (*gorm.DB, error) {
	var dialector gorm.Dialector
	switch driver {
	case "mysql":
		dialector = mysql.Open(dsn)
	case "sqlite":
		dialector = sqlite.Open(dsn)
	default:
		return nil, fmt.Errorf("unsupported db driver %q", driver)
	}

	db, err := gorm.Open(dialector, &gorm.Config{
		Logger: logger.Default.LogMode(logger.Warn),
	})
	if err != nil {
		return nil, fmt.Errorf("open db: %w", err)
	}

	if driver == "sqlite" {
		// SQLite 单连接：避免并发写触发 "database is locked"，同时保证
		// SELECT ... 行为在测试中串行化。
		sqlDB, _ := db.DB()
		sqlDB.SetMaxOpenConns(1)
		if err := db.Exec("PRAGMA busy_timeout = 5000").Error; err != nil {
			return nil, err
		}
		if err := db.Exec("PRAGMA journal_mode=WAL").Error; err != nil {
			return nil, err
		}
	} else {
		sqlDB, err := db.DB()
		if err != nil {
			return nil, err
		}
		sqlDB.SetMaxIdleConns(5)
		sqlDB.SetMaxOpenConns(20)
	}

	if err := db.AutoMigrate(
		&model.Case{},
		&model.Evidence{},
		&model.VerifyJob{},
		&model.VerifyChunk{},
		&model.ChainEvent{},
	); err != nil {
		return nil, fmt.Errorf("migrate: %w", err)
	}
	return db, nil
}

// Package database 负责建立 GORM/MySQL 连接与执行迁移。
package database

import (
	"fmt"
	"time"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"

	"proofcycle/internal/domain"
)

// AllModels 列出需要 AutoMigrate 的全部实体。
func AllModels() []any {
	return []any{
		&domain.User{},
		&domain.Job{},
		&domain.JobReviewer{},
		&domain.Checklist{},
		&domain.ChecklistItem{},
		&domain.FileVersion{},
		&domain.ChecklistSnapshot{},
		&domain.ChecklistSnapshotItem{},
		&domain.Review{},
		&domain.ReviewItem{},
		&domain.Approval{},
	}
}

// Open 建立 MySQL 连接并做简单连通性重试（等待容器化的 MySQL 就绪）。
func Open(dsn string, maxWait time.Duration) (*gorm.DB, error) {
	var db *gorm.DB
	var err error
	deadline := time.Now().Add(maxWait)
	for {
		db, err = gorm.Open(mysql.Open(dsn), &gorm.Config{
			Logger: logger.Default.LogMode(logger.Warn),
		})
		if err == nil {
			var sqlDB, dErr = db.DB()
			if dErr == nil && sqlDB.Ping() == nil {
				sqlDB.SetMaxOpenConns(25)
				sqlDB.SetMaxIdleConns(10)
				sqlDB.SetConnMaxLifetime(time.Hour)
				return db, nil
			} else if dErr == nil {
				_ = sqlDB.Close()
				err = fmt.Errorf("mysql ping failed")
			}
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("connect mysql after %s: %w", maxWait, err)
		}
		time.Sleep(2 * time.Second)
	}
}

// Migrate 执行 GORM AutoMigrate。
func Migrate(db *gorm.DB) error {
	return db.AutoMigrate(AllModels()...)
}

// Package db 提供数据库连接与迁移。
package db

import (
	"time"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"

	"forensiccore/internal/models"
)

// OpenMySQL 连接 MySQL，带重试（等待容器就绪）。
func OpenMySQL(dsn string) (*gorm.DB, error) {
	var db *gorm.DB
	var err error
	for i := 0; i < 30; i++ {
		db, err = gorm.Open(mysql.Open(dsn), &gorm.Config{
			Logger: gormlogger.Default.LogMode(gormlogger.Warn),
		})
		if err == nil {
			sqlDB, perr := db.DB()
			if perr == nil {
				if perr = sqlDB.Ping(); perr == nil {
					return db, nil
				}
				err = perr
			}
		}
		time.Sleep(time.Second)
	}
	return nil, err
}

// Migrate 建表。
func Migrate(db *gorm.DB) error {
	return models.AutoMigrate(db)
}

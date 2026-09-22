package db

import (
	"fmt"
	"time"

	"activityguard/internal/config"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

// Open 连接 MySQL 并等待其就绪（compose healthcheck 之外再加一层应用侧重试）。
func Open(cfg config.Config) (*gorm.DB, error) {
	var gdb *gorm.DB
	var err error
	for attempt := 0; attempt < 30; attempt++ {
		gdb, err = gorm.Open(mysql.Open(cfg.DSN()), &gorm.Config{
			Logger:  logger.Default.LogMode(logger.Warn),
			NowFunc: func() time.Time { return time.Now().UTC() },
		})
		if err == nil {
			var sqlDB, e = gdb.DB()
			if e == nil {
				if e = sqlDB.Ping(); e == nil {
					sqlDB.SetMaxOpenConns(20)
					sqlDB.SetMaxIdleConns(10)
					sqlDB.SetConnMaxLifetime(time.Hour)
					return gdb, nil
				}
				err = e
			}
		}
		time.Sleep(2 * time.Second)
	}
	return nil, fmt.Errorf("connect mysql after retries: %w", err)
}

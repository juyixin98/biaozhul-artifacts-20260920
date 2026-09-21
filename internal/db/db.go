// Package db opens the MySQL connection and runs schema migrations.
package db

import (
	"fmt"
	"time"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"

	"targetcraft/internal/model"
)

// Open connects to MySQL with a bounded retry (the container may still be
// warming up when the API starts) and returns the *gorm.DB.
func Open(dsn string) (*gorm.DB, error) {
	var lastErr error
	for attempt := 0; attempt < 30; attempt++ {
		gdb, err := gorm.Open(mysql.Open(dsn), &gorm.Config{
			Logger: gormlogger.Default.LogMode(gormlogger.Warn),
		})
		if err == nil {
			sqlDB, err := gdb.DB()
			if err == nil {
				sqlDB.SetMaxOpenConns(50)
				sqlDB.SetMaxIdleConns(10)
				sqlDB.SetConnMaxLifetime(5 * time.Minute)
				if err = sqlDB.Ping(); err == nil {
					return gdb, nil
				}
			}
			lastErr = err
		} else {
			lastErr = err
		}
		time.Sleep(time.Second)
	}
	return nil, fmt.Errorf("connect mysql: %w", lastErr)
}

// Migrate creates or upgrades all tables. It is the single migration entry
// point used by the server, the tests and the docker entrypoint.
func Migrate(gdb *gorm.DB) error {
	return gdb.AutoMigrate(
		&model.Campaign{},
		&model.Creative{},
		&model.BudgetDay{},
		&model.UserHour{},
		&model.Decision{},
		&model.Settlement{},
	)
}

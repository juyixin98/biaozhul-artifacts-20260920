package store

import (
	"context"
	"fmt"
	"time"

	mysqlconn "github.com/go-sql-driver/mysql"
	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"
)

// EnsureDatabase connects without selecting a schema and creates the DSN's
// database if it does not exist. Used by the migrate command so a bare server
// is bootstrappable; serve relies on the database already existing (in Docker
// MYSQL_DATABASE creates it) and therefore does not require CREATE privilege.
func EnsureDatabase(dsn string) error {
	cfg, err := mysqlconn.ParseDSN(dsn)
	if err != nil {
		return fmt.Errorf("parse DSN: %w", err)
	}
	dbName := cfg.DBName
	if dbName == "" {
		return nil
	}
	root := *cfg
	root.DBName = ""
	bootstrap, err := gorm.Open(mysql.Open(root.FormatDSN()), &gorm.Config{
		Logger: gormlogger.Default.LogMode(gormlogger.Silent),
	})
	if err != nil {
		return fmt.Errorf("connect (no schema): %w", err)
	}
	sqlDB, err := bootstrap.DB()
	if err != nil {
		return err
	}
	defer sqlDB.Close()
	if err := bootstrap.Exec(
		fmt.Sprintf("CREATE DATABASE IF NOT EXISTS `%s` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci", dbName),
	).Error; err != nil {
		return fmt.Errorf("create database %s: %w", dbName, err)
	}
	return nil
}

// Open connects to MySQL with retry (compose starts app while DB warms up).
func Open(ctx context.Context, dsn string) (*gorm.DB, error) {
	cfg := &gorm.Config{
		Logger: gormlogger.Default.LogMode(gormlogger.Warn),
	}
	var lastErr error
	deadline := time.Now().Add(60 * time.Second)
	for time.Now().Before(deadline) {
		db, err := gorm.Open(mysql.Open(dsn), cfg)
		if err == nil {
			sqlDB, err := db.DB()
			if err == nil {
				pingCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
				err = sqlDB.PingContext(pingCtx)
				cancel()
			}
			if err == nil {
				sqlDB.SetMaxOpenConns(20)
				sqlDB.SetMaxIdleConns(5)
				sqlDB.SetConnMaxLifetime(time.Hour)
				return db, nil
			}
			_ = sqlDB.Close()
			lastErr = err
		} else {
			lastErr = err
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
	return nil, fmt.Errorf("connect mysql: %w", lastErr)
}

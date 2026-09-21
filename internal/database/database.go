package database

import (
	"fmt"

	"forensiccore/internal/models"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

// Open connects to the database. driver is "mysql" (production) or any driver
// already registered by the caller (tests register the pure-Go sqlite driver).
func Open(driver, dsn string) (*gorm.DB, error) {
	var dialector gorm.Dialector
	switch driver {
	case "mysql":
		dialector = mysql.Open(dsn)
	default:
		return nil, fmt.Errorf("unsupported database driver %q", driver)
	}
	db, err := gorm.Open(dialector, &gorm.Config{
		Logger: logger.Default.LogMode(logger.Warn),
	})
	if err != nil {
		return nil, fmt.Errorf("open database: %w", err)
	}
	return db, nil
}

// Migrate creates / updates the schema.
func Migrate(db *gorm.DB) error {
	return db.AutoMigrate(
		&models.Case{},
		&models.VerificationJob{},
		&models.JobChunk{},
		&models.ChainEvent{},
		&models.ChainLock{},
	)
}

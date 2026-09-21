package store

import (
	"fmt"
	"time"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"

	"targetcraft/internal/models"
)

// Open connects to MySQL with sane pool defaults.
func Open(dsn string) (*gorm.DB, error) {
	db, err := gorm.Open(mysql.Open(dsn), &gorm.Config{
		Logger: gormlogger.Default.LogMode(gormlogger.Warn),
		// Keep all auto timestamps in UTC so day/hour bucketing is consistent.
		NowFunc: func() time.Time { return time.Now().UTC() },
	})
	if err != nil {
		return nil, fmt.Errorf("open database: %w", err)
	}
	sqlDB, err := db.DB()
	if err != nil {
		return nil, err
	}
	sqlDB.SetMaxOpenConns(50)
	sqlDB.SetMaxIdleConns(10)
	sqlDB.SetConnMaxLifetime(5 * time.Minute)
	return db, nil
}

// Migrate applies the schema. GORM AutoMigrate is additive and idempotent,
// safe to run on every boot.
func Migrate(db *gorm.DB) error {
	return db.AutoMigrate(
		&models.Campaign{},
		&models.Creative{},
		&models.Decision{},
		&models.DailyBudget{},
		&models.FreqCounter{},
	)
}

// Seed inserts sample data if the campaigns table is empty. Idempotent.
func Seed(db *gorm.DB, now time.Time) error {
	var count int64
	if err := db.Model(&models.Campaign{}).Count(&count).Error; err != nil {
		return err
	}
	if count > 0 {
		return nil
	}

	campaign := models.Campaign{
		Name:        "sample-launch-campaign",
		Status:      models.CampaignStatusActive,
		StartAt:     now.Add(-24 * time.Hour),
		EndAt:       now.Add(30 * 24 * time.Hour),
		TotalBudget: 1_000_000, // 10,000.00 in cents
		DailyCap:    100_000,   // 1,000.00 per UTC day
		Rules: models.Rules{
			Regions: []string{"CN", "US", "JP"},
			Devices: []string{"ios", "android"},
			Hours:   nil, // all UTC hours
		}.Marshal(),
	}
	if err := db.Create(&campaign).Error; err != nil {
		return err
	}

	creatives := []models.Creative{
		{CampaignID: campaign.ID, Name: "banner-a", Content: "Sample banner A", Status: models.CreativeStatusActive},
		{CampaignID: campaign.ID, Name: "banner-b", Content: "Sample banner B", Status: models.CreativeStatusActive},
	}
	return db.Create(&creatives).Error
}

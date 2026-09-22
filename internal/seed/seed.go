// Package seed initializes reference data: the four detection rules,
// departments, employees and API keys. Everything is idempotent.
package seed

import (
	"time"

	"anomalywatch/internal/config"
	"anomalywatch/internal/httpapi"
	"anomalywatch/internal/models"

	"gorm.io/gorm"
)

// Run inserts default reference data if the database is unseeded.
func Run(db *gorm.DB, cfg config.Config) error {
	now := time.Now().UTC()

	if err := seedRules(db, cfg, now); err != nil {
		return err
	}
	if err := seedDepartmentsAndStaff(db, now); err != nil {
		return err
	}
	if err := seedAPIKeys(db, cfg, now); err != nil {
		return err
	}
	return nil
}

func seedRules(db *gorm.DB, cfg config.Config, now time.Time) error {
	defaults := []models.Rule{
		{
			Code:        models.RuleDownloadBurst,
			Name:        "Download burst",
			Description: "More than 50 file downloads in any 10-minute window.",
			Enabled:     true,
			Params: models.JSONMap{
				"window_minutes": cfg.BurstWindow.Minutes(),
				"threshold":      cfg.BurstThreshold,
			},
			Version: 1, Active: true,
		},
		{
			Code:        models.RuleFirstUSB,
			Name:        "First USB use",
			Description: "First time an employee connects a USB storage device.",
			Enabled:     true,
			Params:      models.JSONMap{},
			Version:     1, Active: true,
		},
		{
			Code:        models.RuleNightActivity,
			Name:        "Night-time activity",
			Description: "Any activity between 20:00 and 06:00 in the employee's local time zone.",
			Enabled:     true,
			Params: models.JSONMap{
				"start_hour": cfg.NightStartHour,
				"end_hour":   cfg.NightEndHour,
			},
			Version: 1, Active: true,
		},
		{
			Code:        models.RuleStatistical,
			Name:        "Statistical volume anomaly",
			Description: "Daily event count more than 2.5 standard deviations above the prior 30 days (min 10 samples).",
			Enabled:     true,
			Params: models.JSONMap{
				"history_days": cfg.StatHistoryDays,
				"z_score":      cfg.StatZScore,
				"min_samples":  cfg.StatMinSamples,
			},
			Version: 1, Active: true,
		},
	}
	for _, r := range defaults {
		var n int64
		if err := db.Model(&models.Rule{}).Where("code = ? AND active = ?", r.Code, true).Count(&n).Error; err != nil {
			return err
		}
		if n > 0 {
			continue
		}
		r.CreatedAt = now
		r.UpdatedAt = now
		if err := db.Create(&r).Error; err != nil {
			return err
		}
	}
	return nil
}

func seedDepartmentsAndStaff(db *gorm.DB, now time.Time) error {
	depts := []models.Department{
		{Name: "Engineering", CreatedAt: now},
		{Name: "Finance", CreatedAt: now},
	}
	deptIDs := map[string]uint64{}
	for _, d := range depts {
		var existing models.Department
		err := db.Where("name = ?", d.Name).First(&existing).Error
		if err == gorm.ErrRecordNotFound {
			if err := db.Create(&d).Error; err != nil {
				return err
			}
			deptIDs[d.Name] = d.ID
			continue
		}
		if err != nil {
			return err
		}
		deptIDs[d.Name] = existing.ID
	}

	employees := []models.Employee{
		{Name: "Alice Chen", DeptID: deptIDs["Engineering"], TimeZone: "Asia/Shanghai"},
		{Name: "Bob Li", DeptID: deptIDs["Engineering"], TimeZone: "America/New_York"},
		{Name: "Carol Wang", DeptID: deptIDs["Finance"], TimeZone: "Europe/London"},
		{Name: "David Zhao", DeptID: deptIDs["Finance"], TimeZone: "UTC"},
	}
	for _, e := range employees {
		var n int64
		if err := db.Model(&models.Employee{}).Where("name = ? AND dept_id = ?", e.Name, e.DeptID).Count(&n).Error; err != nil {
			return err
		}
		if n == 0 {
			e.CreatedAt = now
			if err := db.Create(&e).Error; err != nil {
				return err
			}
		}
	}
	return nil
}

func seedAPIKeys(db *gorm.DB, cfg config.Config, now time.Time) error {
	// Admin: all departments.
	if err := createKeyIfAbsent(db, "admin", cfg.AdminKey, models.RoleAdmin, nil, now); err != nil {
		return err
	}
	// Analyst bound to the Engineering department.
	var eng models.Department
	if err := db.Where("name = ?", "Engineering").First(&eng).Error; err != nil {
		return err
	}
	if err := createKeyIfAbsent(db, "analyst-engineering", cfg.AnalystKey, models.RoleAnalyst, &eng.ID, now); err != nil {
		return err
	}
	return nil
}

func createKeyIfAbsent(db *gorm.DB, name, rawKey, role string, deptID *uint64, now time.Time) error {
	var n int64
	if err := db.Model(&models.APIKey{}).Where("name = ?", name).Count(&n).Error; err != nil {
		return err
	}
	if n > 0 {
		return nil
	}
	return db.Create(&models.APIKey{
		Name:      name,
		KeyHash:   httpapi.HashKeyExported(rawKey),
		Role:      role,
		DeptID:    deptID,
		Active:    true,
		CreatedAt: now,
	}).Error
}

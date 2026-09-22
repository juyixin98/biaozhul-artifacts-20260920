package store

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"time"

	"geoterritory/internal/models"

	"gorm.io/gorm"
)

// Seed inserts two demo organizations with fixed API keys (so the docs work
// copy-paste) plus a handful of geometrically recognisable sample points for
// the first organization. It is idempotent: reruns create nothing.
//
// No regions are seeded: publishing a region is an immutable-version +
// recompute operation and the docs walk through it explicitly, which also
// demonstrates the recompute pipeline end to end.
func Seed(db *gorm.DB) error {
	for _, o := range []models.Organization{
		{Name: "Acme Logistics", APIKey: "demo-key-acme"},
		{Name: "Beta Fleet", APIKey: "demo-key-beta"},
	} {
		var existing models.Organization
		err := db.Where("api_key = ?", o.APIKey).First(&existing).Error
		if err == nil {
			continue
		}
		if !errors.Is(err, gorm.ErrRecordNotFound) {
			return err
		}
		if err := db.Create(&o).Error; err != nil {
			return err
		}
	}

	var acme models.Organization
	if err := db.Where("api_key = ?", "demo-key-acme").First(&acme).Error; err != nil {
		return err
	}

	now := time.Now().UTC()
	samples := []models.Point{
		{OrgID: acme.ID, ExternalID: "S001", Lat: 39.9042, Lng: 116.4074}, // Beijing
		{OrgID: acme.ID, ExternalID: "S002", Lat: 31.2304, Lng: 121.4737}, // Shanghai
		{OrgID: acme.ID, ExternalID: "S003", Lat: 22.3193, Lng: 114.1694}, // Hong Kong
		{OrgID: acme.ID, ExternalID: "S004", Lat: 1.3521, Lng: 103.8198},  // Singapore
		{OrgID: acme.ID, ExternalID: "S005", Lat: 35.6762, Lng: 139.6503}, // Tokyo
	}
	for _, p := range samples {
		var cnt int64
		db.Model(&models.Point{}).Where("org_id = ? AND external_id = ?", acme.ID, p.ExternalID).Count(&cnt)
		if cnt > 0 {
			continue
		}
		if err := db.Exec(`INSERT INTO points (org_id, external_id, lat, lng, version, created_at, updated_at)
			VALUES (?, ?, ?, ?, 1, ?, ?)`, p.OrgID, p.ExternalID, p.Lat, p.Lng, now, now).Error; err != nil {
			return err
		}
	}

	b := make([]byte, 8)
	_, _ = rand.Read(b)
	log.Printf("seed complete; demo API keys: demo-key-acme, demo-key-beta (nonce %s)", hex.EncodeToString(b))
	return nil
}

// EnsureAtLeastOneOrg is used when seeding is disabled: fail fast with an
// actionable message rather than serving an empty tenant set.
func EnsureAtLeastOneOrg(db *gorm.DB) error {
	var cnt int64
	if err := db.Model(&models.Organization{}).Count(&cnt).Error; err != nil {
		return err
	}
	if cnt == 0 {
		return fmt.Errorf("no organizations exist; enable SEED_SAMPLES or insert an api key manually")
	}
	return nil
}

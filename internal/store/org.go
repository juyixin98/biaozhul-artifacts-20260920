package store

import (
	"errors"
	"strings"

	"gorm.io/gorm"

	"geoterritory/internal/models"
)

// ErrNotFound is a store-level not-found sentinel.
var ErrNotFound = errors.New("not found")

// AuthError describes an invalid API key.
var ErrAuth = errors.New("invalid api key")

// OrganizationByAPIKey resolves an API key to its organization.
func OrganizationByAPIKey(gdb *gorm.DB, key string) (*models.Organization, error) {
	var org models.Organization
	err := gdb.Where("api_key = ?", key).First(&org).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrAuth
	}
	if err != nil {
		return nil, err
	}
	return &org, nil
}

// SeedOrganization ensures an organization with the given name/api key and
// its catalog state row (version 0, empty catalog) exist. Idempotent.
func SeedOrganization(gdb *gorm.DB, name, apiKey string) error {
	var org models.Organization
	err := gdb.Where("api_key = ?", apiKey).First(&org).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		org = models.Organization{Name: name, APIKey: apiKey}
		if err := gdb.Create(&org).Error; err != nil {
			return err
		}
	} else if err != nil {
		return err
	}
	return gdb.Exec(`INSERT INTO region_catalog_state (org_id, current_version, published_version, updated_at)
		VALUES (?, 0, 0, UTC_TIMESTAMP(6))
		ON DUPLICATE KEY UPDATE org_id = org_id`, org.ID).Error
}

// SeedAPIKeys parses "name=key,name=key" pairs and seeds each organization.
func SeedAPIKeys(gdb *gorm.DB, spec string) error {
	for _, pair := range strings.Split(spec, ",") {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		kv := strings.SplitN(pair, "=", 2)
		if len(kv) != 2 {
			continue
		}
		if err := SeedOrganization(gdb, strings.TrimSpace(kv[0]), strings.TrimSpace(kv[1])); err != nil {
			return err
		}
	}
	return nil
}

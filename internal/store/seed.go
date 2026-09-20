package store

import (
	"context"
	"errors"

	"gorm.io/gorm"

	"sitevitals/internal/models"
)

// Seed installs global default budgets if none exist.
func (s *Store) Seed(ctx context.Context) error {
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var count int64
		if err := tx.Model(&models.Budget{}).Where("site_id = 0").Count(&count).Error; err != nil {
			return err
		}
		if count == 0 {
			b := BudgetDefaults
			if err := tx.Create(&b).Error; err != nil {
				return err
			}
		}
		return nil
	})
}

// EnsureSite creates a whitelist entry if an identical (origin, prefix) does
// not exist yet. Used by demo seeding and tests.
func (s *Store) EnsureSite(ctx context.Context, name, origin, prefix string, enabled bool) (*models.Site, error) {
	var site models.Site
	err := s.db.WithContext(ctx).Where("origin = ? AND path_prefix = ?", origin, prefix).First(&site).Error
	if err == nil {
		return &site, nil
	}
	if !errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, err
	}
	site = models.Site{Name: name, Origin: origin, PathPrefix: prefix, Enabled: enabled}
	if err := s.db.WithContext(ctx).Create(&site).Error; err != nil {
		return nil, err
	}
	return &site, nil
}

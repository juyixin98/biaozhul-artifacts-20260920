package store

import (
	"errors"

	"gorm.io/gorm"

	"geoterritory/internal/models"
)

// RegionWithVersion is a region plus its latest immutable snapshot.
type RegionWithVersion struct {
	models.Region
	Vertices models.Vertices `json:"vertices"`
}

// GetRegion fetches one region of an org by ID.
func GetRegion(gdb *gorm.DB, orgID, id uint64) (*models.Region, error) {
	var r models.Region
	err := gdb.Where("id = ? AND org_id = ?", id, orgID).First(&r).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrNotFound
	}
	return &r, err
}

// ListRegions lists regions for an org (newest first), optionally only active.
func ListRegions(gdb *gorm.DB, orgID uint64, activeOnly bool) ([]models.Region, error) {
	q := gdb.Where("org_id = ?", orgID)
	if activeOnly {
		q = q.Where("active = TRUE")
	}
	var out []models.Region
	err := q.Order("priority ASC, id ASC").Find(&out).Error
	return out, err
}

// GetRegionVersion fetches one immutable region snapshot.
func GetRegionVersion(gdb *gorm.DB, orgID, regionID uint64, version int) (*models.RegionVersion, error) {
	var rv models.RegionVersion
	err := gdb.Where("region_id = ? AND version = ? AND org_id = ?", regionID, version, orgID).
		First(&rv).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrNotFound
	}
	return &rv, err
}

// LatestRegionVersion returns the newest snapshot for a region.
func LatestRegionVersion(gdb *gorm.DB, regionID uint64) (*models.RegionVersion, error) {
	var rv models.RegionVersion
	err := gdb.Where("region_id = ?", regionID).Order("version DESC").First(&rv).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrNotFound
	}
	return &rv, err
}

// DeactivateRegion marks a region inactive. The removal from future catalogs
// takes effect on the next publish (catalogs are immutable, so a separate
// publish is required).
func DeactivateRegion(tx *gorm.DB, orgID, id uint64) error {
	res := tx.Model(&models.Region{}).
		Where("id = ? AND org_id = ?", id, orgID).
		UpdateColumn("active", false)
	if res.Error != nil {
		return res.Error
	}
	if res.RowsAffected == 0 {
		return ErrNotFound
	}
	return nil
}

// CountActiveRegions reports how many active regions the org has (used to
// detect a "remove all regions" publish that yields an empty catalog).
func ActiveRegions(tx *gorm.DB, orgID uint64) ([]models.Region, error) {
	var out []models.Region
	err := tx.Where("org_id = ? AND active = TRUE", orgID).Find(&out).Error
	return out, err
}

package store

import (
	"errors"
	"time"

	"gorm.io/gorm"

	"geoterritory/internal/engine"
	"geoterritory/internal/models"
)

// ErrActiveJob is returned when a publish is attempted while a reassignment
// for that organization is still running.
var ErrActiveJob = errors.New("a reassignment job is already active")

// CatalogStateRow returns the catalog pointers for an org.
func CatalogStateRow(tx *gorm.DB, orgID uint64) (*models.CatalogState, error) {
	var st models.CatalogState
	if err := tx.Where("org_id = ?", orgID).First(&st).Error; err != nil {
		return nil, err
	}
	return &st, nil
}

// LoadCatalog builds the immutable in-memory view of one catalog version.
func LoadCatalog(tx *gorm.DB, orgID uint64, version int) (*engine.Catalog, error) {
	var rows []struct {
		RegionID        uint64
		RegionVersionID uint64
		Version         int
		Name            string
		Priority        int
		MinLat          float64
		MinLng          float64
		MaxLat          float64
		MaxLng          float64
		Vertices        models.Vertices
	}
	// Join immutable entries with the immutable region snapshots.
	err := tx.Table("catalog_entries AS ce").
		Select("ce.region_id AS region_id, ce.region_version_id AS region_version_id, ce.version AS version, "+
			"rv.name AS name, rv.priority AS priority, rv.min_lat AS min_lat, rv.min_lng AS min_lng, "+
			"rv.max_lat AS max_lat, rv.max_lng AS max_lng, rv.vertices AS vertices").
		Joins("JOIN region_versions AS rv ON rv.id = ce.region_version_id").
		Where("ce.org_id = ? AND ce.catalog_version = ?", orgID, version).
		Scan(&rows).Error
	if err != nil {
		return nil, err
	}
	entries := make([]engine.Entry, 0, len(rows))
	for _, r := range rows {
		entries = append(entries, engine.Entry{
			RegionID:        r.RegionID,
			RegionVersionID: r.RegionVersionID,
			Version:         r.Version,
			Name:            r.Name,
			Priority:        r.Priority,
			Vertices:        r.Vertices,
			MinLat:          r.MinLat,
			MinLng:          r.MinLng,
			MaxLat:          r.MaxLat,
			MaxLng:          r.MaxLng,
		})
	}
	return engine.NewCatalog(version, entries), nil
}

// CreateRegionInput carries a validated region definition.
type CreateRegionInput struct {
	Name     string
	Priority int
	Vertices models.Vertices
}

// PublishRegion creates a region (or publishes a new version of an existing
// one), builds the next immutable catalog and enqueues the durable
// reassignment job. Everything happens in one transaction while the
// organization lock is held.
//
// Returns (newCatalogVersion, jobID). The caller must hold the org lock (via
// WithOrgLock); this function operates on tx.
func PublishRegion(tx *gorm.DB, orgID uint64, in CreateRegionInput) (int, uint64, error) {
	// Reject if an active job exists (defense in depth; unique index would
	// also catch it at job insert).
	var active int64
	if err := tx.Table("reassignment_jobs").
		Where("org_id = ? AND status IN ?", orgID, []string{models.JobPending, models.JobRunning}).
		Count(&active).Error; err != nil {
		return 0, 0, err
	}
	if active > 0 {
		return 0, 0, ErrActiveJob
	}

	st, err := CatalogStateRow(tx, orgID)
	if err != nil {
		return 0, 0, err
	}

	var region models.Region
	err = tx.Where("org_id = ? AND name = ?", orgID, in.Name).First(&region).Error
	switch {
	case errors.Is(err, gorm.ErrRecordNotFound):
		region = models.Region{
			OrgID:    orgID,
			Name:     in.Name,
			Priority: in.Priority,
			Active:   true,
		}
		if err := tx.Create(&region).Error; err != nil {
			return 0, 0, err
		}
	case err != nil:
		return 0, 0, err
	default:
		region.Priority = in.Priority
		region.Active = true
		if err := tx.Save(&region).Error; err != nil {
			return 0, 0, err
		}
	}

	// Immutable snapshot.
	region.LatestVersion++
	rv := models.RegionVersion{
		RegionID: region.ID,
		Version:  region.LatestVersion,
		OrgID:    orgID,
		Name:     region.Name,
		Priority: in.Priority,
		Vertices: in.Vertices,
	}
	rv.MinLat, rv.MinLng, rv.MaxLat, rv.MaxLng = boundsOf(in.Vertices)
	rv.AreaSqDeg = absArea(in.Vertices)
	if err := tx.Create(&rv).Error; err != nil {
		return 0, 0, err
	}
	if err := tx.Model(&models.Region{}).Where("id = ?", region.ID).
		UpdateColumn("latest_version", region.LatestVersion).Error; err != nil {
		return 0, 0, err
	}

	// Build the next catalog version from current published (== current when
	// no job is active), replacing this region's snapshot with the new one.
	fromVersion := st.PublishedVersion
	toVersion := st.PublishedVersion + 1
	type entrySpec struct {
		regionID        uint64
		version         int
		regionVersionID uint64
	}
	var specs []entrySpec
	if fromVersion > 0 {
		var prev []models.CatalogEntry
		if err := tx.Where("org_id = ? AND catalog_version = ?", orgID, fromVersion).
			Find(&prev).Error; err != nil {
			return 0, 0, err
		}
		for _, e := range prev {
			if e.RegionID == region.ID {
				continue // replaced below
			}
			specs = append(specs, entrySpec{e.RegionID, e.Version, e.RegionVersionID})
		}
	}
	specs = append(specs, entrySpec{region.ID, region.LatestVersion, rv.ID})

	now := time.Now().UTC()
	entries := make([]*models.CatalogEntry, 0, len(specs))
	for _, s := range specs {
		entries = append(entries, &models.CatalogEntry{
			OrgID:           orgID,
			CatalogVersion:  toVersion,
			RegionVersionID: s.regionVersionID,
			RegionID:        s.regionID,
			Version:         s.version,
			CreatedAt:       now,
		})
	}
	if len(entries) > 0 {
		if err := tx.Create(&entries).Error; err != nil {
			return 0, 0, err
		}
	}

	var total int64
	if err := tx.Model(&models.Point{}).Where("org_id = ?", orgID).Count(&total).Error; err != nil {
		return 0, 0, err
	}

	job := models.ReassignJob{
		OrgID:       orgID,
		FromVersion: fromVersion,
		ToVersion:   toVersion,
		Status:      models.JobPending,
		TotalPoints: total,
	}
	if err := tx.Create(&job).Error; err != nil {
		return 0, 0, err
	}

	// Advance the published pointer; current pointer stays on the old,
	// consistent catalog until the worker flips it atomically.
	if err := tx.Model(&models.CatalogState{}).Where("org_id = ?", orgID).
		Updates(map[string]any{
			"published_version": toVersion,
			"updated_at":        now,
		}).Error; err != nil {
		return 0, 0, err
	}
	return toVersion, job.ID, nil
}

func boundsOf(v models.Vertices) (minLat, minLng, maxLat, maxLng float64) {
	minLat = v[0].Lat
	maxLat = v[0].Lat
	minLng = v[0].Lng
	maxLng = v[0].Lng
	for _, p := range v[1:] {
		if p.Lat < minLat {
			minLat = p.Lat
		}
		if p.Lat > maxLat {
			maxLat = p.Lat
		}
		if p.Lng < minLng {
			minLng = p.Lng
		}
		if p.Lng > maxLng {
			maxLng = p.Lng
		}
	}
	return
}

func absArea(v models.Vertices) float64 {
	var a float64
	n := len(v)
	for i := 0; i < n; i++ {
		x, y := v[i], v[(i+1)%n]
		a += x.Lng*y.Lat - y.Lng*x.Lat
	}
	if a < 0 {
		return -a / 2
	}
	return a / 2
}

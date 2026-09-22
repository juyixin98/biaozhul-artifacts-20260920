package service

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"

	"geoterritory/internal/geometry"
	"geoterritory/internal/models"

	"gorm.io/gorm"
)

// ValidationError carries an HTTP status and a client-safe message.
type ValidationError struct {
	Status  int
	Message string
}

func (e *ValidationError) Error() string { return e.Message }

func badRequest(format string, a ...any) error {
	return &ValidationError{Status: http.StatusBadRequest, Message: fmt.Sprintf(format, a...)}
}

// RegionService handles region definition and immutable version publishing.
type RegionService struct {
	db *gorm.DB
}

func NewRegionService(db *gorm.DB) *RegionService { return &RegionService{db: db} }

// CreateRegionInput is the request body for POST /regions and for new
// versions. Polygon is an open ring: the closing edge is implicit.
type CreateRegionInput struct {
	Name     string            `json:"name"`
	Priority int               `json:"priority"` // 1 (highest) .. 5 (lowest)
	Polygon  []geometry.Vertex `json:"polygon"`
}

// CreateRegion creates the region row, its first immutable version (in
// 'building' status) and enqueues the persistent recompute job, all in one
// transaction. The new region takes effect only after the job applies.
func (s *RegionService) CreateRegion(orgID int64, in CreateRegionInput) (*models.RegionVersion, *models.ReassignJob, error) {
	if in.Name == "" {
		return nil, nil, badRequest("name is required")
	}
	if err := validatePriority(in.Priority); err != nil {
		return nil, nil, err
	}
	if err := geometry.ValidatePolygon(in.Polygon); err != nil {
		return nil, nil, badRequest("invalid polygon: %v", err)
	}

	var rv models.RegionVersion
	var job models.ReassignJob
	err := s.db.Transaction(func(tx *gorm.DB) error {
		var org models.Organization
		if err := tx.Clauses(lockOrgClause()).First(&org, orgID).Error; err != nil {
			return err
		}

		region := models.Region{OrgID: orgID, Name: in.Name}
		if err := tx.Create(&region).Error; err != nil {
			if isDuplicateKey(err) {
				return badRequest("region name %q already exists in this organization", in.Name)
			}
			return err
		}

		targetSeq := nextTargetSeq(tx, org)
		rv = models.RegionVersion{
			RegionID: region.ID,
			OrgID:    orgID,
			Version:  1,
			Priority: in.Priority,
			Polygon:  polygonJSON(in.Polygon),
			SetSeq:   targetSeq,
			Status:   "building",
		}
		if err := tx.Create(&rv).Error; err != nil {
			return err
		}
		job = models.ReassignJob{
			OrgID:        orgID,
			TargetSeq:    targetSeq,
			NewVersionID: rv.ID,
			Status:       "pending",
		}
		if err := tx.Create(&job).Error; err != nil {
			return err
		}
		return nil
	})
	if err != nil {
		return nil, nil, err
	}
	return &rv, &job, nil
}

// PublishVersion creates a new immutable version of an existing region and
// enqueues its recompute job. Geometry and priority are snapshotted together;
// changing either is a publish.
func (s *RegionService) PublishVersion(orgID, regionID int64, in CreateRegionInput) (*models.RegionVersion, *models.ReassignJob, error) {
	if err := validatePriority(in.Priority); err != nil {
		return nil, nil, err
	}
	if err := geometry.ValidatePolygon(in.Polygon); err != nil {
		return nil, nil, badRequest("invalid polygon: %v", err)
	}

	var rv models.RegionVersion
	var job models.ReassignJob
	err := s.db.Transaction(func(tx *gorm.DB) error {
		var org models.Organization
		if err := tx.Clauses(lockOrgClause()).First(&org, orgID).Error; err != nil {
			return err
		}
		var region models.Region
		if err := tx.Where("id = ? AND org_id = ?", regionID, orgID).First(&region).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return &ValidationError{Status: http.StatusNotFound, Message: "region not found"}
			}
			return err
		}

		var last models.RegionVersion
		if err := tx.Where("region_id = ?", region.ID).Order("version DESC").First(&last).Error; err != nil {
			return err
		}
		// Refuse a no-op publish: identical geometry and priority.
		if last.Priority == in.Priority && last.Polygon == polygonJSON(in.Polygon) {
			return badRequest("new version is identical to version %d", last.Version)
		}

		targetSeq := nextTargetSeq(tx, org)
		rv = models.RegionVersion{
			RegionID: region.ID,
			OrgID:    orgID,
			Version:  last.Version + 1,
			Priority: in.Priority,
			Polygon:  polygonJSON(in.Polygon),
			SetSeq:   targetSeq,
			Status:   "building",
		}
		if err := tx.Create(&rv).Error; err != nil {
			return err
		}
		job = models.ReassignJob{
			OrgID:        orgID,
			TargetSeq:    targetSeq,
			NewVersionID: rv.ID,
			Status:       "pending",
		}
		if err := tx.Create(&job).Error; err != nil {
			return err
		}
		return nil
	})
	if err != nil {
		return nil, nil, err
	}
	return &rv, &job, nil
}

// GetRegion returns a region with its version list (newest first).
func (s *RegionService) GetRegion(orgID, regionID int64) (*models.Region, []models.RegionVersion, error) {
	var region models.Region
	if err := s.db.Where("id = ? AND org_id = ?", regionID, orgID).First(&region).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, nil, &ValidationError{Status: http.StatusNotFound, Message: "region not found"}
		}
		return nil, nil, err
	}
	var versions []models.RegionVersion
	if err := s.db.Where("region_id = ?", region.ID).Order("version DESC").Find(&versions).Error; err != nil {
		return nil, nil, err
	}
	return &region, versions, nil
}

// ListRegions returns regions scoped to the organization.
func (s *RegionService) ListRegions(orgID int64) ([]models.Region, error) {
	var regions []models.Region
	err := s.db.Where("org_id = ?", orgID).Order("id ASC").Find(&regions).Error
	return regions, err
}

// GetJob reports recompute job status (for polling POST responses).
func (s *RegionService) GetJob(orgID, jobID int64) (*models.ReassignJob, error) {
	var job models.ReassignJob
	if err := s.db.Where("id = ? AND org_id = ?", jobID, orgID).First(&job).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, &ValidationError{Status: http.StatusNotFound, Message: "job not found"}
		}
		return nil, err
	}
	return &job, nil
}

func validatePriority(p int) error {
	if p < 1 || p > 5 {
		return badRequest("priority must be between 1 (highest) and 5 (lowest), got %d", p)
	}
	return nil
}

func polygonJSON(verts []geometry.Vertex) string {
	b, _ := json.Marshal(verts)
	return string(b)
}

// nextTargetSeq allocates a monotonically increasing set sequence while the
// caller holds the organization row lock. It is one more than the largest of
// the currently applied seq and every already-enqueued job's target seq, so
// publishes can never reuse or reorder a seq.
func nextTargetSeq(tx *gorm.DB, org models.Organization) int64 {
	var maxJob int64
	tx.Table("reassign_jobs").Where("org_id = ?", org.ID).
		Select("COALESCE(MAX(target_seq), 0)").Scan(&maxJob)
	cand := org.ActiveSetSeq
	if maxJob > cand {
		cand = maxJob
	}
	return cand + 1
}

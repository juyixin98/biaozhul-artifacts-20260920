package service

import (
	"encoding/json"
	"sort"

	"geoterritory/internal/geometry"
	"geoterritory/internal/models"

	"gorm.io/gorm"
)

// RegionShape is one active territory used for assignment: the immutable
// version id, its priority and the validated polygon ring.
type RegionShape struct {
	RegionID        int64
	RegionVersionID int64
	Priority        int
	Polygon         []geometry.Vertex
}

// EffectiveSet is the full set of simultaneously active region versions for
// an organization, fixed by ActiveSetSeq. Assignments are always computed
// against one immutable set so results stay reproducible until a publish.
type EffectiveSet struct {
	SetSeq int64
	Shapes []RegionShape
}

// Assignment is the result of classifying one point: either a region/version
// pair or an "unassigned" sentinel.
type Assignment struct {
	RegionID        *int64
	RegionVersionID *int64
}

var Unassigned = Assignment{}

// LoadSetAtSeq exposes the target-set loader to the worker package.
func LoadSetAtSeq(db *gorm.DB, orgID, targetSeq int64) (*EffectiveSet, error) {
	return loadSetAtSeq(db, orgID, targetSeq)
}

// LoadEffectiveSet exposes the current-set loader.
func LoadEffectiveSet(db *gorm.DB, orgID int64) (*EffectiveSet, error) {
	return loadEffectiveSet(db, orgID)
}

// loadEffectiveSet reads the region versions effective at the org's current
// active_set_seq. Building versions (set_seq 0 / status 'building') are
// excluded, so an in-progress recompute never leaks into queries.
func loadEffectiveSet(db *gorm.DB, orgID int64) (*EffectiveSet, error) {
	var org models.Organization
	if err := db.Select("id, active_set_seq").First(&org, orgID).Error; err != nil {
		return nil, err
	}
	var versions []models.RegionVersion
	if err := db.Where("org_id = ? AND status = 'active'", orgID).Find(&versions).Error; err != nil {
		return nil, err
	}
	set := &EffectiveSet{SetSeq: org.ActiveSetSeq}
	for _, v := range versions {
		var verts []geometry.Vertex
		if err := json.Unmarshal([]byte(v.Polygon), &verts); err != nil {
			return nil, err
		}
		set.Shapes = append(set.Shapes, RegionShape{
			RegionID:        v.RegionID,
			RegionVersionID: v.ID,
			Priority:        v.Priority,
			Polygon:         verts,
		})
	}
	return set, nil
}

// loadSetAtSeq loads the version set for an arbitrary target seq. The set
// contains, per region, the newest version whose set_seq is <= target:
// already-active versions (set_seq < target) plus any 'building' versions
// published by queued jobs whose seq precedes or equals target. The worker
// therefore computes against exactly what the org will look like once all
// jobs up to target have applied, even while those rows are still in
// 'building' status.
func loadSetAtSeq(db *gorm.DB, orgID, targetSeq int64) (*EffectiveSet, error) {
	var versions []models.RegionVersion
	if err := db.Where("org_id = ? AND set_seq > 0 AND set_seq <= ? AND status IN ?",
		orgID, targetSeq, []string{"active", "building"}).
		Order("set_seq ASC").Find(&versions).Error; err != nil {
		return nil, err
	}
	set := &EffectiveSet{SetSeq: targetSeq}
	// versions are ordered ascending; later rows overwrite earlier ones, so
	// the highest set_seq wins per region.
	index := make(map[int64]int)
	for _, v := range versions {
		var verts []geometry.Vertex
		if err := json.Unmarshal([]byte(v.Polygon), &verts); err != nil {
			return nil, err
		}
		shape := RegionShape{
			RegionID:        v.RegionID,
			RegionVersionID: v.ID,
			Priority:        v.Priority,
			Polygon:         verts,
		}
		if idx, ok := index[v.RegionID]; ok {
			set.Shapes[idx] = shape
			continue
		}
		index[v.RegionID] = len(set.Shapes)
		set.Shapes = append(set.Shapes, shape)
	}
	return set, nil
}

// Assign classifies one point against the set.
//
// Overlap rule (deterministic): when a point lies inside more than one
// region the winner is the region with the smallest priority number
// (1 = highest, 5 = lowest); ties between equal priorities are broken by the
// smallest region ID. A point inside no region is Unassigned.
func (s *EffectiveSet) Assign(p geometry.Vertex) Assignment {
	candidates := make([]RegionShape, 0, 4)
	for _, sh := range s.Shapes {
		if geometry.PointInPolygon(p, sh.Polygon) {
			candidates = append(candidates, sh)
		}
	}
	if len(candidates) == 0 {
		return Unassigned
	}
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].Priority != candidates[j].Priority {
			return candidates[i].Priority < candidates[j].Priority
		}
		return candidates[i].RegionID < candidates[j].RegionID
	})
	winner := candidates[0]
	rid := winner.RegionID
	rvid := winner.RegionVersionID
	return Assignment{RegionID: &rid, RegionVersionID: &rvid}
}

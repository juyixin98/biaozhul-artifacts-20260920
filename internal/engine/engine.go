// Package engine implements the local territory assignment rules.
//
// A Catalog is an immutable, in-memory view of one region catalog version.
// Points are assigned by testing against every containing polygon and, when
// several regions overlap, choosing priority 1..5 (smaller wins) and then the
// smaller region ID as a deterministic tie-break.
package engine

import (
	"sort"

	"geoterritory/geometry"
)

// Entry is one region snapshot inside a catalog view.
type Entry struct {
	RegionID        uint64
	RegionVersionID uint64
	Version         int // the region's own snapshot version
	Name            string
	Priority        int
	Vertices        []geometry.LatLng
	MinLat          float64
	MinLng          float64
	MaxLat          float64
	MaxLng          float64
}

// Catalog is an immutable set of region snapshots.
type Catalog struct {
	Version int // catalog version number
	entries []Entry
}

// NewCatalog builds a catalog view. Entries must refer to validated rings
// (validation happens before persistence at publish time).
func NewCatalog(version int, entries []Entry) *Catalog {
	sorted := make([]Entry, len(entries))
	copy(sorted, entries)
	sort.SliceStable(sorted, func(i, j int) bool {
		if sorted[i].Priority != sorted[j].Priority {
			return sorted[i].Priority < sorted[j].Priority
		}
		return sorted[i].RegionID < sorted[j].RegionID
	})
	return &Catalog{Version: version, entries: sorted}
}

// Entries returns the ordered region snapshots (priority, then region ID).
func (c *Catalog) Entries() []Entry {
	out := make([]Entry, len(c.entries))
	copy(out, c.entries)
	return out
}

// Decision is the result of assigning one point. RegionID == 0 means the
// point is outside every region and stays UNASSIGNED.
type Decision struct {
	Matched         bool
	RegionID        uint64
	RegionVersionID uint64
	RegionVersion   int
	OnBoundary      bool
}

// AssignPoint finds the winning region for p.
//
// Boundary points (polygon edge, including vertices) count as inside. The
// winner is the first entry in priority/region-ID order whose polygon
// contains p, so overlap resolution is fully deterministic and stable.
func (c *Catalog) AssignPoint(p geometry.LatLng) Decision {
	for _, e := range c.entries {
		// Cheap bounding-box rejection before the exact ring test.
		if p.Lat < e.MinLat-geometry.Eps || p.Lat > e.MaxLat+geometry.Eps ||
			p.Lng < e.MinLng-geometry.Eps || p.Lng > e.MaxLng+geometry.Eps {
			continue
		}
		loc := geometry.PointInRing(p, []geometry.LatLng(e.Vertices))
		if loc == geometry.Inside {
			return Decision{Matched: true, RegionID: e.RegionID,
				RegionVersionID: e.RegionVersionID, RegionVersion: e.Version}
		}
		if loc == geometry.Boundary {
			return Decision{Matched: true, RegionID: e.RegionID,
				RegionVersionID: e.RegionVersionID, RegionVersion: e.Version,
				OnBoundary: true}
		}
	}
	return Decision{}
}

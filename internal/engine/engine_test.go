package engine

import (
	"testing"

	"geoterritory/geometry"
)

func ring(cx, cy, half float64) []geometry.LatLng {
	return []geometry.LatLng{
		{Lat: cy - half, Lng: cx - half},
		{Lat: cy + half, Lng: cx - half},
		{Lat: cy + half, Lng: cx + half},
		{Lat: cy - half, Lng: cx + half},
	}
}

func entry(id uint64, pri int, vertices []geometry.LatLng) Entry {
	minLat, minLng, maxLat, maxLng := geometry.Bounds(vertices)
	return Entry{RegionID: id, RegionVersionID: id * 10, Version: 1,
		Priority: pri, Vertices: vertices,
		MinLat: minLat, MinLng: minLng, MaxLat: maxLat, MaxLng: maxLng}
}

func TestAssign_PriorityWinsOverlap(t *testing.T) {
	// Two overlapping squares centered at origin: id 1 priority 3, id 2
	// priority 1. Priority 1 must win regardless of entry order.
	hi := entry(2, 1, ring(0, 0, 2))
	lo := entry(1, 3, ring(1, 1, 2))
	cat := NewCatalog(1, []Entry{lo, hi}) // low priority inserted first
	d := cat.AssignPoint(geometry.LatLng{Lat: 1, Lng: 1})
	if !d.Matched || d.RegionID != 2 {
		t.Fatalf("priority-1 region must win overlap, got %+v", d)
	}
}

func TestAssign_TieBreakByRegionID(t *testing.T) {
	a := entry(7, 2, ring(0, 0, 2))
	b := entry(3, 2, ring(0, 0, 2))
	cat := NewCatalog(1, []Entry{a, b})
	d := cat.AssignPoint(geometry.LatLng{Lat: 0, Lng: 0})
	if !d.Matched || d.RegionID != 3 {
		t.Fatalf("equal priority must resolve to smaller region id 3, got %d", d.RegionID)
	}
}

func TestAssign_OutsideIsUnassigned(t *testing.T) {
	cat := NewCatalog(1, []Entry{entry(1, 1, ring(0, 0, 1))})
	if d := cat.AssignPoint(geometry.LatLng{Lat: 5, Lng: 5}); d.Matched {
		t.Fatalf("outside point must be unmatched/unassigned, got %+v", d)
	}
}

func TestAssign_BoundaryCountsInside(t *testing.T) {
	cat := NewCatalog(1, []Entry{entry(1, 1, ring(0, 0, 1))})
	d := cat.AssignPoint(geometry.LatLng{Lat: 0, Lng: -1}) // edge midpoint
	if !d.Matched || !d.OnBoundary {
		t.Fatalf("edge point must match and be flagged boundary, got %+v", d)
	}
}
